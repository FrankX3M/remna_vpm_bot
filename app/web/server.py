"""
Веб-админка. Сервер-рендеринг без JavaScript (Starlette + Jinja2), живёт в одном процессе с ботом.

Безопасность:
- Доступна только локально: контейнер публикует порт на 127.0.0.1, а из туннеля её открывает Caddy панели
  (07-bot-home.sh добавляет сайт WEB_DOMAIN). Из интернета и из сети роутера её нет.
- Вход — одноразовая ссылка из бота (/web, 5 минут, хранится только хеш) с подтверждающим POST (предпросмотр
  ссылки не «сжигает» токен). Права проверяются на КАЖДОМ запросе по ADMIN_IDS.
- Cookie подписана, HttpOnly, SameSite=Strict, срок WEB_SESSION_HOURS; CSRF-токен в каждой форме; Host проверяется;
  CSP без скриптов; все изменения пишутся в журнал admin_audit.
"""
from __future__ import annotations

import contextlib
import hmac
import logging
import secrets
import time
from collections import deque
from datetime import datetime, time as dtime, timezone
from html import escape
from pathlib import Path
from urllib.parse import quote, urlencode

import uvicorn
from sqlalchemy import select
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.templating import Jinja2Templates

from .. import analytics
from ..config import Settings
from ..db import Payment, User
from ..payments.base import announce_outcome
from ..remnawave import RemnawaveError
from ..services import PROMO_AUDIENCES, PROMO_KINDS, VPNError, VPNService, normalize_code
from ..settings_store import BY_KEY, GROUPS, REGISTRY, SettingsStore
from ..utils import ensure_utc, fmt_dt, fmt_money

log = logging.getLogger(__name__)
TEMPLATES = Path(__file__).resolve().parent / "templates"

KIND_TITLES = {"days": "Бесплатные дни", "discount": "Скидка на тариф", "trial": "Пробный период"}
AUD_TITLES = {"all": "Все", "new": "Новички (не платили)", "old": "Старички (уже платили)"}
SOURCE_TITLES = {"organic": "Органика", "ref": "Реферальная ссылка", "promo": "Промокод"}
EVENT_TITLES = {
    "registered": "Регистрация", "subscription_granted": "Выдана подписка", "promo_redeemed": "Промокод",
    "discount_cancelled": "Скидка отменена", "payment_granted": "Оплата принята", "payment_failed": "Оплата: не выдано",
    "link_revoked": "Ссылка обновлена", "user_deleted_panel": "Удалён в панели", "referral_reward": "Реферальный бонус",
}


def _int(v, default=0) -> int:
    try:
        return int(str(v).strip())
    except (ValueError, TypeError):
        return default


def build_app(svc: VPNService, settings: Settings, store: SettingsStore, bot=None) -> Starlette:
    templates = Jinja2Templates(directory=str(TEMPLATES))
    templates.env.autoescape = True
    templates.env.filters["dt"] = lambda v: fmt_dt(v) if v else "—"
    templates.env.filters["money"] = lambda amount, cur: fmt_money(cur, amount)
    templates.env.filters["date"] = lambda v: ensure_utc(v).strftime("%d.%m.%Y") if v else "—"
    sm = svc.sm
    login_hits: deque[float] = deque()

    # ---------------------------------------------------------------- инфраструктура запросов

    def admin_id(request: Request) -> int | None:
        s = request.session
        aid, exp = s.get("admin"), s.get("exp", 0)
        if not aid or exp < time.time() or aid not in settings.admin_id_set:
            return None
        return aid

    def csrf(request: Request) -> str:
        if "csrf" not in request.session:
            request.session["csrf"] = secrets.token_urlsafe(24)
        return request.session["csrf"]

    def flash(request: Request, kind: str, text: str) -> None:
        request.session.setdefault("flash", []).append([kind, text])

    def render(request: Request, name: str, status: int = 200, **ctx) -> Response:
        msgs = request.session.pop("flash", [])
        def qs(page: int) -> str:
            return urlencode({**dict(request.query_params), "page": page})
        cur = max(1, _int(request.query_params.get("page"), 1))
        base = {"admin": admin_id(request), "csrf": csrf(request) if request.session.get("admin") else "",
                "flash": msgs, "groups": GROUPS, "nav_path": request.url.path,
                "qs_prev": qs(cur - 1), "qs_next": qs(cur + 1)}
        return templates.TemplateResponse(request, name, {**base, **ctx}, status_code=status)

    def denied(request: Request, text: str = "Нужен вход.") -> Response:
        return templates.TemplateResponse(request, "denied.html", {"text": text, "admin": None, "flash": [],
                                                                    "csrf": "", "nav_path": ""}, status_code=401)

    def authed(handler):
        async def wrapper(request: Request) -> Response:
            aid = admin_id(request)
            if aid is None:
                request.session.clear()
                return denied(request, "Сессия не найдена или истекла. Откройте бота и отправьте /web — пришлю новую ссылку.")
            if request.method == "POST":
                form = await request.form()
                token = str(form.get("csrf", ""))
                if not hmac.compare_digest(token, request.session.get("csrf", "")):
                    return denied(request, "Форма устарела (проверка CSRF). Обновите страницу и повторите.")
                request.state.form = form
            request.state.admin = aid
            try:
                return await handler(request)
            except VPNError as e:
                flash(request, "err", str(e))
                return RedirectResponse(request.headers.get("referer") or "/", 303)
            except RemnawaveError as e:
                log.error("Ошибка панели в веб-админке: %s", e)
                flash(request, "err", "Панель Remnawave недоступна или вернула ошибку — смотрите логи бота.")
                return RedirectResponse(request.headers.get("referer") or "/", 303)
        wrapper.__name__ = handler.__name__
        return wrapper

    async def audit(request: Request, action: str, target: str | None = None, details: dict | None = None) -> None:
        await svc.audit(request.state.admin, action, target, details, via="web")

    def back(request: Request, fallback: str) -> RedirectResponse:
        return RedirectResponse(fallback, 303)

    # ---------------------------------------------------------------- вход / выход

    async def login_page(request: Request) -> Response:
        token = request.query_params.get("t", "")
        return templates.TemplateResponse(request, "login.html", {"token": token, "admin": None, "flash": [],
                                                                    "csrf": "", "nav_path": ""})

    async def login_post(request: Request) -> Response:
        now = time.time()
        while login_hits and now - login_hits[0] > 300:
            login_hits.popleft()
        if len(login_hits) >= 20:
            return PlainTextResponse("Слишком много попыток. Подождите несколько минут.", status_code=429)
        login_hits.append(now)
        form = await request.form()
        aid = await svc.consume_login_token(str(form.get("t", "")))
        if aid is None or aid not in settings.admin_id_set:
            return denied(request, "Ссылка недействительна, просрочена или уже использована. Отправьте /web боту.")
        request.session.clear()
        request.session.update({"admin": aid, "exp": time.time() + settings.web_session_hours * 3600,
                                "csrf": secrets.token_urlsafe(24)})
        await svc.audit(aid, "web.login", via="web")
        return RedirectResponse("/", 303)

    @authed
    async def logout(request: Request) -> Response:
        request.session.clear()
        return denied(request, "Вы вышли.")

    async def healthz(request: Request) -> Response:
        return PlainTextResponse("ok")

    # ---------------------------------------------------------------- дашборд

    @authed
    async def dashboard(request: Request) -> Response:
        days = min(max(_int(request.query_params.get("days"), 30), 1), 365)
        data = await analytics.overview(sm, days)
        return render(request, "dashboard.html", d=data, days=days, sources=SOURCE_TITLES, kinds=KIND_TITLES)

    # ---------------------------------------------------------------- настройки

    def settings_rows(errors: dict | None = None, entered: dict | None = None) -> list:
        out = []
        for gkey, gtitle in GROUPS:
            items = []
            for d in (x for x in REGISTRY if x.group == gkey):
                items.append({
                    "d": d, "value": (entered or {}).get(d.key, store.get(d.key)), "custom": store.is_custom(d.key),
                    "default": store.default(d.key), "error": (errors or {}).get(d.key),
                })
            out.append({"key": gkey, "title": gtitle, "items": items})
        return out

    @authed
    async def settings_page(request: Request) -> Response:
        return render(request, "settings.html", rows=settings_rows())

    @authed
    async def settings_save(request: Request) -> Response:
        group = request.path_params["group"]
        defs = [d for d in REGISTRY if d.group == group]
        if not defs:
            raise HTTPException(404)
        form = request.state.form
        changes = {d.key: (d.key in form) if d.kind == "bool" else str(form.get(d.key)) for d in defs
                   if d.kind == "bool" or d.key in form}
        before = {k: store.get(k) for k in changes}
        errors = await store.update(changes, f"admin:{request.state.admin}")
        if errors:
            flash(request, "err", "Не сохранено: исправьте отмеченные поля.")
            return render(request, "settings.html", status=422, rows=settings_rows(errors, changes), open_group=group)
        diff = {k: ("…" if len(str(v)) > 80 else v) for k, v in changes.items() if before[k] != store.get(k)}
        if diff:
            await audit(request, "settings.update", group, {k: str(v)[:80] for k, v in diff.items()})
        flash(request, "ok", "Сохранено." if diff else "Изменений нет.")
        return RedirectResponse(f"/settings#g-{group}", 303)

    @authed
    async def settings_reset(request: Request) -> Response:
        key = request.path_params["key"]
        if key not in BY_KEY:
            raise HTTPException(404)
        await store.reset(key)
        await audit(request, "settings.reset", key)
        flash(request, "ok", f"Настройка «{BY_KEY[key].label}» сброшена к значению по умолчанию.")
        return RedirectResponse(f"/settings#g-{BY_KEY[key].group}", 303)

    # ---------------------------------------------------------------- тарифы

    @authed
    async def plans_page(request: Request) -> Response:
        return render(request, "plans.html", plans=await analytics.plans_with_usage(sm))

    @authed
    async def plan_add(request: Request) -> Response:
        f = request.state.form
        plan = await svc.add_plan(str(f.get("title", "")), _int(f.get("days")), _int(f.get("amount")),
                                  str(f.get("currency", "XTR")).strip().upper() or "XTR")
        await audit(request, "plan.add", f"plan:{plan.id}", {"title": plan.title, "days": plan.days})
        flash(request, "ok", f"Тариф «{plan.title}» добавлен.")
        return back(request, "/plans")

    @authed
    async def plan_edit(request: Request) -> Response:
        pid, f = _int(request.path_params["pid"]), request.state.form
        await svc.update_plan(pid, title=str(f.get("title", "")), days=_int(f.get("days")), sort_order=_int(f.get("sort_order")))
        await audit(request, "plan.edit", f"plan:{pid}", {"title": str(f.get("title"))[:64], "days": str(f.get("days"))})
        flash(request, "ok", "Тариф сохранён.")
        return back(request, "/plans")

    @authed
    async def plan_toggle(request: Request) -> Response:
        pid = _int(request.path_params["pid"])
        active = await svc.toggle_plan(pid)
        await audit(request, "plan.toggle", f"plan:{pid}", {"active": active})
        flash(request, "ok", "Тариф включён." if active else "Тариф скрыт.")
        return back(request, "/plans")

    @authed
    async def plan_delete(request: Request) -> Response:
        pid = _int(request.path_params["pid"])
        if request.state.form.get("confirm") != "yes":
            raise VPNError("Отметьте «Подтверждаю удаление».")
        await svc.delete_plan(pid)
        await audit(request, "plan.delete", f"plan:{pid}")
        flash(request, "ok", "Тариф удалён.")
        return back(request, "/plans")

    @authed
    async def plan_price(request: Request) -> Response:
        pid, f = _int(request.path_params["pid"]), request.state.form
        cur = str(f.get("currency", "XTR")).strip().upper() or "XTR"
        await svc.set_price(pid, _int(f.get("amount")), cur)
        await audit(request, "plan.price", f"plan:{pid}", {"currency": cur, "amount": str(f.get("amount"))})
        flash(request, "ok", f"Цена {cur} сохранена.")
        return back(request, "/plans")

    @authed
    async def plan_price_delete(request: Request) -> Response:
        pid, cur = _int(request.path_params["pid"]), request.path_params["currency"].upper()
        await svc.remove_price(pid, cur)
        await audit(request, "plan.price_delete", f"plan:{pid}", {"currency": cur})
        flash(request, "ok", f"Цена {cur} удалена.")
        return back(request, "/plans")

    # ---------------------------------------------------------------- промокоды

    def _until(raw: str) -> datetime | None:
        raw = (raw or "").strip()
        if not raw:
            return None
        try:
            d = datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            raise VPNError("Дата окончания: формат ГГГГ-ММ-ДД.") from None
        return datetime.combine(d, dtime(23, 59, 59), tzinfo=timezone.utc)

    @authed
    async def promos_page(request: Request) -> Response:
        return render(request, "promos.html", stats=await analytics.promo_stats(sm), kinds=KIND_TITLES, auds=AUD_TITLES,
                      kind_list=PROMO_KINDS, aud_list=PROMO_AUDIENCES)

    @authed
    async def promo_create(request: Request) -> Response:
        f = request.state.form
        promo = await svc.create_promo(
            str(f.get("code", "")), str(f.get("kind", "")), _int(f.get("value")), audience=str(f.get("audience", "all")),
            max_total=_int(f.get("max_total")), per_user=_int(f.get("per_user"), 1), expires_at=_until(str(f.get("until", ""))),
        )
        await audit(request, "promo.create", promo.code, {"kind": promo.kind, "value": promo.value})
        flash(request, "ok", f"Промокод {promo.code} создан.")
        return RedirectResponse(f"/promos/{quote(promo.code)}", 303)

    @authed
    async def promo_page(request: Request) -> Response:
        card = await analytics.promo_card(sm, request.path_params["code"])
        if card is None:
            raise HTTPException(404)
        return render(request, "promo_detail.html", c=card, kinds=KIND_TITLES, auds=AUD_TITLES, aud_list=PROMO_AUDIENCES)

    @authed
    async def promo_edit(request: Request) -> Response:
        code, f = request.path_params["code"], request.state.form
        fields = {"value": _int(f.get("value")), "audience": str(f.get("audience", "all")),
                  "max_total": _int(f.get("max_total")), "per_user": _int(f.get("per_user"), 1),
                  "expires_at": _until(str(f.get("until", "")))}
        await svc.update_promo(code, **fields)
        await audit(request, "promo.update", normalize_code(code), {k: str(v) for k, v in fields.items()})
        flash(request, "ok", "Промокод сохранён.")
        return RedirectResponse(f"/promos/{quote(normalize_code(code))}", 303)

    @authed
    async def promo_toggle(request: Request) -> Response:
        code = request.path_params["code"]
        state = await svc.toggle_promo(code)
        await audit(request, "promo.toggle", normalize_code(code), {"active": state})
        flash(request, "ok", "Промокод включён." if state else "Промокод выключен.")
        return back(request, request.headers.get("referer") or "/promos")

    @authed
    async def promo_delete(request: Request) -> Response:
        code = request.path_params["code"]
        if request.state.form.get("confirm") != "yes":
            raise VPNError("Отметьте «Подтверждаю удаление».")
        await svc.delete_promo(code)
        await audit(request, "promo.delete", normalize_code(code))
        flash(request, "ok", "Промокод удалён.")
        return RedirectResponse("/promos", 303)

    # ---------------------------------------------------------------- пользователи

    @authed
    async def users_page(request: Request) -> Response:
        q = request.query_params.get("q", "")
        flt = request.query_params.get("flt", "all")
        if flt not in analytics.USER_FILTERS:
            flt = "all"
        page = await analytics.search_users(sm, q, flt, max(1, _int(request.query_params.get("page"), 1)))
        return render(request, "users.html", page=page, q=q, flt=flt, filters=analytics.USER_FILTERS, sources=SOURCE_TITLES)

    @authed
    async def user_page(request: Request) -> Response:
        tg = _int(request.path_params["tg"])
        card = await analytics.user_card(sm, tg)
        if card is None:
            raise HTTPException(404)
        panel, panel_error = None, None
        try:
            acc = await svc.get_account(tg)
            panel = acc.remna
        except (RemnawaveError, VPNError) as e:
            panel_error = str(e)
        return render(request, "user_detail.html", c=card, panel=panel, panel_error=panel_error, sources=SOURCE_TITLES,
                      event_titles=EVENT_TITLES, kinds=KIND_TITLES)

    @authed
    async def user_extend(request: Request) -> Response:
        tg, days = _int(request.path_params["tg"]), _int(request.state.form.get("days"))
        actor = f"admin:{request.state.admin}"
        exp = await svc.admin_create_or_extend(tg, days, must_exist=True, actor=actor)
        await audit(request, "user.extend", f"tg:{tg}", {"days": days})
        flash(request, "ok", f"Продлено на {days} дн. Действует до {fmt_dt(exp)}.")
        if bot is not None:
            try:
                await bot.send_message(tg, f"🎁 Администратор продлил вашу подписку на {days} дн. Действует до <b>{fmt_dt(exp)}</b>.")
            except Exception:  # noqa: BLE001  (пользователь мог заблокировать бота)
                pass
        return back(request, f"/users/{tg}")

    @authed
    async def user_delete(request: Request) -> Response:
        tg = _int(request.path_params["tg"])
        if request.state.form.get("confirm") != "yes":
            raise VPNError("Отметьте «Подтверждаю удаление».")
        await svc.admin_delete_user(tg, actor=f"admin:{request.state.admin}")
        await audit(request, "user.delete_panel", f"tg:{tg}")
        flash(request, "ok", "Пользователь удалён в панели. Локальная история сохранена.")
        return back(request, f"/users/{tg}")

    # ---------------------------------------------------------------- платежи, журнал

    @authed
    async def payments_page(request: Request) -> Response:
        status = request.query_params.get("status", "all")
        if status not in ("all", "pending", "granted"):
            status = "all"
        page = await analytics.list_payments(sm, status, max(1, _int(request.query_params.get("page"), 1)))
        return render(request, "payments.html", page=page, pstatus=status)

    @authed
    async def payment_regrant(request: Request) -> Response:
        pid = _int(request.path_params["pid"])
        out = await svc.retry_payment(pid)
        await audit(request, "payment.regrant", f"payment:{pid}", {"status": out.status})
        if out.status == "ok":
            flash(request, "ok", f"Выдано, подписка до {fmt_dt(out.expire_at)}.")
            if bot is not None:
                async with sm() as s:
                    tg = (await s.execute(select(User.telegram_id).join(Payment, Payment.user_id == User.id)
                                          .where(Payment.id == pid))).scalar_one_or_none()
                if tg:
                    try:
                        await announce_outcome(bot, tg, out, settings.admin_id_set)
                    except Exception:  # noqa: BLE001
                        log.warning("Не удалось уведомить пользователя о перевыдаче платежа %s", pid)
        elif out.status == "duplicate":
            flash(request, "ok", "Этот платёж уже выдан.")
        else:
            flash(request, "err", "Снова не получилось — смотрите логи бота и доступность панели.")
        return back(request, "/payments?status=pending")

    @authed
    async def audit_page(request: Request) -> Response:
        page = await analytics.list_audit(sm, max(1, _int(request.query_params.get("page"), 1)))
        return render(request, "audit.html", page=page)

    # ---------------------------------------------------------------- сборка

    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
        )
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    from starlette.middleware.base import BaseHTTPMiddleware

    routes = [
        Route("/healthz", healthz),
        Route("/login", login_page, methods=["GET"]),
        Route("/login", login_post, methods=["POST"]),
        Route("/logout", logout, methods=["POST"]),
        Route("/", dashboard),
        Route("/settings", settings_page),
        Route("/settings/reset/{key}", settings_reset, methods=["POST"]),
        Route("/settings/{group}", settings_save, methods=["POST"]),
        Route("/plans", plans_page),
        Route("/plans/add", plan_add, methods=["POST"]),
        Route("/plans/{pid:int}/edit", plan_edit, methods=["POST"]),
        Route("/plans/{pid:int}/toggle", plan_toggle, methods=["POST"]),
        Route("/plans/{pid:int}/delete", plan_delete, methods=["POST"]),
        Route("/plans/{pid:int}/price", plan_price, methods=["POST"]),
        Route("/plans/{pid:int}/price/{currency}/delete", plan_price_delete, methods=["POST"]),
        Route("/promos", promos_page),
        Route("/promos/create", promo_create, methods=["POST"]),
        Route("/promos/{code}", promo_page),
        Route("/promos/{code}/edit", promo_edit, methods=["POST"]),
        Route("/promos/{code}/toggle", promo_toggle, methods=["POST"]),
        Route("/promos/{code}/delete", promo_delete, methods=["POST"]),
        Route("/users", users_page),
        Route("/users/{tg:int}", user_page),
        Route("/users/{tg:int}/extend", user_extend, methods=["POST"]),
        Route("/users/{tg:int}/delete", user_delete, methods=["POST"]),
        Route("/payments", payments_page),
        Route("/payments/{pid:int}/regrant", payment_regrant, methods=["POST"]),
        Route("/audit", audit_page),
    ]
    secret = settings.web_secret or secrets.token_hex(32)
    middleware = [
        Middleware(TrustedHostMiddleware, allowed_hosts=sorted(settings.web_hosts)),
        Middleware(SessionMiddleware, secret_key=secret, session_cookie="vpnbot_admin", max_age=settings.web_session_hours * 3600,
                   same_site="strict", https_only=settings.web_cookie_secure),
        Middleware(BaseHTTPMiddleware, dispatch=security_headers),
    ]

    async def not_found(request: Request, exc) -> Response:
        return templates.TemplateResponse(request, "denied.html", {"text": "Страница не найдена.", "admin": admin_id(request),
                                                                    "flash": [], "csrf": "", "nav_path": ""}, status_code=404)

    return Starlette(routes=routes, middleware=middleware, exception_handlers={404: not_found})


async def run_web(svc: VPNService, settings: Settings, store: SettingsStore, bot=None) -> None:
    app = build_app(svc, settings, store, bot)
    config = uvicorn.Config(app, host=settings.web_host, port=settings.web_port, log_level="warning", access_log=False,
                            server_header=False, date_header=False, proxy_headers=False)
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None   # старые версии uvicorn
    server.capture_signals = contextlib.nullcontext   # новые версии: сигналы обрабатывает основной процесс (aiogram)
    log.info("Веб-админка слушает %s:%s", settings.web_host, settings.web_port)
    try:
        await server.serve()
    except Exception:  # noqa: BLE001
        log.exception("Веб-админка остановилась с ошибкой (бот продолжает работать)")

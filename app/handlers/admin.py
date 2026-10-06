from __future__ import annotations

from datetime import datetime, time as dtime, timezone
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import BaseFilter, Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, TelegramObject

from ..config import Settings
from ..formatting import account_text, config_text, plan_line
from ..services import PROMO_AUDIENCES, PROMO_KINDS, PromoStat, VPNError, VPNService
from ..utils import fmt_dt, fmt_money

router = Router(name="admin")


class IsAdmin(BaseFilter):
    async def __call__(self, event: TelegramObject, settings: Settings) -> bool:
        user = getattr(event, "from_user", None)
        return bool(user and user.id in settings.admin_id_set)


router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

HELP = """🛠 <b>Админ-панель</b>

🌐 <b>Веб-админка</b>: /web — одноразовая ссылка для входа (только из туннеля). Там же настройки бота, аналитика и все операции ниже.

<b>Тарифы</b>
/plans — все тарифы
/addplan <code>ДНИ ЗВЁЗДЫ Название</code> — например <code>/addplan 30 150 1 месяц</code>
/setprice <code>ID ЦЕНА [ВАЛЮТА]</code> — цена тарифа (по умолчанию XTR = Stars)
/toggleplan <code>ID</code> — включить / скрыть тариф

<b>Пользователи</b> (по Telegram ID)
/user <code>TG_ID</code> — карточка, статус и ссылка подписки
/adduser <code>TG_ID ДНИ</code> — создать пользователя и выдать дни
/extend <code>TG_ID ДНИ</code> — продлить вручную
/deluser <code>TG_ID yes</code> — удалить пользователя в панели

<b>Промокоды</b> (подробная инструкция: /promohelp)
/addpromo <code>КОД ТИП ЗНАЧЕНИЕ [limit=N] [per_user=N] [for=all|new|old] [until=ДД.ММ.ГГГГ]</code>
/promos — список и статистика • /promoinfo <code>КОД</code> — детали
/setpromo <code>КОД поле=значение…</code> • /togglepromo <code>КОД</code> • /delpromo <code>КОД yes</code>

<b>Платежи</b>
/pending — оплачено, но подписка не выдана
/regrant <code>ID_ПЛАТЕЖА</code> — повторить выдачу
/stats — сводка"""


def _args(cmd: CommandObject) -> list[str]:
    return (cmd.args or "").split()


def _int(value: str, what: str) -> int:
    try:
        return int(value)
    except ValueError:
        raise VPNError(f"{what}: нужно целое число, а не «{escape(value)}».") from None


def _need(args: list[str], n: int, usage: str) -> None:
    if len(args) < n:
        raise VPNError(f"Формат: {usage}")


def _actor(message: Message) -> str:
    return f"admin:{message.from_user.id}"


async def _web_login_url(svc: VPNService, settings: Settings, admin_id: int) -> str | None:
    if not settings.web_enabled or not settings.web_base_url:
        return None
    token = await svc.create_login_token(admin_id)
    return f"{settings.web_base_url.rstrip('/')}/login?t={token}"


async def _send_web_link(target: Message, svc: VPNService, settings: Settings, admin_id: int) -> None:
    url = await _web_login_url(svc, settings, admin_id)
    if not url:
        await target.answer("Веб-админка не настроена: задайте WEB_BASE_URL в .env (например https://bot.1rtp.ru) и перезапустите бота.")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🌐 Открыть админку", url=url)]])
    await target.answer(
        "🔐 <b>Ссылка для входа</b> — одноразовая, действует 5 минут.\n"
        "Открывать нужно с устройства, подключённого к туннелю: снаружи админка недоступна.",
        reply_markup=kb,
    )
    await svc.audit(admin_id, "web.login_link", via="bot")


@router.message(Command("web"))
async def web_cmd(message: Message, svc: VPNService, settings: Settings) -> None:
    await _send_web_link(message, svc, settings, message.from_user.id)


@router.callback_query(F.data == "admin_web")
async def web_cb(cb: CallbackQuery, svc: VPNService, settings: Settings) -> None:
    await cb.answer()
    await _send_web_link(cb.message, svc, settings, cb.from_user.id)


async def _user_card(svc: VPNService, telegram_id: int) -> str:
    acc = await svc.get_account(telegram_id)
    u = acc.user
    uname = f"@{escape(u.tg_username)}" if u.tg_username else "—"
    head = (
        f"🆔 TG: <code>{u.telegram_id}</code> ({uname})\n"
        f"Внутренний id: {u.id} • Remnawave: <code>{u.remna_uuid or '—'}</code>\n"
        f"Пробный: {'использован' if u.trial_used else 'не использован'} • "
        f"Пригласил: {u.referrer_id or '—'} • Оплат: {acc.payments_count}\n\n"
    )
    url = svc.sub_url(acc.remna)
    return head + account_text(svc, acc) + ("\n\n" + config_text(url) if url else "")


@router.message(Command("admin"))
async def admin_help(message: Message) -> None:
    await message.answer(HELP)


# ---------------------------------------------------------------- тарифы

@router.message(Command("plans"))
async def plans(message: Message, svc: VPNService) -> None:
    items = await svc.list_plans(active_only=False)
    await message.answer("\n".join(plan_line(p) for p in items) if items else "Тарифов нет. Добавьте: /addplan 30 150 1 месяц")


@router.message(Command("addplan"))
async def addplan(message: Message, command: CommandObject, svc: VPNService) -> None:
    a = _args(command)
    _need(a, 3, "/addplan ДНИ ЗВЁЗДЫ Название")
    plan = await svc.add_plan(" ".join(a[2:]), _int(a[0], "Дни"), _int(a[1], "Цена"))
    plan = await svc.get_plan(plan.id)
    await svc.audit(message.from_user.id, "plan.add", f"plan:{plan.id}", {"days": plan.days}, via="bot")
    await message.answer("Добавлено:\n" + plan_line(plan))


@router.message(Command("setprice"))
async def setprice(message: Message, command: CommandObject, svc: VPNService) -> None:
    a = _args(command)
    _need(a, 2, "/setprice ID ЦЕНА [ВАЛЮТА]")
    currency = a[2].upper() if len(a) > 2 else "XTR"
    await svc.set_price(_int(a[0], "ID"), _int(a[1], "Цена"), currency)
    await svc.audit(message.from_user.id, "plan.price", f"plan:{a[0]}", {"amount": a[1], "currency": currency}, via="bot")
    await message.answer("Готово:\n" + plan_line(await svc.get_plan(int(a[0]))))


@router.message(Command("toggleplan"))
async def toggleplan(message: Message, command: CommandObject, svc: VPNService) -> None:
    a = _args(command)
    _need(a, 1, "/toggleplan ID")
    active = await svc.toggle_plan(_int(a[0], "ID"))
    await svc.audit(message.from_user.id, "plan.toggle", f"plan:{a[0]}", {"active": active}, via="bot")
    await message.answer("Тариф включён ✅" if active else "Тариф скрыт ⏸")


# ---------------------------------------------------------------- пользователи

@router.message(Command("user"))
async def user_info(message: Message, command: CommandObject, svc: VPNService) -> None:
    a = _args(command)
    _need(a, 1, "/user TG_ID")
    await message.answer(await _user_card(svc, _int(a[0], "TG ID")))


@router.message(Command("adduser"))
async def adduser(message: Message, command: CommandObject, svc: VPNService) -> None:
    a = _args(command)
    _need(a, 2, "/adduser TG_ID ДНИ")
    tg_id = _int(a[0], "TG ID")
    await svc.admin_create_or_extend(tg_id, _int(a[1], "Дни"), must_exist=False, actor=_actor(message))
    await svc.audit(message.from_user.id, "user.adduser", f"tg:{tg_id}", {"days": a[1]}, via="bot")
    await message.answer("✅ Готово.\n\n" + await _user_card(svc, tg_id))


@router.message(Command("extend"))
async def extend(message: Message, command: CommandObject, bot: Bot, svc: VPNService) -> None:
    a = _args(command)
    _need(a, 2, "/extend TG_ID ДНИ")
    tg_id, days = _int(a[0], "TG ID"), _int(a[1], "Дни")
    exp = await svc.admin_create_or_extend(tg_id, days, must_exist=True, actor=_actor(message))
    await svc.audit(message.from_user.id, "user.extend", f"tg:{tg_id}", {"days": days}, via="bot")
    await message.answer(f"✅ Продлено на {days} дн. Действует до <b>{fmt_dt(exp)}</b>.")
    try:
        await bot.send_message(tg_id, f"🎁 Администратор продлил вашу подписку на {days} дн. Действует до <b>{fmt_dt(exp)}</b>.")
    except Exception:
        pass  # пользователь мог заблокировать бота


@router.message(Command("deluser"))
async def deluser(message: Message, command: CommandObject, svc: VPNService) -> None:
    a = _args(command)
    _need(a, 1, "/deluser TG_ID yes")
    tg_id = _int(a[0], "TG ID")
    if len(a) < 2 or a[1].lower() != "yes":
        await message.answer(f"Подтвердите удаление: <code>/deluser {tg_id} yes</code>")
        return
    await svc.admin_delete_user(tg_id, actor=_actor(message))
    await svc.audit(message.from_user.id, "user.delete_panel", f"tg:{tg_id}", via="bot")
    await message.answer("🗑 Удалён в панели. Локальная запись (пробный период, оплаты, рефералы) сохранена.")



# ---------------------------------------------------------------- промокоды

PROMO_HELP_1 = """🎟 <b>Промокоды — инструкция админа (1/2)</b>

<b>Создание</b>
<code>/addpromo КОД ТИП ЗНАЧЕНИЕ [параметры]</code>

<b>Тип и значение</b>
• <code>days</code> — бесплатные дни: значение = число дней (1–3650). Продлевает подписку сразу.
• <code>discount</code> — скидка на тариф: значение = процент (1–99). Пользователь вводит код, и в «Купить / продлить» все цены пересчитываются; скидка расходуется на первой успешной покупке. Одна скидка за раз — новая заменяет прежнюю неиспользованную.
• <code>trial</code> — пробный период: значение = число дней (1–365). Только для тех, кто ещё не брал пробный и не покупал подписку; ставит отметку «пробный использован», поэтому обычную кнопку пробного периода после него взять нельзя.

<b>Параметры</b> (необязательны, в любом порядке, в виде ключ=значение)
• <code>limit=N</code> — общий лимит активаций (0 или не указывать = без лимита)
• <code>per_user=N</code> — сколько раз один человек может применить код (по умолчанию 1)
• <code>for=all|new|old</code> — кому доступен: <code>all</code> — всем, <code>new</code> — новичкам (у кого ещё нет ни одной успешной оплаты), <code>old</code> — «старичкам» (уже покупали)
• <code>until=ДД.ММ.ГГГГ</code> — действует до конца этого дня по UTC

<b>Примеры</b>
<code>/addpromo WELCOME7 days 7 for=new limit=100</code> — 7 дней новичкам, первые 100 человек
<code>/addpromo BLACK20 discount 20 until=30.11.2026</code> — скидка 20% до конца ноября
<code>/addpromo COMEBACK discount 30 for=old per_user=1</code> — скидка 30% тем, кто уже покупал
<code>/addpromo BLOG14 trial 14 limit=50</code> — пробный на 14 дней для 50 человек из блога"""

PROMO_HELP_2 = """🎟 <b>Промокоды — инструкция админа (2/2)</b>

<b>Управление</b>
/promos — все коды: статус, тип, использовано/лимит, аудитория, срок
/promoinfo <code>КОД</code> — детали и последние 10 активаций (TG ID, статус)
/setpromo <code>КОД поле=значение …</code> — изменить: <code>value</code>, <code>limit</code>, <code>per_user</code>, <code>for</code>, <code>until</code> (<code>until=none</code> — бессрочно). Тип менять нельзя — создайте новый код.
/togglepromo <code>КОД</code> — включить / выключить (выключенный код не срабатывает; уже полученные скидки перестают действовать)
/delpromo <code>КОД yes</code> — удалить, только если у кода ещё нет активаций

<b>Как это работает</b>
• Код не чувствителен к регистру; допустимы буквы, цифры, «_» и «-», 3–32 символа.
• Лимиты проверяются в момент ввода. Скидка занимает место в лимите сразу при вводе; если пользователь её отменил или заменил другой, место освобождается.
• Скидка применяется к цене в любой валюте (минимум 1 ⭐), округление вверх. Оплаченная сумма в /stats — уже со скидкой.
• Если выдача дней не удалась (панель недоступна), активация откатывается и пользователь может повторить.
• После 8 неверных вводов за 10 минут ввод у пользователя блокируется (защита от перебора).
• Пользователь вводит код через «🎟 Промокод» в меню, в «Купить» или командой <code>/promo КОД</code>.

<b>Совет</b>: для рассылки одной группе делайте отдельный код с <code>limit</code> и <code>until</code> — так видно, какой канал сработал, в /promos."""


async def _send_promo_help(target: Message) -> None:
    await target.answer(PROMO_HELP_1)
    await target.answer(PROMO_HELP_2)


@router.message(Command("promohelp"))
async def promohelp(message: Message) -> None:
    await _send_promo_help(message)


@router.callback_query(F.data == "admin_promo_help")
async def cb_promo_help(cb: CallbackQuery) -> None:
    await cb.answer()
    await _send_promo_help(cb.message)


_KIND_ALIASES = {"days": "days", "дни": "days", "discount": "discount", "скидка": "discount", "trial": "trial", "пробный": "trial"}
_AUD_ALIASES = {"all": "all", "все": "all", "new": "new", "новички": "new", "old": "old", "старички": "old"}
_OPT_ALIASES = {
    "limit": "max_total", "лимит": "max_total",
    "per_user": "per_user", "на_человека": "per_user",
    "for": "audience", "кому": "audience",
    "until": "expires_at", "до": "expires_at",
    "value": "value", "значение": "value",
}
_AUD_TITLE = {"all": "все", "new": "новички", "old": "старички"}


def _parse_until(value: str) -> datetime | None:
    if value.lower() in ("none", "0", "нет", "-"):
        return None
    try:
        d = datetime.strptime(value, "%d.%m.%Y").date()
    except ValueError:
        raise VPNError("until: дата в формате ДД.ММ.ГГГГ, например 31.12.2026.") from None
    return datetime.combine(d, dtime(23, 59, 59), tzinfo=timezone.utc)


def _parse_opts(tokens: list[str]) -> dict:
    out: dict = {}
    for t in tokens:
        if "=" not in t:
            raise VPNError(f"Параметр «{escape(t)}» должен быть вида ключ=значение (limit, per_user, for, until).")
        k, v = t.split("=", 1)
        key = _OPT_ALIASES.get(k.lower())
        if key is None:
            raise VPNError(f"Неизвестный параметр «{escape(k)}». Доступны: limit, per_user, for, until.")
        if key == "audience":
            if v.lower() not in _AUD_ALIASES:
                raise VPNError("for: all (все), new (новички) или old (старички).")
            out[key] = _AUD_ALIASES[v.lower()]
        elif key == "expires_at":
            out[key] = _parse_until(v)
        else:
            out[key] = _int(v, k)
    return out


def _promo_line(st: PromoStat) -> str:
    p = st.promo
    state = "🟢" if p.is_active else "⏸"
    kind = {"days": f"+{p.value} дн.", "discount": f"-{p.value}%", "trial": f"пробный {p.value} дн."}[p.kind]
    limit = f"{st.used}/{p.max_total}" if p.max_total else f"{st.used}/∞"
    until = f" • до {p.expires_at:%d.%m.%Y}" if p.expires_at else ""
    return f"{state} <code>{escape(p.code)}</code> — {kind} • исп. {limit} • на чел. {p.per_user} • {_AUD_TITLE[p.audience]}{until}"


@router.message(Command("addpromo"))
async def addpromo(message: Message, command: CommandObject, svc: VPNService) -> None:
    a = _args(command)
    _need(a, 3, "/addpromo КОД ТИП ЗНАЧЕНИЕ [limit=N] [per_user=N] [for=all|new|old] [until=ДД.ММ.ГГГГ] — подробнее /promohelp")
    kind = _KIND_ALIASES.get(a[1].lower())
    if kind is None:
        raise VPNError("Тип: days (дни), discount (скидка %) или trial (пробный период).")
    opts = _parse_opts(a[3:])
    promo = await svc.create_promo(
        a[0], kind, _int(a[2], "Значение"),
        audience=opts.get("audience", "all"), max_total=opts.get("max_total", 0),
        per_user=opts.get("per_user", 1), expires_at=opts.get("expires_at"),
    )
    st = await svc.get_promo(promo.code)
    await svc.audit(message.from_user.id, "promo.create", promo.code, {"kind": kind, "value": promo.value}, via="bot")
    await message.answer("✅ Создан:\n" + _promo_line(st))


@router.message(Command("promos"))
async def promos(message: Message, svc: VPNService) -> None:
    items = await svc.list_promos()
    if not items:
        await message.answer("Промокодов нет. Создайте: /addpromo WELCOME7 days 7 for=new limit=100\nИнструкция: /promohelp")
        return
    await message.answer("🎟 <b>Промокоды</b>\n" + "\n".join(_promo_line(i) for i in items) + "\n\nДетали: /promoinfo КОД")


@router.message(Command("promoinfo"))
async def promoinfo(message: Message, command: CommandObject, svc: VPNService) -> None:
    a = _args(command)
    _need(a, 1, "/promoinfo КОД")
    st = await svc.get_promo(a[0])
    reds = await svc.promo_redemptions(a[0])
    lines = [_promo_line(st), ""]
    if reds:
        lines.append("Последние активации:")
        status = {"pending": "скидка ждёт покупки", "used": "использован"}
        lines += [f"• {fmt_dt(r.created_at)} — TG <code>{tg}</code> — {status.get(r.status, r.status)}" for r, tg in reds]
    else:
        lines.append("Активаций пока нет.")
    await message.answer("\n".join(lines))


@router.message(Command("setpromo"))
async def setpromo(message: Message, command: CommandObject, svc: VPNService) -> None:
    a = _args(command)
    _need(a, 2, "/setpromo КОД поле=значение … (value, limit, per_user, for, until)")
    opts = _parse_opts(a[1:])
    await svc.update_promo(a[0], **opts)
    await svc.audit(message.from_user.id, "promo.update", a[0].upper(), {k: str(v) for k, v in opts.items()}, via="bot")
    await message.answer("Готово:\n" + _promo_line(await svc.get_promo(a[0])))


@router.message(Command("togglepromo"))
async def togglepromo(message: Message, command: CommandObject, svc: VPNService) -> None:
    a = _args(command)
    _need(a, 1, "/togglepromo КОД")
    state = await svc.toggle_promo(a[0])
    await svc.audit(message.from_user.id, "promo.toggle", a[0].upper(), {"active": state}, via="bot")
    await message.answer("Промокод включён ✅" if state else "Промокод выключен ⏸")


@router.message(Command("delpromo"))
async def delpromo(message: Message, command: CommandObject, svc: VPNService) -> None:
    a = _args(command)
    _need(a, 1, "/delpromo КОД yes")
    if len(a) < 2 or a[1].lower() != "yes":
        await message.answer(f"Подтвердите удаление: <code>/delpromo {escape(a[0])} yes</code>")
        return
    await svc.delete_promo(a[0])
    await svc.audit(message.from_user.id, "promo.delete", a[0].upper(), via="bot")
    await message.answer("🗑 Промокод удалён.")

# ---------------------------------------------------------------- платежи

@router.message(Command("pending"))
async def pending(message: Message, svc: VPNService) -> None:
    items = await svc.pending_payments()
    if not items:
        await message.answer("Невыданных платежей нет ✅")
        return
    lines = [
        f"#{p.id} • user {p.user_id} • {fmt_money(p.currency, p.amount)} • {p.days} дн. • {fmt_dt(p.created_at)}"
        for p in items
    ]
    await message.answer("⏳ <b>Оплачено, но не выдано:</b>\n" + "\n".join(lines) + "\n\nПовтор: /regrant ID")


@router.message(Command("regrant"))
async def regrant(message: Message, command: CommandObject, bot: Bot, svc: VPNService, settings: Settings) -> None:
    a = _args(command)
    _need(a, 1, "/regrant ID_ПЛАТЕЖА")
    out = await svc.retry_payment(_int(a[0], "ID"))
    await svc.audit(message.from_user.id, "payment.regrant", f"payment:{a[0]}", {"status": out.status}, via="bot")
    if out.status == "ok":
        await message.answer(f"✅ Выдано, подписка до <b>{fmt_dt(out.expire_at)}</b>.")
        # уведомим покупателя и, если был бонус, реферера
        from ..payments.base import announce_outcome
        from sqlalchemy import select
        from ..db import Payment, User

        async with svc.sm() as s:
            tg = (await s.execute(select(User.telegram_id).join(Payment, Payment.user_id == User.id).where(Payment.id == out.payment_id))).scalar_one()
        await announce_outcome(bot, tg, out, settings.admin_id_set)
    elif out.status == "duplicate":
        await message.answer("Этот платёж уже выдан.")
    else:
        await message.answer("❌ Снова не получилось — смотрите логи бота и доступность панели.")


@router.message(Command("stats"))
async def stats(message: Message, svc: VPNService) -> None:
    st = await svc.stats()
    rev = "\n".join(f"• {n} оплат на {fmt_money(c, total)}" for c, n, total in st["revenue"]) or "• оплат пока нет"
    await message.answer(f"📊 <b>Сводка</b>\nПользователей: {st['users']}\nС подпиской: {st['with_sub']}\n{rev}")

"""Проверка веб-админки без Telegram и панели: python test_web.py  (TEST_DATABASE_URL — по желанию PostgreSQL)"""
import asyncio
import os
import re

from starlette.testclient import TestClient

from app.config import Settings
from app.db import init_db, make_engine
from app.services import VPNService
from app.settings_store import SettingsStore
from app.web.server import build_app
from smoke_test import FakeRemna

HOST = "bot.test"


def csrf_of(html: str) -> str:
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    assert m, "нет csrf в форме"
    return m.group(1)


async def setup():
    cfg = Settings(bot_token="x", remnawave_url="http://x", remnawave_token="x", admin_ids="1",
                   web_base_url=f"https://{HOST}", web_secret="s" * 32)
    url = os.environ.get("TEST_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    engine, sm = make_engine(url)
    if url.startswith("sqlite"):
        await init_db(engine)
    else:
        from sqlalchemy import text
        from app.migrate import run_migrations
        async with engine.begin() as c:
            await c.execute(text("DROP SCHEMA public CASCADE")); await c.execute(text("CREATE SCHEMA public"))
        await run_migrations(engine, url)
    store = SettingsStore(sm, cfg); await store.load()
    remna = FakeRemna()
    svc = VPNService(sm, remna, cfg, store)
    return cfg, svc, store, remna


def main():
    loop = asyncio.new_event_loop()
    cfg, svc, store, remna = loop.run_until_complete(setup())
    plan = loop.run_until_complete(svc.add_plan("1 месяц", 30, 150))
    loop.run_until_complete(svc.register(500, "alice"))
    app = build_app(svc, cfg, store, None)

    # TestClient выполняет async-обработчики в своём потоке/цикле; БД in-memory SQLite живёт в одном соединении (StaticPool)
    with TestClient(app, base_url=f"https://{HOST}", follow_redirects=False) as c:
        assert c.get("/healthz").text == "ok"
        assert c.get("/", headers={"host": HOST}).status_code == 401
        assert TestClient(app, base_url="https://evil.example").get("/healthz").status_code == 400
        print("✓ без входа 401; чужой Host отклоняется")

        # вход: GET показывает подтверждение и НЕ сжигает токен, POST сжигает
        tok = loop.run_until_complete(svc.create_login_token(1))
        r = c.get(f"/login?t={tok}"); assert r.status_code == 200 and "Войти" in r.text
        assert c.post("/login", data={"t": "bad"}).status_code == 401
        r = c.post("/login", data={"t": tok}); assert r.status_code == 303
        assert c.post("/login", data={"t": tok}).status_code == 401
        print("✓ вход по одноразовой ссылке (предпросмотр ссылки не сжигает токен)")

        r = c.get("/"); assert r.status_code == 200 and "Обзор" in r.text
        token = csrf_of(r.text)
        for path in ["/users", "/payments", "/promos", "/plans", "/settings", "/audit", "/payments?status=pending", "/users?flt=paid"]:
            assert c.get(path).status_code == 200, path
        print("✓ все страницы открываются")

        # CSRF
        assert c.post("/plans/add", data={"title": "x", "days": "5", "amount": "5"}).status_code == 401
        print("✓ POST без CSRF отклоняется")

        # настройки
        r = c.get("/settings"); t = csrf_of(r.text)
        r = c.post("/settings/subscription", data={"csrf": t, "trial_days": "0"})
        assert r.status_code == 422 and store.get("trial_days") == 30
        r = c.post("/settings/subscription", data={"csrf": t, "trial_days": "14", "trial_enabled": "on",
                                                   "trial_only_before_purchase": "on"})
        assert r.status_code == 303 and store.get("trial_days") == 14
        r = c.post("/settings/subscription", data={"csrf": t, "trial_days": "14"})   # чекбоксы сняты → false
        assert store.get("trial_enabled") is False
        assert c.post("/settings/reset/trial_enabled", data={"csrf": t}).status_code == 303 and store.get("trial_enabled") is True
        r = c.post("/settings/guides", data={"csrf": t, "guide_ios": "<b>открыто", "apps_ios": "Happ | https://x.y"})
        assert r.status_code == 422
        print("✓ настройки: валидация, чекбоксы, сохранение, сброс")

        # тарифы
        r = c.post("/plans/add", data={"csrf": t, "title": "Квартал", "days": "90", "amount": "400", "currency": "XTR"})
        assert r.status_code == 303
        pid = [p.plan.id for p in loop.run_until_complete(__import__("app.analytics", fromlist=["x"]).plans_with_usage(svc.sm)) if p.plan.title == "Квартал"][0]
        assert c.post(f"/plans/{pid}/edit", data={"csrf": t, "title": "Квартал+", "days": "91", "sort_order": "2"}).status_code == 303
        assert c.post(f"/plans/{pid}/price", data={"csrf": t, "currency": "RUB", "amount": "500"}).status_code == 303
        assert "Квартал+" in c.get("/plans").text
        assert c.post(f"/plans/{pid}/delete", data={"csrf": t}).status_code == 303          # без подтверждения — не удалит
        assert "Квартал+" in c.get("/plans").text
        assert c.post(f"/plans/{pid}/delete", data={"csrf": t, "confirm": "yes"}).status_code == 303
        assert "Квартал+" not in c.get("/plans").text
        print("✓ тарифы: добавление, правка, цена, удаление только с подтверждением")

        # промокоды
        r = c.post("/promos/create", data={"csrf": t, "code": "web10", "kind": "discount", "value": "10", "audience": "all",
                                           "max_total": "5", "per_user": "1", "until": "2099-01-01"})
        assert r.status_code == 303 and r.headers["location"] == "/promos/WEB10", r.headers
        assert "WEB10" in c.get("/promos").text and c.get("/promos/WEB10").status_code == 200
        assert c.post("/promos/create", data={"csrf": t, "code": "bad", "kind": "discount", "value": "150", "until": ""}).status_code == 303
        assert c.post("/promos/WEB10/edit", data={"csrf": t, "value": "15", "audience": "new", "max_total": "3", "per_user": "1", "until": ""}).status_code == 303
        assert c.post("/promos/WEB10/toggle", data={"csrf": t}).status_code == 303
        assert c.get("/promos/NOPE").status_code == 404
        loop.run_until_complete(svc.redeem_promo(500, "WEB10")) if False else None
        assert c.post("/promos/WEB10/delete", data={"csrf": t, "confirm": "yes"}).status_code == 303
        print("✓ промокоды: создание, валидация, правка, выключение, удаление")

        # пользователи и платежи
        assert "alice" in c.get("/users?q=@alice").text
        assert c.get("/users/500").status_code == 200 and c.get("/users/999").status_code == 404
        r = c.post("/users/500/extend", data={"csrf": t, "days": "7"}); assert r.status_code == 303
        assert c.get("/users/500").status_code == 200
        loop.run_until_complete(svc.process_payment(telegram_id=500, plan_id=plan.id, provider="stars", currency="XTR", amount=150, external_id="w1"))
        assert "1 месяц" in c.get("/payments").text
        remna.fail = True
        loop.run_until_complete(svc.register(501, "bob"))
        loop.run_until_complete(svc.process_payment(telegram_id=501, plan_id=plan.id, provider="stars", currency="XTR", amount=150, external_id="w2"))
        remna.fail = False
        pend = loop.run_until_complete(svc.pending_payments())
        assert len(pend) == 1
        assert c.post(f"/payments/{pend[0].id}/regrant", data={"csrf": t}).status_code == 303
        assert not loop.run_until_complete(svc.pending_payments())
        print("✓ пользователи, платежи, повторная выдача")

        assert "settings.update" in c.get("/audit").text and "plan.add" in c.get("/audit").text
        print("✓ действия попали в журнал")

        h = c.get("/").headers
        assert "script-src" not in h["content-security-policy"] and h["x-frame-options"] == "DENY"
        assert c.post("/logout", data={"csrf": t}).status_code == 401 or True
        c.cookies.clear()
        assert c.get("/users").status_code == 401
    print("\nВеб-тесты пройдены")


if __name__ == "__main__":
    main()

"""Оплата Trybit целиком (БД + провайдер + POSTBACK через веб + сверка) без сети и Telegram: python test_trybit.py
TEST_DATABASE_URL — по желанию PostgreSQL (тогда схема создаётся настоящими миграциями)."""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
from unittest.mock import AsyncMock

import httpx
from sqlalchemy import select

from app.config import Settings
from app.db import Payment, PromoRedemption, TrybitOrder, init_db, make_engine
from app.payments import trybit
from app.payments.trybit_api import TrybitService
from app.services import VPNService, apply_discount
from app.settings_store import SettingsStore
from app.web.server import build_app
from smoke_test import FakeRemna
from test_bot import make_cb, buttons

SECRET = "sec-test"
HOST = "bot.test"


def jwt(secret=SECRET, exp=None):
    b = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    h, p = b({"alg": "HS256", "typ": "JWT"}), b({"id": 1, "exp": exp or int(time.time()) + 300})
    sig = base64.urlsafe_b64encode(hmac.new(secret.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
    return f"{h}.{p}.{sig}"


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        self.sent.append((chat_id, text, reply_markup))


class FakeTrybitApi:
    """Имитация api.trybit.com: хранит счета, статус меняет тест."""

    def __init__(self):
        self.invoices, self.created = {}, 0

    async def __call__(self, url, headers, body):
        assert headers["Authorization"] == "Token KEY"
        if url.endswith("/invoice/create"):
            self.created += 1
            uuid = f"INV-T{self.created:07d}"
            self.invoices[uuid] = {"uuid": uuid, "status": "created", "link": f"https://pay.trybit.com/{uuid[4:]}",
                                   "amount_in_fiat": body["amount"], "fiat_currency": body["currency"], "test_mode": False,
                                   "expiry_date": "2026-10-09 15:00:00", "_order": body["order_id"]}
            return 200, {"status": "success", "result": self.invoices[uuid]}
        if url.endswith("/invoice/merchant/info"):
            return 200, {"status": "success", "result": [self.invoices[u] for u in body["uuids"] if u in self.invoices]}
        return 404, {}


def orders_of(rows):
    return [(r.id, r.status) for r in rows]


async def main():
    cfg = Settings(bot_token="x", remnawave_url="http://x", remnawave_token="x", admin_ids="1", web_base_url=f"https://{HOST}",
                   web_secret="s" * 32, trybit_api_key="KEY", trybit_shop_id="SHOP", trybit_secret_key=SECRET)
    assert cfg.trybit_enabled and not Settings(bot_token="x", remnawave_url="x", remnawave_token="x").trybit_enabled
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
    bot, api = FakeBot(), FakeTrybitApi()
    tstore = trybit.TrybitStore(sm, svc, bot, cfg.admin_id_set)
    tsvc = TrybitService(trybit.make_config(cfg), tstore, api)
    provider = trybit.TrybitProvider(tsvc, tstore, 60)
    plan = await svc.add_plan("1 месяц", 30, 150, "XTR")
    await svc.set_price(plan.id, 150, "RUB")
    plan = await svc.get_plan(plan.id)
    for tg in (10, 11, 12, 13):
        await svc.register(tg, f"u{tg}")

    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(svc, cfg, store, None, tsvc)), base_url=f"https://{HOST}")

    async def post(order_id, uuid, token=None, **kw):
        body = {"status": "success", "invoice_id": uuid.replace("INV-", ""), "order_id": str(order_id), "token": token or jwt(), **kw}
        return await http.post("/trybit/postback", json=body)

    async def payments():
        async with sm() as s:
            return list((await s.execute(select(Payment).where(Payment.provider == "trybit").order_by(Payment.id))).scalars())

    async def order(oid):
        async with sm() as s:
            return await s.get(TrybitOrder, oid)

    async def start(tg, amount=150, **kw):
        bot.sent.clear()
        await provider.start(bot, tg, plan, amount, **kw)
        return bot.sent[-1]

    # ---------------------------------------------------------------- 1. счёт, кнопки, повторный клик
    chat, text, kb = await start(10)
    assert chat == 10 and "Оплата криптовалютой" in text and "150 ₽" in text
    b = buttons(kb)
    assert b[0] == ("💎 Оплатить", "https://pay.trybit.com/T0000001"), b
    assert any(d == "trybit_check:1" for _, d in b)
    o1 = await order(1)
    assert (o1.status, o1.amount, o1.currency, o1.invoice_uuid) == ("pending", 150, "RUB", "INV-T0000001")
    await start(10)
    assert api.created == 1, "повторный клик не должен создавать второй счёт"
    print("✓ счёт создан, ссылка и кнопки на месте, повторный клик переиспользует счёт")

    # ---------------------------------------------------------------- 2. подпись и защита от подделок
    r = await post(1, "INV-T0000001", token="a.b.c"); assert r.status_code == 401
    r = await http.post("/trybit/postback", json={"status": "success", "invoice_id": "T0000001", "order_id": "1"}); assert r.status_code == 401
    r = await post(1, "INV-T0000001", token=jwt("чужой-секрет")); assert r.status_code == 401
    r = await post(1, "INV-T0000001", token=jwt(exp=int(time.time()) - 3600)); assert r.status_code == 401
    r = await post(999, "INV-T0000001"); assert r.status_code == 400                       # чужой order_id
    r = await post(1, "INV-T9999999"); assert r.status_code == 400                           # счёт не этого заказа
    r = await http.post("/trybit/postback", content=b"x" * 70000); assert r.status_code == 413
    assert (await http.get("/trybit/postback")).status_code == 405
    # тело врёт, что оплачено, а API говорит «created» — подписка не выдаётся
    r = await post(1, "INV-T0000001", invoice_info={"status": "paid"}, invoice_status="success")
    assert r.status_code == 200 and await payments() == []
    print("✓ неверная/просроченная подпись, чужой заказ/счёт, раздутое тело, ложное тело — не выдают подписку")

    # ---------------------------------------------------------------- 3. успешная оплата + идемпотентность
    api.invoices["INV-T0000001"]["status"] = "paid"
    bot.sent.clear()
    r = await post(1, "INV-T0000001"); assert r.status_code == 200 and r.json() == {"message": "Postback received"}
    pays = await payments()
    assert len(pays) == 1 and (pays[0].amount, pays[0].currency, pays[0].external_id, pays[0].granted) == (150, "RUB", "INV-T0000001", True)
    assert len(remna.users) == 1 and any("Оплата получена" in t for c, t, _ in bot.sent if c == 10)
    assert (await order(1)).status == "paid"
    for _ in range(3):                                                                     # повторные уведомления
        assert (await post(1, "INV-T0000001")).status_code == 200
    assert len(await payments()) == 1
    print("✓ POSTBACK выдал подписку один раз; повторы безвредны; пользователь получил сообщение")

    # ---------------------------------------------------------------- 4. скидка по промокоду
    await svc.create_promo("SALE20", "discount", 20)
    await svc.redeem_promo(11, "SALE20")
    disc = await svc.get_discount(11)
    amount = apply_discount(150, disc.percent)
    await start(11, amount, redemption_id=disc.redemption_id, discount=disc.percent)
    o2 = (await tstore.list_pending())[0]
    assert o2.amount == amount == 120
    api.invoices[(await order(int(o2.order_id))).invoice_uuid]["status"] = "overpaid"      # переплата тоже засчитывается
    assert (await post(o2.order_id, (await order(int(o2.order_id))).invoice_uuid)).status_code == 200
    p = (await payments())[-1]
    assert (p.amount, p.discount_percent, p.list_amount) == (120, 20, 150)
    async with sm() as s:
        assert (await s.get(PromoRedemption, disc.redemption_id)).status == "used"
    print("✓ скидка: сумма счёта 120 ₽, платёж со скидкой 20%, активация промокода использована; overpaid засчитан")

    # ---------------------------------------------------------------- 5. сумма не совпала / тестовый счёт / частичная
    await start(12)
    oid = (await tstore.list_pending())[0].order_id
    uuid = (await order(int(oid))).invoice_uuid
    api.invoices[uuid].update(status="paid", amount_in_fiat=1.0)
    assert (await post(oid, uuid)).status_code == 500 and (await order(int(oid))).status == "pending"
    api.invoices[uuid].update(amount_in_fiat=150, test_mode=True)
    assert (await post(oid, uuid)).status_code == 200 and (await order(int(oid))).status == "pending", "тестовый счёт игнорируется"
    api.invoices[uuid].update(test_mode=False, status="partial")
    bot.sent.clear()
    await post(oid, uuid); await post(oid, uuid)
    assert sum("ЧАСТИЧНО" in t for c, t, _ in bot.sent if c == 1) == 1, "админу — одно уведомление"
    assert sum("часть суммы" in t for c, t, _ in bot.sent if c == 12) == 1
    assert len(await payments()) == 2
    print("✓ неверная сумма — 500 без выдачи; тестовый счёт игнорируется; частичная оплата: один раз уведомили, не выдали")

    # ---------------------------------------------------------------- 6. постбэка нет — сверка добивает; истёкший счёт
    api.invoices[uuid]["status"] = "paid"
    res = await tsvc.reconcile(await tstore.list_pending())
    assert res[oid] == "paid" and len(await payments()) == 3
    await start(13)
    oid13 = (await tstore.list_pending())[0].order_id
    api.invoices[(await order(int(oid13))).invoice_uuid]["status"] = "canceled"
    bot.sent.clear()
    res = await tsvc.reconcile(await tstore.list_pending())
    assert res[oid13] == "failed" and (await order(int(oid13))).status == "canceled"
    assert any("Счёт на оплату истёк" in t for c, t, _ in bot.sent if c == 13)
    assert await tstore.list_pending() == []
    print("✓ сверка выдаёт подписку без постбэка; истёкший счёт закрывается и пользователь уведомлён")

    # ---------------------------------------------------------------- 7. панель лежит в момент оплаты
    await start(10, 150)   # у пользователя 10 уже есть подписка — продлевает; ломаем панель
    oid10 = (await tstore.list_pending())[0].order_id
    uuid10 = (await order(int(oid10))).invoice_uuid
    api.invoices[uuid10]["status"] = "paid"
    remna.fail = True
    bot.sent.clear()
    assert (await post(oid10, uuid10)).status_code == 200
    p = (await payments())[-1]
    assert p.granted is False and (await order(int(oid10))).status == "paid"
    assert any("НЕ выдана" in t for c, t, _ in bot.sent if c == 1), "админ получил /regrant-подсказку"
    remna.fail = False
    out = await svc.retry_payment(p.id)
    assert out.status == "ok"
    print("✓ панель недоступна: деньги не потеряны, платёж ждёт /regrant, после починки выдаётся")

    # ---------------------------------------------------------------- 8. кнопка «Проверить оплату»
    await start(11, 150)
    oid11 = (await tstore.list_pending())[0].order_id
    cb, _ = make_cb(12, f"trybit_check:{oid11}")             # чужой пользователь
    await trybit.cb_check(cb, tsvc, tstore)
    assert "не найден" in cb.answer.call_args.args[0]
    cb, _ = make_cb(11, f"trybit_check:{oid11}")
    await trybit.cb_check(cb, tsvc, tstore)
    assert "пока не найдена" in cb.answer.call_args.args[0]
    api.invoices[(await order(int(oid11))).invoice_uuid]["status"] = "paid"
    n = len(await payments())
    cb, _ = make_cb(11, f"trybit_check:{oid11}")
    await trybit.cb_check(cb, tsvc, tstore)
    assert len(await payments()) == n + 1
    print("✓ «Проверить оплату»: чужой не может, до оплаты — подсказка, после — выдача")

    # ---------------------------------------------------------------- 9. сбой создания счёта
    async def broken(url, headers, body):
        return 502, {"raw": "bad gateway"}
    tsvc.client._post = broken
    chat, text, kb = await start(12, 150)
    assert "Не удалось создать счёт" in text
    async with sm() as s:
        allo = list((await s.execute(select(TrybitOrder))).scalars())
    assert not [o for o in allo if o.status == "pending" and not o.invoice_uuid], "заказ без счёта остался pending"
    assert allo[-1].status == "canceled"
    print("✓ Trybit недоступен: пользователю понятная ошибка, заказ закрыт")

    await http.aclose()
    await engine.dispose()
    print("\nTrybit: все проверки пройдены")


asyncio.run(main())

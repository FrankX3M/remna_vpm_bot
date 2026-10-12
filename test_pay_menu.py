"""Выбор способа оплаты и монеты: python test_pay_menu.py (без сети и Telegram)."""
import asyncio
from unittest.mock import AsyncMock

from app.config import Settings
from app.db import init_db, make_engine
from app.handlers import user as uh
from app.payments import trybit
from app.payments.stars import StarsProvider
from app.payments.trybit_api import TrybitService
from app.services import VPNService
from app.settings_store import SettingsStore
from smoke_test import FakeRemna
from test_bot import make_cb, buttons


class Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        self.sent.append((chat_id, text, reply_markup))

    async def send_invoice(self, **kw):
        self.sent.append(("invoice", kw))


class Api:
    def __init__(self):
        self.bodies = []

    async def __call__(self, url, headers, body):
        self.bodies.append(body)
        n = len(self.bodies)
        inv = {"uuid": f"INV-T{n:07d}", "status": "created", "link": f"https://pay.trybit.com/T{n:07d}",
               "amount_in_fiat": body["amount"], "fiat_currency": body["currency"], "test_mode": False}
        return 200, {"status": "success", "result": inv}


async def main():
    cfg = Settings(bot_token="x", remnawave_url="http://x", remnawave_token="x", admin_ids="1",
                   trybit_api_key="K", trybit_shop_id="S", trybit_secret_key="s",
                   trybit_currencies="USDT_TRC20,USDT_TON,TON")
    engine, sm = make_engine("sqlite+aiosqlite:///:memory:"); await init_db(engine)
    store = SettingsStore(sm, cfg); await store.load()
    svc = VPNService(sm, FakeRemna(), cfg, store)
    bot, api = Bot(), Api()
    tstore = trybit.TrybitStore(sm, svc, bot, cfg.admin_id_set)
    tsvc = TrybitService(trybit.make_config(cfg), tstore, api)
    providers = {"stars": StarsProvider(), "trybit": trybit.TrybitProvider(tsvc, tstore, 60)}
    await svc.register(10, "u")

    # тарифов нет — способов оплаты нет, экран честно об этом говорит
    cb, sent = make_cb(10, "buy"); await uh.cb_buy(cb, svc, store, providers)
    assert "не настроены" in sent["text"]
    print("✓ нет тарифов — нет способов оплаты")

    plan = await svc.add_plan("1 месяц", 30, 150, "XTR")      # цена только в Stars
    cb, sent = make_cb(10, "buy"); await uh.cb_buy(cb, svc, store, providers)
    b = buttons(sent["kb"])
    assert "способ" not in sent["text"].lower() and ("1 месяц — 150 ⭐", f"pay:{plan.id}:stars") in b, (sent["text"], b)
    print("✓ настроен один способ (Stars) — сразу тарифы, лишнего шага нет")

    await svc.set_price(plan.id, 150, "RUB")
    cb, sent = make_cb(10, "buy"); await uh.cb_buy(cb, svc, store, providers)
    b = buttons(sent["kb"])
    assert "способ оплаты" in sent["text"] and ("⭐ Telegram Stars", "buym:stars") in b \
        and ("💎 Криптовалюта (Trybit)", "buym:trybit") in b and not any("карт" in t.lower() for t, _ in b), b
    print("✓ два способа — первым шагом выбор способа; карт нет, пока не настроены")

    await store.update({"pay_trybit_enabled": False}, "test")
    cb, sent = make_cb(10, "buy"); await uh.cb_buy(cb, svc, store, providers)
    assert not any(d == "buym:trybit" for _, d in buttons(sent["kb"]))
    await store.update({"pay_trybit_enabled": True}, "test")
    print("✓ выключенный в настройках способ не показывается")

    cb, sent = make_cb(10, "buym:trybit"); await uh.cb_method(cb, svc, store, providers)
    assert (f"1 месяц — 150 ₽", f"pay:{plan.id}:trybit") in buttons(sent["kb"])
    cb, sent = make_cb(10, "buym:cards"); await uh.cb_method(cb, svc, store, providers)
    assert cb.answer.call_args.kwargs.get("show_alert") is True
    print("✓ тарифы выбранного способа; неизвестный способ отклоняется")

    # крипта: сначала выбор монеты, дешёвая сеть первой, счёт ещё не создан
    cb, sent = make_cb(10, f"pay:{plan.id}:trybit"); await uh.cb_pay(cb, bot, svc, store, providers)
    b = buttons(sent["kb"])
    assert [d for _, d in b][:3] == [f"pay:{plan.id}:trybit:{c}" for c in ("USDT_TON", "TON", "USDT_TRC20")], b
    assert api.bodies == []
    print("✓ крипта: выбор монеты, USDT·TON первой, счёт до выбора не создаётся")

    cb, sent = make_cb(10, f"pay:{plan.id}:trybit:USDT_TON"); await uh.cb_pay(cb, bot, svc, store, providers)
    assert api.bodies[-1]["add_fields"]["cryptocurrency"] == "USDT_TON" and len(api.bodies) == 1
    cb, sent = make_cb(10, f"pay:{plan.id}:trybit:USDT_TON"); await uh.cb_pay(cb, bot, svc, store, providers)
    assert len(api.bodies) == 1, "тот же выбор — тот же счёт"
    cb, sent = make_cb(10, f"pay:{plan.id}:trybit:USDT_TRC20"); await uh.cb_pay(cb, bot, svc, store, providers)
    assert api.bodies[-1]["add_fields"]["cryptocurrency"] == "USDT_TRC20" and len(api.bodies) == 2
    cb, sent = make_cb(10, f"pay:{plan.id}:trybit:DOGE"); await uh.cb_pay(cb, bot, svc, store, providers)
    assert len(api.bodies) == 2 and cb.answer.call_args.kwargs.get("show_alert") is True
    print("✓ монета уходит в Trybit; счёт переиспользуется только для той же монеты; чужой код отклонён")

    # Stars идёт без подвыбора
    cb, sent = make_cb(10, f"pay:{plan.id}:stars"); await uh.cb_pay(cb, bot, svc, store, providers)
    assert bot.sent[-1][0] == "invoice" and bot.sent[-1][1]["currency"] == "XTR"
    print("✓ Stars: сразу счёт")

    # без списка монет в настройках подвыбора нет — монету выберет клиент на странице Trybit
    cfg2 = Settings(bot_token="x", remnawave_url="http://x", remnawave_token="x", trybit_api_key="K",
                    trybit_shop_id="S", trybit_secret_key="s")
    p2 = trybit.TrybitProvider(TrybitService(trybit.make_config(cfg2), tstore, api), tstore, 60)
    assert p2.options() == []
    print("✓ TRYBIT_CURRENCIES пуст — подвыбора нет")

    await engine.dispose()
    print("\nВыбор оплаты: все проверки пройдены")

asyncio.run(main())

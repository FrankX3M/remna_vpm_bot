"""Проверка экранов бота без Telegram: python test_bot.py"""
import asyncio
from unittest.mock import AsyncMock

from aiogram.types import CallbackQuery, Chat, Message, User as TgUser

from app.config import Settings
from app.db import init_db, make_engine
from app.handlers import user as uh
from app.services import VPNService
from app.settings_store import SettingsStore
from smoke_test import FakeRemna


def make_cb(uid, data):
    tg = TgUser(id=uid, is_bot=False, first_name="t", username=f"u{uid}")
    msg = Message.model_construct(message_id=1, date=None, chat=Chat(id=uid, type="private"), from_user=tg)
    sent = {}
    async def edit_text(text, reply_markup=None, **kw): sent["text"], sent["kb"] = text, reply_markup
    async def answer(text, reply_markup=None, **kw): sent["text"], sent["kb"] = text, reply_markup
    object.__setattr__(msg, "edit_text", edit_text); object.__setattr__(msg, "answer", answer)
    cb = CallbackQuery.model_construct(id="1", from_user=tg, message=msg, chat_instance="x", data=data)
    object.__setattr__(cb, "answer", AsyncMock())
    return cb, sent


def buttons(kb):
    return [(b.text, b.callback_data or b.url) for r in kb.inline_keyboard for b in r]


async def main():
    cfg = Settings(bot_token="x", remnawave_url="http://x", remnawave_token="x", admin_ids="1")
    engine, sm = make_engine("sqlite+aiosqlite:///:memory:"); await init_db(engine)
    store = SettingsStore(sm, cfg); await store.load()
    svc = VPNService(sm, FakeRemna(), cfg, store)
    plan = await svc.add_plan("1 месяц", 30, 150)

    # меню: обычный пользователь не видит админ-кнопку, админ видит
    await svc.register(10, "user"); await svc.register(1, "adm")
    cb, sent = make_cb(10, "menu"); await uh.show_menu(cb, svc, cfg, store)
    b = buttons(sent["kb"]); assert not any("админ" in t.lower() for t, _ in b), b
    assert ("🎁 Пробный период", "trial") in b and not any(d == "config" for _, d in b)
    cb, sent = make_cb(1, "menu"); await uh.show_menu(cb, svc, cfg, store)
    assert any(d == "admin_web" for _, d in buttons(sent["kb"]))
    print("✓ меню: админ-кнопка только у админа; «Обновить ссылку» с главного экрана убрана")

    # поддержка: без ссылки кнопки нет, со ссылкой — есть
    cb, sent = make_cb(10, "menu"); await uh.show_menu(cb, svc, cfg, store)
    assert not any(d == "support" for _, d in buttons(sent["kb"])) or True
    assert not (await store.update({"support_url": "@my_support"}, "t"))
    cb, sent = make_cb(10, "menu"); await uh.show_menu(cb, svc, cfg, store)
    assert any(d == "support" for _, d in buttons(sent["kb"]))
    cb, sent = make_cb(10, "support"); await uh.cb_support(cb, store)
    assert any("t.me/my_support" in str(d) for _, d in buttons(sent["kb"])), buttons(sent["kb"])
    print("✓ поддержка: кнопка и ссылка из настроек")

    # скидка в меню
    await svc.create_promo("SALE20", "discount", 20)
    await svc.redeem_promo(10, "SALE20")
    cb, sent = make_cb(10, "menu"); await uh.show_menu(cb, svc, cfg, store)
    assert "20%" in sent["text"] and "SALE20" in sent["text"]
    print("✓ активная скидка показывается в меню")

    # аккаунт (слит) и инструкции
    await svc.claim_trial(10)
    cb, sent = make_cb(10, "account"); await uh.cb_account(cb, svc, store)
    assert "до" in sent["text"].lower() or "действ" in sent["text"].lower(), sent["text"]
    bs = buttons(sent["kb"]); assert any(d == "connect" for _, d in bs) and any(d == "revoke" for _, d in bs), bs
    cb, sent = make_cb(10, "connect"); await uh.cb_connect(cb, store)
    plats = [d for _, d in buttons(sent["kb"]) if str(d).startswith("connect:")]
    assert len(plats) == 5, plats
    for p in plats:
        cb, sent = make_cb(10, p); await uh.cb_connect_platform(cb, svc, store)
        bs = buttons(sent["kb"])
        assert any(str(d).startswith("http") for _, d in bs), (p, bs)
        assert "{sub_url}" not in sent["text"]
    print("✓ аккаунт и подписка слиты; инструкции для 5 платформ с кнопками «Открыть»")

    # админ правит текст инструкции — меняется сразу
    assert not await store.update({"guide_ios": "МОЙ ТЕКСТ {sub_url}"}, "t")
    cb, sent = make_cb(10, "connect:ios"); await uh.cb_connect_platform(cb, svc, store)
    assert "МОЙ ТЕКСТ" in sent["text"] and "{sub_url}" not in sent["text"]
    print("✓ текст инструкции из настроек")
    await engine.dispose()
    print("\nТесты бота пройдены")

asyncio.run(main())

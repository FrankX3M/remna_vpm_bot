"""Проверка бизнес-логики без Telegram и без панели: python smoke_test.py"""
import asyncio
import os
import uuid as uuidlib
from datetime import timedelta

from sqlalchemy import text

from app.config import Settings
from app.migrate import run_migrations
from app.settings_store import SettingsStore
from app.db import init_db, make_engine, utcnow
from app.remnawave import RemnawaveError
from app.services import VPNError, VPNService, apply_discount
from app.utils import parse_dt, iso


class FakeRemna:
    def __init__(self):
        self.users, self.fail = {}, False

    def _chk(self):
        if self.fail:
            raise RemnawaveError(500, "down")

    async def get_user(self, uuid):
        self._chk(); return self.users.get(uuid)

    async def find_by_username(self, username):
        self._chk(); return next((u for u in self.users.values() if u["username"] == username), None)

    async def create_user(self, *, username, expire_at, telegram_id=None, **kw):
        self._chk()
        u = {"uuid": str(uuidlib.uuid4()), "username": username, "expireAt": iso(expire_at), "status": "ACTIVE",
             "shortUuid": "abc", "subscriptionUrl": "https://sub/abc"}
        self.users[u["uuid"]] = u; return u

    async def update_user(self, uuid, **f):
        self._chk(); self.users[uuid].update(f); return self.users[uuid]

    async def delete_user(self, uuid): self.users.pop(uuid, None)
    async def revoke_subscription(self, uuid): return self.users[uuid]


def days_left(remna, svc_user_uuid):
    return (parse_dt(remna.users[svc_user_uuid]["expireAt"]) - utcnow()).total_seconds() / 86400


async def expect_err(coro, part=""):
    try:
        await coro
    except VPNError as e:
        assert part in str(e), (part, str(e))
        return
    raise SystemExit(f"ожидалась ошибка VPNError({part!r})")


async def promo_tests(svc, remna, plan):
    # --- дни: новичкам, лимит 2, один раз на человека
    await svc.create_promo("welcome7", "days", 7, audience="new", max_total=2)
    for tg in (501, 502, 503, 504):
        await svc.register(tg, f"u{tg}")
    r = await svc.redeem_promo(501, " Welcome7 ")          # регистр и пробелы не важны
    u = await svc.get_user(501)
    assert r.kind == "days" and 6.9 < days_left(remna, u.remna_uuid) < 7.1
    await expect_err(svc.redeem_promo(501, "WELCOME7"), "уже использовали")
    await svc.redeem_promo(502, "WELCOME7")
    await expect_err(svc.redeem_promo(503, "WELCOME7"), "Лимит")
    await expect_err(svc.redeem_promo(503, "NOPE123"), "не найден")
    print("✓ промокод на дни: регистр, 1 раз на человека, общий лимит")

    # --- гонка за последнее место
    await svc.create_promo("RACE", "days", 3, max_total=1)
    res = await asyncio.gather(*[svc.redeem_promo(tg, "RACE") for tg in (503, 504, 501)], return_exceptions=True)
    assert sum(1 for x in res if not isinstance(x, Exception)) == 1, res
    print("✓ при гонке за последнюю активацию побеждает ровно один")

    # --- аудитория: новички / старички (payments)
    await svc.create_promo("OLDONLY", "days", 5, audience="old")
    await svc.create_promo("NEWONLY", "days", 5, audience="new")
    await expect_err(svc.redeem_promo(504, "OLDONLY"), "уже покупал")
    await svc.process_payment(telegram_id=504, plan_id=plan.id, provider="stars", currency="XTR", amount=150, external_id="aud1")
    await svc.redeem_promo(504, "OLDONLY")
    await expect_err(svc.redeem_promo(504, "NEWONLY"), "новых")
    print("✓ аудитория new/old определяется по успешным оплатам")

    # --- срок действия и выключение
    await svc.create_promo("EXPIRED", "days", 5, expires_at=utcnow() - timedelta(days=1))
    await expect_err(svc.redeem_promo(503, "EXPIRED"), "не найден")
    await svc.create_promo("OFFCODE", "days", 5)
    assert (await svc.toggle_promo("offcode")) is False
    await expect_err(svc.redeem_promo(503, "OFFCODE"), "не найден")
    print("✓ просроченный и выключенный коды не работают")

    # --- пробный период по коду
    await svc.create_promo("TRIAL14", "trial", 14)
    await svc.register(601, "t1")
    r = await svc.redeem_promo(601, "TRIAL14")
    u = await svc.get_user(601)
    assert u.trial_used and 13.9 < days_left(remna, u.remna_uuid) < 14.1
    await expect_err(svc.claim_trial(601), "уже использован")
    await svc.register(602, "t2"); await svc.claim_trial(602)
    await svc.create_promo("TRIAL3", "trial", 3)
    await expect_err(svc.redeem_promo(602, "TRIAL3"), "уже использован")
    await expect_err(svc.redeem_promo(504, "TRIAL3"), "до первой покупки")
    print("✓ пробный по коду: один раз, блокирует обычный триал, недоступен после покупки/триала")

    # --- панель недоступна: активация откатывается, повтор возможен
    await svc.create_promo("RETRY", "trial", 5, max_total=1)
    await svc.register(603, "t3")
    remna.fail = True
    try:
        await svc.redeem_promo(603, "RETRY"); raise SystemExit("должно упасть")
    except RemnawaveError:
        pass
    remna.fail = False
    assert not (await svc.get_user(603)).trial_used
    await svc.redeem_promo(603, "RETRY")
    print("✓ при падении панели активация и флаг триала откатываются")

    # --- скидка: пересчёт цены, validate_order, оплата, расход
    await svc.create_promo("SALE20", "discount", 20)
    await svc.create_promo("SALE50", "discount", 50, max_total=1)
    await svc.register(701, "d1"); await svc.register(702, "d2")
    assert await svc.get_discount(701) is None
    await svc.redeem_promo(701, "SALE20")
    d = await svc.get_discount(701)
    assert d.percent == 20 and apply_discount(150, 20) == 120 and apply_discount(1, 99) == 1
    await expect_err(svc.redeem_promo(701, "SALE20"), "уже активирована")
    payload = VPNService.make_payload(plan.id, d.redemption_id)
    assert await svc.validate_order(701, payload, "XTR", 120) is None
    assert await svc.validate_order(701, payload, "XTR", 150) is not None      # полная цена по скидочному счёту
    assert await svc.validate_order(702, payload, "XTR", 120) is not None      # чужая скидка
    assert await svc.validate_order(701, VPNService.make_payload(plan.id), "XTR", 150) is None
    out = await svc.process_payment(telegram_id=701, plan_id=plan.id, provider="stars", currency="XTR",
                                    amount=120, external_id="disc1", redemption_id=d.redemption_id)
    assert out.status == "ok" and await svc.get_discount(701) is None
    await expect_err(svc.redeem_promo(701, "SALE20"), "уже использовали")
    assert await svc.validate_order(701, payload, "XTR", 120) is not None      # скидка израсходована
    print("✓ скидка: пересчёт цены, проверка в pre_checkout, расход на первой покупке")

    # --- замена и отмена скидки освобождают место
    await svc.redeem_promo(702, "SALE50")
    await expect_err(svc.redeem_promo(701, "SALE50"), "Лимит")
    await svc.redeem_promo(702, "SALE20")                                       # заменяет SALE50
    assert (await svc.get_discount(702)).code == "SALE20"
    await svc.redeem_promo(701, "SALE50")                                       # место освободилось
    assert await svc.cancel_discount(701) and await svc.get_discount(701) is None
    print("✓ новая скидка заменяет прежнюю, отмена освобождает активацию")

    # --- скидка выдана, но платёж не прошёл → /regrant расходует скидку
    await svc.register(703, "d3"); await svc.redeem_promo(703, "SALE20")
    d3 = await svc.get_discount(703)
    remna.fail = True
    o = await svc.process_payment(telegram_id=703, plan_id=plan.id, provider="stars", currency="XTR",
                                  amount=120, external_id="disc2", redemption_id=d3.redemption_id)
    assert o.status == "error"
    await expect_err(svc.redeem_promo(703, "SALE50"), "обрабатывается")
    remna.fail = False
    assert (await svc.retry_payment(o.payment_id)).status == "ok" and await svc.get_discount(703) is None
    print("✓ скидка на неудавшемся платеже расходуется после /regrant")

    # --- админ: правки, удаление, перебор кодов
    p = await svc.update_promo("welcome7", max_total=10, audience="all", expires_at=None)
    assert p.max_total == 10
    await expect_err(svc.update_promo("welcome7", value=0), "от 1")
    await expect_err(svc.delete_promo("welcome7"), "активац")
    await svc.create_promo("TMP", "days", 1); await svc.delete_promo("tmp")
    await expect_err(svc.create_promo("welcome7", "days", 1), "уже существует")
    await expect_err(svc.create_promo("x", "days", 1), "3–32")
    await expect_err(svc.create_promo("BADPCT", "discount", 100), "от 1 до 99")
    assert any(st.promo.code == "WELCOME7" and st.used == 2 for st in await svc.list_promos())
    await svc.register(800, "brute")
    for i in range(8):
        await expect_err(svc.redeem_promo(800, f"WRONG{i}"), "не найден")
    await expect_err(svc.redeem_promo(800, "WELCOME7"), "Слишком много")
    print("✓ админ-операции и защита от перебора")


async def extra_tests(svc, store, remna, plan, sm):
    from sqlalchemy import func, select
    from app import analytics
    from app.db import Event, User, AdminAudit

    # --- настройки: валидация, применение «на лету», сброс
    errs = await store.update({"trial_days": "0"}, "t"); assert "trial_days" in errs
    errs = await store.update({"welcome_text": "<b>не закрыт"}, "t"); assert "welcome_text" in errs
    assert not await store.update({"trial_days": "7"}, "t") and store.get("trial_days") == 7
    await svc.register(900, "trialist")
    await svc.claim_trial(900)
    u = await svc.get_user(900)
    assert 6.9 < days_left(remna, u.remna_uuid) < 7.1
    print("✓ длительность пробного берётся из настроек и меняется без перезапуска")
    await store.update({"trial_enabled": False}, "t")
    await svc.register(901, "x")
    await expect_err(svc.claim_trial(901), "недоступен")
    await store.reset("trial_enabled"); await store.reset("trial_days")
    assert store.get("trial_days") == 30 and store.get("trial_enabled") is True
    print("✓ отключение пробного и сброс к значению по умолчанию")

    # --- рефералка управляется настройками
    a, _ = await svc.register(910, "ref_a")
    await store.update({"referral_enabled": False}, "t")
    b, _ = await svc.register(911, "ref_b", referrer_id=a.id)
    o = await svc.process_payment(telegram_id=911, plan_id=plan.id, provider="stars", currency="XTR", amount=150, external_id="r-off")
    assert o.status == "ok" and o.referrer_telegram_id is None
    await store.reset("referral_enabled")
    await store.update({"referral_bonus_days": "10", "referral_invited_bonus_days": "5"}, "t")
    c, _ = await svc.register(912, "ref_c", referrer_id=a.id)
    ra = days_left(remna, (await svc.get_user(910)).remna_uuid) if (await svc.get_user(910)).remna_uuid else 0
    o = await svc.process_payment(telegram_id=912, plan_id=plan.id, provider="stars", currency="XTR", amount=150, external_id="r-on")
    assert o.referrer_telegram_id == 910 and o.bonus_days == 10 and o.invited_bonus_days == 5, o
    cu = await svc.get_user(912)
    assert 34.9 < days_left(remna, cu.remna_uuid) < 35.1
    await store.reset("referral_bonus_days"); await store.reset("referral_invited_bonus_days")
    print("✓ рефералка: выключатель, бонус рефереру и приглашённому из настроек")

    # --- источники и события
    async with sm() as s:
        assert (await s.get(User, b.id)).source == "ref" and (await s.get(User, a.id)).source == "organic"
    await svc.create_promo("SRC1", "days", 3, audience="new")
    await svc.register(920, "promo_guy")
    await svc.redeem_promo(920, "SRC1")
    async with sm() as s:
        pu = (await s.execute(select(User).where(User.telegram_id == 920))).scalar_one()
        assert pu.source == "promo" and pu.source_promo_id is not None
        kinds = {r[0] for r in (await s.execute(select(Event.type))).all()}
    assert {"registered", "subscription_granted", "promo_redeemed", "payment_granted"} <= kinds, kinds
    print("✓ источник пользователя и журнал событий пишутся")

    # --- тарифы: правка, удаление, цены
    p2 = await svc.add_plan("тест", 10, 50)
    await svc.update_plan(p2.id, title="тест2", days=11, sort_order=5)
    await svc.set_price(p2.id, 70, "RUB"); await svc.remove_price(p2.id, "RUB")
    await svc.delete_plan(p2.id)
    await expect_err(svc.delete_plan(plan.id), "платеж")
    print("✓ тарифы: правка, цены, удаление (с защитой, если есть платежи)")

    # --- одноразовый вход в веб
    tok = await svc.create_login_token(1)
    assert await svc.consume_login_token(tok) == 1
    assert await svc.consume_login_token(tok) is None
    assert await svc.consume_login_token("junk") is None
    print("✓ токен входа в веб одноразовый")

    # --- синхронизация снимка и аналитика
    n = await svc.sync_subscriptions(pause=0)
    assert n > 0
    ov = await analytics.overview(sm, 30)
    assert ov["users"]["total"] >= 10 and ov["users"]["old"] >= 3 and ov["subs"]["active"] > 0
    assert any(x["source"] == "promo" for x in ov["sources"]) and ov["revenue"]["total"]
    for flt in analytics.USER_FILTERS:
        await analytics.search_users(sm, "", flt, 1)
    assert (await analytics.search_users(sm, "@promo_guy", "all")).total == 1
    card = await analytics.user_card(sm, 920); assert card and card["events"]
    assert (await analytics.list_payments(sm, "all")).total > 0
    assert (await analytics.promo_card(sm, "SRC1"))["stats"]["attributed"] == 1
    await analytics.list_audit(sm); await analytics.plans_with_usage(sm)
    print("✓ аналитика: обзор, фильтры, карточки (включая подсчёт новичков/старичков/источников)")


async def main():
    cfg = Settings(bot_token="x", remnawave_url="http://x", remnawave_token="x", admin_ids="1")
    url = os.environ.get("TEST_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    engine, sm = make_engine(url)
    if url.startswith("sqlite"):
        await init_db(engine)
    else:   # на PostgreSQL проверяем настоящие миграции на чистой БД
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
        await run_migrations(engine, url)
    remna = FakeRemna()
    store = SettingsStore(sm, cfg)
    await store.load()
    svc = VPNService(sm, remna, cfg, store)

    # --- триал: один раз
    a, created = await svc.register(100, "alice"); assert created
    await svc.claim_trial(100)
    try: await svc.claim_trial(100); raise SystemExit("trial twice!")
    except VPNError: pass
    a = await svc.get_user(100)
    assert 29.9 < days_left(remna, a.remna_uuid) < 30.1
    print("✓ триал выдаётся один раз")

    # --- самореферал игнорируется, повторный /start не меняет реферера
    _, c = await svc.register(100, "alice", referrer_id=a.id); assert not c
    assert (await svc.get_user(100)).referrer_id is None
    print("✓ повторный /start не создаёт реферала")

    # --- реферал + платежи
    plan = await svc.add_plan("1 месяц", 30, 150)
    b, _ = await svc.register(200, "bob", referrer_id=a.id)
    assert b.referrer_id == a.id
    before = days_left(remna, a.remna_uuid)

    out = await svc.process_payment(telegram_id=200, plan_id=plan.id, provider="stars", currency="XTR", amount=150, external_id="chg1")
    assert out.status == "ok" and out.referrer_telegram_id == 100 and out.bonus_days == 30
    assert abs(days_left(remna, a.remna_uuid) - (before + 30)) < 0.01
    print("✓ первая покупка приглашённого → +30 дней рефереру")

    out2 = await svc.process_payment(telegram_id=200, plan_id=plan.id, provider="stars", currency="XTR", amount=150, external_id="chg1")
    assert out2.status == "duplicate"
    b = await svc.get_user(200)
    assert 29.9 < days_left(remna, b.remna_uuid) < 30.1  # не продлилось повторно
    print("✓ повтор того же платежа ничего не продлевает")

    out3 = await svc.process_payment(telegram_id=200, plan_id=plan.id, provider="stars", currency="XTR", amount=150, external_id="chg2")
    assert out3.status == "ok" and out3.referrer_telegram_id is None
    assert 59.9 < days_left(remna, b.remna_uuid) < 60.1
    assert abs(days_left(remna, a.remna_uuid) - (before + 30)) < 0.01
    print("✓ вторая покупка продлевает, бонус рефереру второй раз не начисляется")

    # --- гонка: один и тот же платёж одновременно
    c_, _ = await svc.register(300, "carol")
    res = await asyncio.gather(*[
        svc.process_payment(telegram_id=300, plan_id=plan.id, provider="stars", currency="XTR", amount=150, external_id="race")
        for _ in range(5)
    ])
    assert sorted(r.status for r in res).count("ok") == 1, [r.status for r in res]
    c_ = await svc.get_user(300)
    assert 29.9 < days_left(remna, c_.remna_uuid) < 30.1
    print("✓ 5 параллельных одинаковых платежей → выдан ровно один")

    # --- панель недоступна: платёж сохранён, потом regrant
    await svc.register(400, "dave")
    remna.fail = True
    o = await svc.process_payment(telegram_id=400, plan_id=plan.id, provider="stars", currency="XTR", amount=150, external_id="down")
    assert o.status == "error"
    pend = await svc.pending_payments(); assert len(pend) == 1
    remna.fail = False
    o = await svc.retry_payment(pend[0].id); assert o.status == "ok"
    assert not await svc.pending_payments()
    print("✓ при падении панели платёж не теряется, /regrant его выдаёт")

    # --- истёкшая подписка продлевается от «сейчас», а не от прошлого
    d = await svc.get_user(400)
    remna.users[d.remna_uuid].update(expireAt=iso(utcnow() - timedelta(days=10)), status="EXPIRED")
    await svc.process_payment(telegram_id=400, plan_id=plan.id, provider="stars", currency="XTR", amount=150, external_id="renew")
    assert 29.9 < days_left(remna, d.remna_uuid) < 30.1 and remna.users[d.remna_uuid]["status"] == "ACTIVE"
    print("✓ истёкшая подписка продлевается от текущего момента и снова ACTIVE")

    # --- валидация заказа и админ-удаление
    assert await svc.validate_order(100, f"plan:{plan.id}", "XTR", 150) is None
    assert await svc.validate_order(100, f"plan:{plan.id}", "XTR", 1) is not None
    await svc.admin_delete_user(100)
    try: await svc.claim_trial(100); raise SystemExit("trial after delete!")
    except VPNError: pass
    print("✓ pre_checkout сверяет цену; после удаления триал повторно не выдаётся")
    await promo_tests(svc, remna, plan)
    await extra_tests(svc, store, remna, plan, sm)
    await engine.dispose()
    print("\nВсе проверки пройдены")

if __name__ == "__main__":
    asyncio.run(main())

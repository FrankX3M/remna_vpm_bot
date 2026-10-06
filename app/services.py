"""
Вся бизнес-логика. Здесь НЕТ ничего от aiogram — этот слой переиспользуется веб-версией (FastAPI и т.п.).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from .config import Settings
from .db import Payment, Plan, PlanPrice, PromoCode, PromoRedemption, User, utcnow
from .remnawave import RemnawaveClient
from .utils import ensure_utc, iso, parse_dt

log = logging.getLogger(__name__)


class VPNError(Exception):
    """Ошибка с текстом, который безопасно показать пользователю."""


@dataclass
class Account:
    user: User
    remna: dict | None
    last_payment: Payment | None
    payments_count: int
    invited: int
    rewarded: int


@dataclass
class PaymentOutcome:
    status: Literal["ok", "duplicate", "error"]
    payment_id: int | None = None
    expire_at: datetime | None = None
    # заполняется, если за эту покупку начислен реферальный бонус
    referrer_telegram_id: int | None = None
    referrer_expire_at: datetime | None = None
    bonus_days: int = 0


PROMO_KINDS = ("days", "discount", "trial")
PROMO_AUDIENCES = ("all", "new", "old")
_CODE_RE = re.compile(r"^[\w-]{3,32}$")
PROMO_MAX_FAILS = 8          # неудачных вводов...
PROMO_FAIL_WINDOW = 600.0    # ...за столько секунд — и пользователь временно блокируется (защита от перебора)


def normalize_code(raw: str) -> str:
    code = (raw or "").strip().upper()
    if not _CODE_RE.match(code):
        raise VPNError("Промокод: 3–32 символа — буквы, цифры, «_» и «-».")
    return code


def apply_discount(amount: int, percent: int) -> int:
    """Цена со скидкой, округление вверх, минимум 1 (Stars не принимают 0)."""
    return max(1, -(-amount * (100 - percent) // 100))


@dataclass
class PromoResult:
    kind: str
    value: int
    code: str
    expire_at: datetime | None = None   # для days / trial


@dataclass
class Discount:
    redemption_id: int
    percent: int
    code: str


@dataclass
class PromoStat:
    promo: PromoCode
    used: int


class VPNService:
    def __init__(self, sessionmaker: async_sessionmaker, remna: RemnawaveClient, settings: Settings):
        self.sm = sessionmaker
        self.remna = remna
        self.cfg = settings
        # Блокировки на пользователя: два платежа/действия одного человека не пересекутся.
        # Работает в пределах одного процесса бота.
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        # Порядок блокировок: сначала _promo_lock (короткая, без сети), потом блокировка пользователя.
        self._promo_lock = asyncio.Lock()
        self._promo_fails: dict[int, deque] = defaultdict(deque)

    # ------------------------------------------------------------------ пользователи

    async def get_user(self, telegram_id: int) -> User | None:
        async with self.sm() as s:
            return (await s.execute(select(User).where(User.telegram_id == telegram_id))).scalar_one_or_none()

    async def _require_user(self, telegram_id: int) -> User:
        user = await self.get_user(telegram_id)
        if user is None:
            raise VPNError("Пользователь не найден. Нажмите /start.")
        return user

    async def register(self, telegram_id: int, tg_username: str | None, referrer_id: int | None = None) -> tuple[User, bool]:
        """Создаёт пользователя при первом /start. Реферер учитывается только для НОВЫХ пользователей."""
        async with self.sm() as s:
            user = (await s.execute(select(User).where(User.telegram_id == telegram_id))).scalar_one_or_none()
            if user:
                if user.tg_username != tg_username:
                    user.tg_username = tg_username
                    await s.commit()
                return user, False

            valid_ref = None
            if referrer_id:
                ref = await s.get(User, referrer_id)
                if ref and ref.telegram_id != telegram_id:
                    valid_ref = ref.id

            user = User(telegram_id=telegram_id, tg_username=tg_username, referrer_id=valid_ref)
            s.add(user)
            try:
                await s.commit()
            except IntegrityError:  # гонка двух /start
                await s.rollback()
                user = (await s.execute(select(User).where(User.telegram_id == telegram_id))).scalar_one()
                return user, False
            return user, True

    async def get_account(self, telegram_id: int) -> Account:
        user = await self._require_user(telegram_id)
        ru = await self.remna.get_user(user.remna_uuid) if user.remna_uuid else None
        async with self.sm() as s:
            last = (
                await s.execute(
                    select(Payment)
                    .where(Payment.user_id == user.id, Payment.granted.is_(True))
                    .order_by(Payment.created_at.desc(), Payment.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            pays = (
                await s.execute(
                    select(func.count()).select_from(Payment).where(Payment.user_id == user.id, Payment.granted.is_(True))
                )
            ).scalar_one()
            invited = (
                await s.execute(select(func.count()).select_from(User).where(User.referrer_id == user.id))
            ).scalar_one()
            rewarded = (
                await s.execute(
                    select(func.count())
                    .select_from(User)
                    .where(User.referrer_id == user.id, User.referral_rewarded.is_(True))
                )
            ).scalar_one()
        return Account(user, ru, last, pays, invited, rewarded)

    def sub_url(self, ru: dict | None) -> str | None:
        if not ru:
            return None
        if ru.get("subscriptionUrl"):
            return ru["subscriptionUrl"]
        if self.cfg.sub_base_url and ru.get("shortUuid"):
            return f"{self.cfg.sub_base_url.rstrip('/')}/{ru['shortUuid']}"
        return None

    # ------------------------------------------------------------------ выдача дней

    async def _grant_unlocked(self, user_id: int, days: int) -> datetime:
        """Продлевает подписку на N дней (от текущего конца, если она ещё активна). Вызывать под локом."""
        async with self.sm() as s:
            user = await s.get(User, user_id)
        if user is None or user.telegram_id is None:
            raise VPNError("Пользователь не найден.")

        username = f"{self.cfg.username_prefix}{user.telegram_id}"
        now = utcnow()

        ru = await self.remna.get_user(user.remna_uuid) if user.remna_uuid else None
        if ru is None:
            # на случай, если локальная БД пересоздана, а пользователь в панели остался
            ru = await self.remna.find_by_username(username)

        if ru is None:
            new_exp = now + timedelta(days=days)
            limit_gb = self.cfg.traffic_limit_gb
            ru = await self.remna.create_user(
                username=username,
                expire_at=new_exp,
                telegram_id=user.telegram_id,
                traffic_limit_bytes=limit_gb * 1024**3 if limit_gb else 0,
                traffic_strategy="MONTH" if limit_gb else "NO_RESET",
                squads=self.cfg.squad_list,
                hwid_limit=self.cfg.hwid_limit or None,
            )
        else:
            current = parse_dt(ru.get("expireAt"))
            base = max(now, current) if current else now
            new_exp = base + timedelta(days=days)
            fields: dict = {"expireAt": iso(new_exp)}
            if ru.get("status") == "EXPIRED":
                fields["status"] = "ACTIVE"
            await self.remna.update_user(ru["uuid"], **fields)

        if user.remna_uuid != ru["uuid"]:
            async with self.sm() as s:
                await s.execute(update(User).where(User.id == user_id).values(remna_uuid=ru["uuid"]))
                await s.commit()
        return new_exp

    async def grant_days(self, user_id: int, days: int) -> datetime:
        async with self._locks[user_id]:
            return await self._grant_unlocked(user_id, days)

    # ------------------------------------------------------------------ пробный период

    async def claim_trial(self, telegram_id: int) -> datetime:
        user = await self._require_user(telegram_id)
        async with self._locks[user.id]:
            async with self.sm() as s:
                paid = (
                    await s.execute(
                        select(func.count()).select_from(Payment).where(Payment.user_id == user.id, Payment.granted.is_(True))
                    )
                ).scalar_one()
                if paid:
                    raise VPNError("Пробный период доступен только до первой покупки.")
                # атомарно: флаг ставится только один раз, даже при двойном клике
                res = await s.execute(
                    update(User).where(User.id == user.id, User.trial_used.is_(False)).values(trial_used=True)
                )
                await s.commit()
                if res.rowcount == 0:
                    raise VPNError("Пробный период уже использован.")
            try:
                return await self._grant_unlocked(user.id, self.cfg.trial_days)
            except Exception:
                async with self.sm() as s:  # откатываем флаг, чтобы человек мог повторить
                    await s.execute(update(User).where(User.id == user.id).values(trial_used=False))
                    await s.commit()
                raise

    # ------------------------------------------------------------------ ссылка подписки

    async def revoke_link(self, telegram_id: int) -> str | None:
        user = await self._require_user(telegram_id)
        if not user.remna_uuid:
            raise VPNError("У вас ещё нет подписки.")
        async with self._locks[user.id]:
            await self.remna.revoke_subscription(user.remna_uuid)
            ru = await self.remna.get_user(user.remna_uuid)
        return self.sub_url(ru)

    # ------------------------------------------------------------------ тарифы

    async def list_plans(self, *, active_only: bool = True) -> list[Plan]:
        async with self.sm() as s:
            stmt = select(Plan).order_by(Plan.sort_order, Plan.days, Plan.id)
            if active_only:
                stmt = stmt.where(Plan.is_active.is_(True))
            return list((await s.execute(stmt)).scalars().all())

    async def get_plan(self, plan_id: int) -> Plan | None:
        async with self.sm() as s:
            return await s.get(Plan, plan_id)

    async def add_plan(self, title: str, days: int, amount: int, currency: str = "XTR") -> Plan:
        if days <= 0 or amount <= 0:
            raise VPNError("Дни и цена должны быть больше нуля.")
        async with self.sm() as s:
            plan = Plan(title=title[:64], days=days, sort_order=days)
            plan.prices.append(PlanPrice(currency=currency, amount=amount))
            s.add(plan)
            await s.commit()
            return plan

    async def set_price(self, plan_id: int, amount: int, currency: str = "XTR") -> None:
        if amount <= 0:
            raise VPNError("Цена должна быть больше нуля.")
        async with self.sm() as s:
            plan = await s.get(Plan, plan_id)
            if plan is None:
                raise VPNError("Тариф не найден.")
            for p in plan.prices:
                if p.currency == currency:
                    p.amount = amount
                    break
            else:
                plan.prices.append(PlanPrice(currency=currency, amount=amount))
            await s.commit()

    async def toggle_plan(self, plan_id: int) -> bool:
        async with self.sm() as s:
            plan = await s.get(Plan, plan_id)
            if plan is None:
                raise VPNError("Тариф не найден.")
            plan.is_active = not plan.is_active
            await s.commit()
            return plan.is_active

    # ------------------------------------------------------------------ платежи

    @staticmethod
    def make_payload(plan_id: int, redemption_id: int | None = None) -> str:
        return f"plan:{plan_id}" + (f":rd:{redemption_id}" if redemption_id else "")

    @staticmethod
    def parse_order(payload: str) -> tuple[int, int | None]:
        """payload -> (plan_id, redemption_id скидки или None). Формат: plan:ID или plan:ID:rd:RID."""
        try:
            parts = payload.split(":")
            if parts[0] != "plan" or len(parts) not in (2, 4):
                raise ValueError
            if len(parts) == 4 and parts[2] != "rd":
                raise ValueError
            return int(parts[1]), (int(parts[3]) if len(parts) == 4 else None)
        except ValueError:
            raise VPNError("Некорректный заказ.") from None

    @classmethod
    def parse_payload(cls, payload: str) -> int:
        return cls.parse_order(payload)[0]

    async def validate_order(self, telegram_id: int, payload: str, currency: str, amount: int) -> str | None:
        """Проверка перед списанием (pre_checkout). Возвращает текст ошибки или None."""
        if await self.get_user(telegram_id) is None:
            return "Сначала нажмите /start."
        try:
            plan_id, rid = self.parse_order(payload)
        except VPNError as e:
            return str(e)
        plan = await self.get_plan(plan_id)
        if plan is None or not plan.is_active:
            return "Этот тариф больше недоступен."
        expected = plan.price(currency)
        if expected is not None and rid:
            disc = await self.get_discount(telegram_id)
            if disc is None or disc.redemption_id != rid:
                return "Скидка больше недоступна. Откройте меню и выберите тариф заново."
            expected = apply_discount(expected, disc.percent)
        if expected != amount:
            return "Цена изменилась. Откройте меню и выберите тариф заново."
        return None

    async def process_payment(
        self, *, telegram_id: int, plan_id: int, provider: str, currency: str, amount: int, external_id: str,
        redemption_id: int | None = None,
    ) -> PaymentOutcome:
        """
        Единая точка входа для ВСЕХ провайдеров (Stars, вебхуки карт/крипты, веб-версия).
        Идемпотентна: повторный вызов с теми же (provider, external_id) ничего не продлевает.
        """
        user = await self._require_user(telegram_id)
        async with self.sm() as s:
            plan = await s.get(Plan, plan_id)
            if plan is None:
                raise VPNError("Тариф не найден.")
            existing = (
                await s.execute(select(Payment).where(Payment.provider == provider, Payment.external_id == external_id))
            ).scalar_one_or_none()
            if existing is None:
                pay = Payment(
                    user_id=user.id, plan_id=plan.id, provider=provider, currency=currency,
                    amount=amount, days=plan.days, external_id=external_id, granted=False,
                )
                s.add(pay)
                try:
                    await s.commit()
                except IntegrityError:  # параллельный дубль
                    await s.rollback()
                    return PaymentOutcome("duplicate")
                pay_id = pay.id
                if redemption_id:  # привязываем использованную скидку; «used» она станет, когда подписка выдана
                    await s.execute(
                        update(PromoRedemption)
                        .where(
                            PromoRedemption.id == redemption_id, PromoRedemption.user_id == user.id,
                            PromoRedemption.status == "pending", PromoRedemption.payment_id.is_(None),
                        )
                        .values(payment_id=pay_id)
                    )
                    await s.commit()
            else:
                pay_id = existing.id
        return await self._finalize(pay_id)

    async def retry_payment(self, payment_id: int) -> PaymentOutcome:
        return await self._finalize(payment_id)

    async def _finalize(self, pay_id: int) -> PaymentOutcome:
        async with self.sm() as s:
            pay = await s.get(Payment, pay_id)
            if pay is None:
                raise VPNError("Платёж не найден.")
            user_id, days = pay.user_id, pay.days

        async with self._locks[user_id]:
            async with self.sm() as s:
                pay = await s.get(Payment, pay_id)
                if pay.granted:
                    return PaymentOutcome("duplicate", pay_id)
            try:
                expire_at = await self._grant_unlocked(user_id, days)
            except Exception:
                # деньги получены, но панель недоступна: платёж остаётся granted=False → /pending и /regrant
                log.exception("Не удалось выдать подписку по платежу %s", pay_id)
                return PaymentOutcome("error", pay_id)
            async with self.sm() as s:
                await s.execute(update(Payment).where(Payment.id == pay_id).values(granted=True))
                await s.execute(
                    update(PromoRedemption)
                    .where(PromoRedemption.payment_id == pay_id, PromoRedemption.status == "pending")
                    .values(status="used")
                )
                await s.commit()

        outcome = PaymentOutcome("ok", pay_id, expire_at)
        await self._reward_referrer(user_id, outcome)
        return outcome

    async def _reward_referrer(self, user_id: int, outcome: PaymentOutcome) -> None:
        """Бонус пригласившему — только за ПЕРВУЮ покупку приглашённого."""
        async with self.sm() as s:
            res = await s.execute(
                update(User)
                .where(User.id == user_id, User.referrer_id.is_not(None), User.referral_rewarded.is_(False))
                .values(referral_rewarded=True)
            )
            await s.commit()
            if res.rowcount != 1:
                return
            user = await s.get(User, user_id)
            referrer = await s.get(User, user.referrer_id)
        try:
            exp = await self.grant_days(referrer.id, self.cfg.referral_bonus_days)
        except Exception:
            log.exception("Не удалось начислить реферальный бонус (referrer=%s)", referrer.id)
            async with self.sm() as s:  # вернём флаг: бонус начислится при следующей покупке приглашённого
                await s.execute(update(User).where(User.id == user_id).values(referral_rewarded=False))
                await s.commit()
            return
        outcome.referrer_telegram_id = referrer.telegram_id
        outcome.referrer_expire_at = exp
        outcome.bonus_days = self.cfg.referral_bonus_days

    async def pending_payments(self) -> list[Payment]:
        async with self.sm() as s:
            return list(
                (await s.execute(select(Payment).where(Payment.granted.is_(False)).order_by(Payment.id))).scalars().all()
            )

    # ------------------------------------------------------------------ промокоды (пользователь)

    def _check_promo_attempts(self, user_id: int) -> None:
        q = self._promo_fails[user_id]
        now = time.monotonic()
        while q and now - q[0] > PROMO_FAIL_WINDOW:
            q.popleft()
        if len(q) >= PROMO_MAX_FAILS:
            wait = int((PROMO_FAIL_WINDOW - (now - q[0])) // 60) + 1
            raise VPNError(f"Слишком много неверных попыток. Повторите через {wait} мин.")

    async def redeem_promo(self, telegram_id: int, raw_code: str) -> PromoResult:
        user = await self._require_user(telegram_id)
        self._check_promo_attempts(user.id)
        try:
            return await self._redeem(user, normalize_code(raw_code))
        except VPNError:
            self._promo_fails[user.id].append(time.monotonic())
            raise

    async def _redeem(self, user: User, code: str) -> PromoResult:
        # Проверка лимитов и создание активации — под общей блокировкой, чтобы последние места не раздали дважды.
        async with self._promo_lock:
            async with self.sm() as s:
                promo = (await s.execute(select(PromoCode).where(PromoCode.code == code))).scalar_one_or_none()
                exp = ensure_utc(promo.expires_at) if promo else None
                if promo is None or not promo.is_active or (exp and exp <= utcnow()):
                    raise VPNError("Промокод не найден или срок его действия истёк.")

                total = (
                    await s.execute(select(func.count()).select_from(PromoRedemption).where(PromoRedemption.promo_id == promo.id))
                ).scalar_one()
                if promo.max_total and total >= promo.max_total:
                    raise VPNError("Лимит активаций этого промокода исчерпан.")

                pending = list(
                    (
                        await s.execute(
                            select(PromoRedemption).where(PromoRedemption.user_id == user.id, PromoRedemption.status == "pending")
                        )
                    ).scalars().all()
                )
                if promo.kind == "discount" and any(r.promo_id == promo.id for r in pending):
                    raise VPNError("Эта скидка уже активирована. Откройте «Купить / продлить» — цены пересчитаны.")

                mine = (
                    await s.execute(
                        select(func.count()).select_from(PromoRedemption)
                        .where(PromoRedemption.promo_id == promo.id, PromoRedemption.user_id == user.id)
                    )
                ).scalar_one()
                if mine >= max(promo.per_user, 1):
                    raise VPNError("Вы уже использовали этот промокод.")

                paid = (
                    await s.execute(
                        select(func.count()).select_from(Payment).where(Payment.user_id == user.id, Payment.granted.is_(True))
                    )
                ).scalar_one()
                if promo.audience == "new" and paid:
                    raise VPNError("Этот промокод только для новых пользователей (до первой покупки).")
                if promo.audience == "old" and not paid:
                    raise VPNError("Этот промокод только для тех, кто уже покупал подписку.")

                trial_flag_set = False
                if promo.kind == "trial":
                    if paid:
                        raise VPNError("Пробный период доступен только до первой покупки.")
                    res = await s.execute(
                        update(User).where(User.id == user.id, User.trial_used.is_(False)).values(trial_used=True)
                    )
                    if res.rowcount == 0:
                        raise VPNError("Пробный период уже использован.")
                    trial_flag_set = True

                if promo.kind == "discount":
                    for r in pending:  # прежняя неиспользованная скидка заменяется новой
                        if r.payment_id is not None:
                            raise VPNError("Сейчас обрабатывается ваша оплата. Дождитесь её завершения и повторите.")
                    if pending:
                        await s.execute(delete(PromoRedemption).where(PromoRedemption.id.in_([r.id for r in pending])))

                red = PromoRedemption(
                    promo_id=promo.id, user_id=user.id, status="pending" if promo.kind == "discount" else "used"
                )
                s.add(red)
                await s.commit()
                kind, value, rid = promo.kind, promo.value, red.id

        if kind == "discount":
            return PromoResult(kind, value, code)

        try:
            async with self._locks[user.id]:
                expire_at = await self._grant_unlocked(user.id, value)
        except Exception:
            async with self.sm() as s:  # выдать не удалось (панель недоступна): активацию и флаг триала возвращаем
                await s.execute(delete(PromoRedemption).where(PromoRedemption.id == rid))
                if trial_flag_set:
                    await s.execute(update(User).where(User.id == user.id).values(trial_used=False))
                await s.commit()
            raise
        return PromoResult(kind, value, code, expire_at)

    async def get_discount(self, telegram_id: int) -> Discount | None:
        """Активная (неиспользованная) скидка пользователя; недействующий уже промокод скидку не даёт."""
        user = await self.get_user(telegram_id)
        if user is None:
            return None
        async with self.sm() as s:
            row = (
                await s.execute(
                    select(PromoRedemption, PromoCode)
                    .join(PromoCode, PromoCode.id == PromoRedemption.promo_id)
                    .where(
                        PromoRedemption.user_id == user.id, PromoRedemption.status == "pending",
                        PromoCode.kind == "discount", PromoCode.is_active.is_(True),
                    )
                    .order_by(PromoRedemption.id.desc())
                    .limit(1)
                )
            ).first()
        if row is None:
            return None
        red, promo = row
        exp = ensure_utc(promo.expires_at)
        if exp and exp <= utcnow():
            return None
        return Discount(red.id, promo.value, promo.code)

    async def cancel_discount(self, telegram_id: int) -> bool:
        """Отказаться от неиспользованной скидки (активация освобождается и снова может быть использована)."""
        user = await self._require_user(telegram_id)
        async with self._promo_lock:
            async with self.sm() as s:
                res = await s.execute(
                    delete(PromoRedemption).where(
                        PromoRedemption.user_id == user.id, PromoRedemption.status == "pending",
                        PromoRedemption.payment_id.is_(None),
                    )
                )
                await s.commit()
        return res.rowcount > 0

    # ------------------------------------------------------------------ промокоды (админ)

    @staticmethod
    def _validate_promo(kind: str, value: int, audience: str, max_total: int, per_user: int) -> None:
        if kind not in PROMO_KINDS:
            raise VPNError("Тип: days (дни), discount (скидка %) или trial (пробный период).")
        limits = {"days": (1, 3650, "Дни"), "trial": (1, 365, "Дни пробного периода"), "discount": (1, 99, "Скидка, %")}
        lo, hi, what = limits[kind]
        if not lo <= value <= hi:
            raise VPNError(f"{what}: от {lo} до {hi}.")
        if audience not in PROMO_AUDIENCES:
            raise VPNError("Аудитория: all (все), new (новички — ещё не покупали), old (старички — уже покупали).")
        if max_total < 0:
            raise VPNError("Лимит активаций: 0 (без лимита) или больше.")
        if per_user < 1:
            raise VPNError("Активаций на человека: минимум 1.")

    async def create_promo(
        self, code: str, kind: str, value: int, *, audience: str = "all", max_total: int = 0,
        per_user: int = 1, expires_at: datetime | None = None,
    ) -> PromoCode:
        code = normalize_code(code)
        self._validate_promo(kind, value, audience, max_total, per_user)
        async with self.sm() as s:
            promo = PromoCode(
                code=code, kind=kind, value=value, audience=audience, max_total=max_total,
                per_user=per_user, expires_at=expires_at,
            )
            s.add(promo)
            try:
                await s.commit()
            except IntegrityError:
                await s.rollback()
                raise VPNError(f"Промокод {code} уже существует.") from None
            return promo

    async def _promo_by_code(self, s, code: str) -> PromoCode:
        promo = (await s.execute(select(PromoCode).where(PromoCode.code == normalize_code(code)))).scalar_one_or_none()
        if promo is None:
            raise VPNError("Промокод не найден.")
        return promo

    async def get_promo(self, code: str) -> PromoStat:
        async with self.sm() as s:
            promo = await self._promo_by_code(s, code)
            used = (
                await s.execute(select(func.count()).select_from(PromoRedemption).where(PromoRedemption.promo_id == promo.id))
            ).scalar_one()
            return PromoStat(promo, used)

    async def list_promos(self) -> list[PromoStat]:
        async with self.sm() as s:
            rows = (
                await s.execute(
                    select(PromoCode, func.count(PromoRedemption.id))
                    .outerjoin(PromoRedemption, PromoRedemption.promo_id == PromoCode.id)
                    .group_by(PromoCode.id)
                    .order_by(PromoCode.id)
                )
            ).all()
        return [PromoStat(p, n) for p, n in rows]

    async def promo_redemptions(self, code: str, limit: int = 10) -> list[tuple[PromoRedemption, int | None]]:
        async with self.sm() as s:
            promo = await self._promo_by_code(s, code)
            rows = (
                await s.execute(
                    select(PromoRedemption, User.telegram_id)
                    .join(User, User.id == PromoRedemption.user_id)
                    .where(PromoRedemption.promo_id == promo.id)
                    .order_by(PromoRedemption.id.desc())
                    .limit(limit)
                )
            ).all()
        return [(r, tg) for r, tg in rows]

    async def update_promo(self, code: str, **fields) -> PromoCode:
        """Допустимые поля: value, audience, max_total, per_user, expires_at (None = бессрочно)."""
        allowed = {"value", "audience", "max_total", "per_user", "expires_at"}
        if not fields or set(fields) - allowed:
            raise VPNError("Можно менять: value, for, limit, per_user, until.")
        async with self._promo_lock:
            async with self.sm() as s:
                promo = await self._promo_by_code(s, code)
                data = {k: getattr(promo, k) for k in allowed} | fields
                self._validate_promo(promo.kind, data["value"], data["audience"], data["max_total"], data["per_user"])
                for k, v in fields.items():
                    setattr(promo, k, v)
                await s.commit()
                return promo

    async def toggle_promo(self, code: str) -> bool:
        async with self._promo_lock:
            async with self.sm() as s:
                promo = await self._promo_by_code(s, code)
                promo.is_active = not promo.is_active
                await s.commit()
                return promo.is_active

    async def delete_promo(self, code: str) -> None:
        """Удаляет только промокод без единой активации (иначе теряется история) — для остальных есть отключение."""
        async with self._promo_lock:
            async with self.sm() as s:
                promo = await self._promo_by_code(s, code)
                used = (
                    await s.execute(select(func.count()).select_from(PromoRedemption).where(PromoRedemption.promo_id == promo.id))
                ).scalar_one()
                if used:
                    raise VPNError(f"У промокода уже {used} активаций — удалять нельзя, отключите: /togglepromo {promo.code}")
                await s.delete(promo)
                await s.commit()

    # ------------------------------------------------------------------ админ

    async def admin_create_or_extend(self, telegram_id: int, days: int, *, must_exist: bool) -> datetime:
        if days <= 0:
            raise VPNError("Количество дней должно быть больше нуля.")
        if must_exist:
            user = await self._require_user(telegram_id)
        else:
            user, _ = await self.register(telegram_id, None)
        return await self.grant_days(user.id, days)

    async def admin_delete_user(self, telegram_id: int) -> None:
        """Удаляет пользователя в панели. Локальная запись остаётся (trial_used, рефералы, платежи)."""
        user = await self._require_user(telegram_id)
        async with self._locks[user.id]:
            if user.remna_uuid:
                await self.remna.delete_user(user.remna_uuid)
            async with self.sm() as s:
                await s.execute(update(User).where(User.id == user.id).values(remna_uuid=None))
                await s.commit()

    async def stats(self) -> dict:
        async with self.sm() as s:
            users = (await s.execute(select(func.count()).select_from(User))).scalar_one()
            with_sub = (
                await s.execute(select(func.count()).select_from(User).where(User.remna_uuid.is_not(None)))
            ).scalar_one()
            rows = (
                await s.execute(
                    select(Payment.currency, func.count(), func.sum(Payment.amount))
                    .where(Payment.granted.is_(True))
                    .group_by(Payment.currency)
                )
            ).all()
        return {"users": users, "with_sub": with_sub, "revenue": [(c, n, int(t or 0)) for c, n, t in rows]}

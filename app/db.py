from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, false
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# JSON на SQLite (тесты) и JSONB на PostgreSQL (прод)
JsonType = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


class User(Base):
    """Наш пользователь. Для веба позже добавятся email/пароль — telegram_id уже необязателен."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, index=True)
    tg_username: Mapped[str | None] = mapped_column(String(64))
    remna_uuid: Mapped[str | None] = mapped_column(String(64))
    trial_used: Mapped[bool] = mapped_column(Boolean, default=False)
    referrer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    referral_rewarded: Mapped[bool] = mapped_column(Boolean, default=False)
    # Откуда пришёл: organic | ref (реф-ссылка) | promo (первое касание — промокод до первой покупки)
    source: Mapped[str] = mapped_column(String(16), default="organic", server_default="organic")
    source_promo_id: Mapped[int | None] = mapped_column(ForeignKey("promo_codes.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(64))
    days: Mapped[int] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    prices: Mapped[list["PlanPrice"]] = relationship(lazy="selectin", cascade="all, delete-orphan")

    def price(self, currency: str) -> int | None:
        for p in self.prices:
            if p.currency == currency:
                return p.amount
        return None


class PlanPrice(Base):
    """Цена тарифа в конкретной валюте: XTR (Stars), RUB, USDT... Новый способ оплаты = новая валюта."""

    __tablename__ = "plan_prices"
    __table_args__ = (UniqueConstraint("plan_id", "currency"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"))
    currency: Mapped[str] = mapped_column(String(8))
    amount: Mapped[int] = mapped_column(Integer)


class Payment(Base):
    __tablename__ = "payments"
    # Идемпотентность: один и тот же платёж провайдера не может попасть в базу дважды.
    __table_args__ = (UniqueConstraint("provider", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id"))
    provider: Mapped[str] = mapped_column(String(32))
    currency: Mapped[str] = mapped_column(String(8))
    amount: Mapped[int] = mapped_column(Integer)
    days: Mapped[int] = mapped_column(Integer)
    external_id: Mapped[str] = mapped_column(String(255))
    granted: Mapped[bool] = mapped_column(Boolean, default=False)  # подписка реально выдана
    list_amount: Mapped[int | None] = mapped_column(Integer)           # цена тарифа до скидки (на момент оплаты)
    discount_percent: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    promo_id: Mapped[int | None] = mapped_column(ForeignKey("promo_codes.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TrybitOrder(Base):
    """Счёт Trybit в ожидании оплаты. Payment появляется только после подтверждения (process_payment), а этот
    заказ хранит всё, что нужно для выдачи: пользователя, тариф, скидку, сумму и привязку к счёту у провайдера."""

    __tablename__ = "trybit_orders"
    __table_args__ = (Index("ix_trybit_orders_status", "status", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)          # = order_id в Trybit
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"))
    redemption_id: Mapped[int | None] = mapped_column(Integer)
    amount: Mapped[int] = mapped_column(Integer)                # со скидкой, в `currency`
    currency: Mapped[str] = mapped_column(String(8))
    invoice_uuid: Mapped[str | None] = mapped_column(String(32), unique=True)   # INV-XXXXXXXX
    link: Mapped[str | None] = mapped_column(String(255))
    # pending | paid | canceled (истёк/отменён) | error (оплачен, но не обработан — разбирает админ)
    status: Mapped[str] = mapped_column(String(12), default="pending")
    partial_notified: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PromoCode(Base):
    """Промокод. kind: days (бесплатные дни) | discount (скидка %, на следующую покупку) | trial (пробный период, N дней)."""

    __tablename__ = "promo_codes"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)  # всегда в верхнем регистре
    kind: Mapped[str] = mapped_column(String(16))
    value: Mapped[int] = mapped_column(Integer)
    audience: Mapped[str] = mapped_column(String(8), default="all")  # all | new (ещё не платили) | old (платили)
    max_total: Mapped[int] = mapped_column(Integer, default=0)  # 0 = без лимита
    per_user: Mapped[int] = mapped_column(Integer, default=1)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PromoRedemption(Base):
    """Активация промокода. Скидка живёт в статусе pending до покупки, дни/триал сразу used."""

    __tablename__ = "promo_redemptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    promo_id: Mapped[int] = mapped_column(ForeignKey("promo_codes.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String(8), default="used")  # pending | used
    payment_id: Mapped[int | None] = mapped_column(ForeignKey("payments.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Event(Base):
    """Журнал событий (append-only): источник правды для аналитики."""

    __tablename__ = "events"
    __table_args__ = (Index("ix_events_type_ts", "type", "ts"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    # registered | subscription_granted | promo_redeemed | discount_cancelled | payment_granted | payment_failed
    # | link_revoked | user_deleted_panel | trial_started | referral_bonus ...
    type: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(String(24))      # для subscription_granted: trial|payment|promo|referral|admin
    days: Mapped[int | None] = mapped_column(Integer)
    expire_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    promo_id: Mapped[int | None] = mapped_column(ForeignKey("promo_codes.id"))
    payment_id: Mapped[int | None] = mapped_column(ForeignKey("payments.id"))
    actor: Mapped[str] = mapped_column(String(32), default="system")   # system | user | admin:<tg_id>
    data: Mapped[dict | None] = mapped_column(JsonType)


class SubscriptionState(Base):
    """Локальный снимок срока подписки (истина — в панели; обновляется при выдаче и фоновой синхронизацией)."""

    __tablename__ = "subscription_state"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    expire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str | None] = mapped_column(String(16))
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SettingRow(Base):
    """Настройки, которые админ меняет в веб-панели. Нет строки — действует значение по умолчанию."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[object] = mapped_column(JsonType)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_by: Mapped[str | None] = mapped_column(String(32))


class AdminAudit(Base):
    """Кто из админов что менял (веб-панель и команды бота)."""

    __tablename__ = "admin_audit"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    admin_tg_id: Mapped[int | None] = mapped_column(BigInteger)
    via: Mapped[str] = mapped_column(String(8), default="web")   # web | bot
    action: Mapped[str] = mapped_column(String(48))
    target: Mapped[str | None] = mapped_column(String(128))
    details: Mapped[dict | None] = mapped_column(JsonType)


class WebLoginToken(Base):
    """Одноразовые ссылки входа в веб-панель (хранится только хеш)."""

    __tablename__ = "web_login_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    admin_tg_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


def make_engine(url: str):
    kwargs = {} if url.startswith("sqlite") else {"pool_pre_ping": True, "pool_size": 5, "max_overflow": 5}
    engine = create_async_engine(url, **kwargs)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def init_db(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

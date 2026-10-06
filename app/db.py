from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


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


def make_engine(url: str):
    engine = create_async_engine(url)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def init_db(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

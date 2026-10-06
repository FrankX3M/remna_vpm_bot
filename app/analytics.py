"""
Чтение для админ-панели: аналитика, списки, карточки. Только SELECT — бизнес-операции живут в services.py.
Запросы написаны на SQLAlchemy и работают и на PostgreSQL (прод), и на SQLite (тесты).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import Integer, and_, cast, distinct, func, or_, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from .db import (
    AdminAudit, Event, Payment, Plan, PromoCode, PromoRedemption, SubscriptionState, User, utcnow,
)
from .utils import ensure_utc

PAGE_SIZE = 25


def _granted():
    return Payment.granted.is_(True)


@dataclass
class Page:
    items: list
    page: int
    per_page: int
    total: int

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.per_page))


# ---------------------------------------------------------------------------------------------- дашборд

async def overview(sm: async_sessionmaker, days: int = 30) -> dict:
    now = utcnow()
    since = now - timedelta(days=days)
    out: dict = {"now": now, "days": days}
    async with sm() as s:
        users = (await s.execute(select(func.count()).select_from(User))).scalar_one()
        paying = (
            await s.execute(select(func.count(distinct(Payment.user_id))).where(_granted()))
        ).scalar_one()
        trial_users = (
            await s.execute(select(func.count()).select_from(User).where(User.trial_used.is_(True)))
        ).scalar_one()
        trial_paid = (
            await s.execute(
                select(func.count(distinct(Payment.user_id)))
                .join(User, User.id == Payment.user_id)
                .where(_granted(), User.trial_used.is_(True))
            )
        ).scalar_one()
        repeat = (
            await s.execute(
                select(func.count()).select_from(
                    select(Payment.user_id).where(_granted()).group_by(Payment.user_id).having(func.count() > 1).subquery()
                )
            )
        ).scalar_one()
        new_30 = (
            await s.execute(select(func.count()).select_from(User).where(User.created_at >= since))
        ).scalar_one()
        out["users"] = {
            "total": users, "new": users - paying, "old": paying, "repeat": repeat,
            "trial_used": trial_users, "trial_paid": trial_paid, "registered_period": new_30,
        }

        # --- источники
        src_rows = (
            await s.execute(select(User.source, func.count()).group_by(User.source))
        ).all()
        src_paid = dict(
            (
                await s.execute(
                    select(User.source, func.count(distinct(Payment.user_id)))
                    .join(User, User.id == Payment.user_id)
                    .where(_granted())
                    .group_by(User.source)
                )
            ).all()
        )
        out["sources"] = [
            {"source": src, "users": n, "paid": src_paid.get(src, 0), "conv": round(100 * src_paid.get(src, 0) / n, 1) if n else 0}
            for src, n in sorted(src_rows)
        ]

        # --- подписки по локальному снимку
        active = (
            await s.execute(select(func.count()).select_from(SubscriptionState).where(SubscriptionState.expire_at > now))
        ).scalar_one()
        soon = (
            await s.execute(
                select(func.count()).select_from(SubscriptionState)
                .where(SubscriptionState.expire_at > now, SubscriptionState.expire_at <= now + timedelta(days=7))
            )
        ).scalar_one()
        expired = (
            await s.execute(select(func.count()).select_from(SubscriptionState).where(SubscriptionState.expire_at <= now))
        ).scalar_one()
        no_state = (
            await s.execute(
                select(func.count()).select_from(User)
                .outerjoin(SubscriptionState, SubscriptionState.user_id == User.id)
                .where(SubscriptionState.user_id.is_(None), User.remna_uuid.is_not(None))
            )
        ).scalar_one()
        never = (
            await s.execute(select(func.count()).select_from(User).where(User.remna_uuid.is_(None)))
        ).scalar_one()
        last_sync = (await s.execute(select(func.max(SubscriptionState.synced_at)))).scalar_one()
        out["subs"] = {"active": active, "soon": soon, "expired": expired, "unknown": no_state, "never": never,
                       "last_sync": last_sync}

        # --- выручка
        rev_rows = (
            await s.execute(
                select(Payment.currency, func.count(), func.coalesce(func.sum(Payment.amount), 0),
                       func.coalesce(func.sum(Payment.list_amount - Payment.amount), 0))
                .where(_granted(), Payment.created_at >= since)
                .group_by(Payment.currency)
            )
        ).all()
        total_rows = (
            await s.execute(
                select(Payment.currency, func.count(), func.coalesce(func.sum(Payment.amount), 0))
                .where(_granted()).group_by(Payment.currency)
            )
        ).all()
        out["revenue"] = {
            "period": [{"currency": c, "count": n, "sum": int(t or 0), "discount": int(d or 0)} for c, n, t, d in rev_rows],
            "total": [{"currency": c, "count": n, "sum": int(t or 0)} for c, n, t in total_rows],
        }
        day_col = func.date(Payment.created_at)
        by_day = (
            await s.execute(
                select(day_col, Payment.currency, func.count(), func.coalesce(func.sum(Payment.amount), 0))
                .where(_granted(), Payment.created_at >= since)
                .group_by(day_col, Payment.currency)
                .order_by(day_col.desc())
            )
        ).all()
        out["revenue_by_day"] = [{"day": str(d), "currency": c, "count": n, "sum": int(t or 0)} for d, c, n, t in by_day]
        out["pending_payments"] = (
            await s.execute(select(func.count()).select_from(Payment).where(Payment.granted.is_(False)))
        ).scalar_one()

        # --- рефералы
        ref_total = (
            await s.execute(select(func.count()).select_from(User).where(User.referrer_id.is_not(None)))
        ).scalar_one()
        ref_paid = (
            await s.execute(
                select(func.count(distinct(Payment.user_id)))
                .join(User, User.id == Payment.user_id)
                .where(_granted(), User.referrer_id.is_not(None))
            )
        ).scalar_one()
        top = (
            await s.execute(
                select(User.referrer_id, func.count(), func.sum(cast(User.referral_rewarded, Integer)))
                .where(User.referrer_id.is_not(None))
                .group_by(User.referrer_id)
                .order_by(func.count().desc())
                .limit(10)
            )
        ).all()
        top_users = {}
        if top:
            ids = [r[0] for r in top]
            for u in (await s.execute(select(User).where(User.id.in_(ids)))).scalars().all():
                top_users[u.id] = u
        out["referrals"] = {
            "invited": ref_total, "paid": ref_paid,
            "top": [
                {"user": top_users.get(rid), "invited": n, "rewarded": int(rw or 0)} for rid, n, rw in top
            ],
        }

        # --- скидки на руках
        out["discount_holders"] = (
            await s.execute(
                select(func.count()).select_from(PromoRedemption)
                .join(PromoCode, PromoCode.id == PromoRedemption.promo_id)
                .where(PromoRedemption.status == "pending", PromoCode.is_active.is_(True))
            )
        ).scalar_one()
    out["promos"] = await promo_stats(sm)
    return out


async def promo_stats(sm: async_sessionmaker) -> list[dict]:
    now = utcnow()
    async with sm() as s:
        promos = list((await s.execute(select(PromoCode).order_by(PromoCode.id))).scalars().all())
        used = dict((await s.execute(
            select(PromoRedemption.promo_id, func.count()).group_by(PromoRedemption.promo_id))).all())
        uniq = dict((await s.execute(
            select(PromoRedemption.promo_id, func.count(distinct(PromoRedemption.user_id)))
            .group_by(PromoRedemption.promo_id))).all())
        pending = dict((await s.execute(
            select(PromoRedemption.promo_id, func.count()).where(PromoRedemption.status == "pending")
            .group_by(PromoRedemption.promo_id))).all())
        attributed = dict((await s.execute(
            select(User.source_promo_id, func.count()).where(User.source_promo_id.is_not(None))
            .group_by(User.source_promo_id))).all())
        buyers = dict((await s.execute(
            select(PromoRedemption.promo_id, func.count(distinct(PromoRedemption.user_id)))
            .join(Payment, and_(Payment.user_id == PromoRedemption.user_id, _granted()))
            .group_by(PromoRedemption.promo_id))).all())
        active_now = dict((await s.execute(
            select(PromoRedemption.promo_id, func.count(distinct(PromoRedemption.user_id)))
            .join(SubscriptionState, SubscriptionState.user_id == PromoRedemption.user_id)
            .where(SubscriptionState.expire_at > now, PromoRedemption.status == "used")
            .group_by(PromoRedemption.promo_id))).all())
        rev_rows = (await s.execute(
            select(Payment.promo_id, Payment.currency, func.count(), func.coalesce(func.sum(Payment.amount), 0))
            .where(_granted(), Payment.promo_id.is_not(None))
            .group_by(Payment.promo_id, Payment.currency))).all()
    revenue: dict[int, list] = {}
    for pid, cur, n, tot in rev_rows:
        revenue.setdefault(pid, []).append({"currency": cur, "count": n, "sum": int(tot or 0)})
    out = []
    for p in promos:
        exp = ensure_utc(p.expires_at)
        out.append({
            "promo": p, "expired": bool(exp and exp <= now),
            "used": used.get(p.id, 0), "unique": uniq.get(p.id, 0), "pending": pending.get(p.id, 0),
            "attributed": attributed.get(p.id, 0), "buyers": buyers.get(p.id, 0),
            "active_now": active_now.get(p.id, 0), "revenue": revenue.get(p.id, []),
        })
    return out


# ---------------------------------------------------------------------------------------------- пользователи

USER_FILTERS = {
    "all": "Все",
    "new": "Новички (не платили)",
    "paid": "Старички (платили)",
    "active": "Подписка активна",
    "expired": "Подписка истекла",
    "trial": "Брали пробный",
    "ref": "Пришли по рефералке",
    "promo": "Пришли по промокоду",
}


async def search_users(sm: async_sessionmaker, q: str = "", flt: str = "all", page: int = 1,
                       per_page: int = PAGE_SIZE) -> Page:
    now = utcnow()
    paid_sq = (
        select(Payment.user_id.label("uid"), func.count().label("n"))
        .where(_granted()).group_by(Payment.user_id).subquery()
    )
    base = (
        select(User, SubscriptionState.expire_at, SubscriptionState.status, func.coalesce(paid_sq.c.n, 0))
        .outerjoin(SubscriptionState, SubscriptionState.user_id == User.id)
        .outerjoin(paid_sq, paid_sq.c.uid == User.id)
    )
    conds = []
    q = (q or "").strip().lstrip("@")
    if q:
        if q.isdigit():
            conds.append(or_(User.telegram_id == int(q), User.id == int(q)))
        else:
            conds.append(func.lower(User.tg_username).like(f"%{q.lower()}%"))
    if flt == "new":
        conds.append(func.coalesce(paid_sq.c.n, 0) == 0)
    elif flt == "paid":
        conds.append(func.coalesce(paid_sq.c.n, 0) > 0)
    elif flt == "active":
        conds.append(SubscriptionState.expire_at > now)
    elif flt == "expired":
        conds.append(SubscriptionState.expire_at <= now)
    elif flt == "trial":
        conds.append(User.trial_used.is_(True))
    elif flt == "ref":
        conds.append(User.source == "ref")
    elif flt == "promo":
        conds.append(User.source == "promo")
    page = max(1, page)
    async with sm() as s:
        total = (await s.execute(select(func.count()).select_from(base.where(*conds).subquery()))).scalar_one()
        rows = (
            await s.execute(base.where(*conds).order_by(User.id.desc()).limit(per_page).offset((page - 1) * per_page))
        ).all()
    items = [
        {"user": u, "expire_at": ensure_utc(exp), "status": st, "paid": int(n)} for u, exp, st, n in rows
    ]
    return Page(items, page, per_page, total)


async def user_card(sm: async_sessionmaker, telegram_id: int) -> dict | None:
    async with sm() as s:
        u = (await s.execute(select(User).where(User.telegram_id == telegram_id))).scalar_one_or_none()
        if u is None:
            return None
        state = await s.get(SubscriptionState, u.id)
        pays = (
            await s.execute(
                select(Payment, Plan.title).outerjoin(Plan, Plan.id == Payment.plan_id)
                .where(Payment.user_id == u.id).order_by(Payment.id.desc()).limit(50)
            )
        ).all()
        reds = (
            await s.execute(
                select(PromoRedemption, PromoCode)
                .join(PromoCode, PromoCode.id == PromoRedemption.promo_id)
                .where(PromoRedemption.user_id == u.id).order_by(PromoRedemption.id.desc())
            )
        ).all()
        events = (
            await s.execute(select(Event).where(Event.user_id == u.id).order_by(Event.id.desc()).limit(60))
        ).scalars().all()
        referrer = await s.get(User, u.referrer_id) if u.referrer_id else None
        invited = (await s.execute(select(func.count()).select_from(User).where(User.referrer_id == u.id))).scalar_one()
        src_promo = await s.get(PromoCode, u.source_promo_id) if u.source_promo_id else None
    return {
        "user": u, "state": state, "payments": pays, "redemptions": reds, "events": events,
        "referrer": referrer, "invited": invited, "source_promo": src_promo,
    }


# ---------------------------------------------------------------------------------------------- платежи, журнал

async def list_payments(sm: async_sessionmaker, status: str = "all", page: int = 1, per_page: int = PAGE_SIZE) -> Page:
    conds = []
    if status == "pending":
        conds.append(Payment.granted.is_(False))
    elif status == "granted":
        conds.append(_granted())
    async with sm() as s:
        total = (await s.execute(select(func.count()).select_from(Payment).where(*conds))).scalar_one()
        rows = (
            await s.execute(
                select(Payment, User.telegram_id, User.tg_username, Plan.title, PromoCode.code)
                .join(User, User.id == Payment.user_id)
                .outerjoin(Plan, Plan.id == Payment.plan_id)
                .outerjoin(PromoCode, PromoCode.id == Payment.promo_id)
                .where(*conds).order_by(Payment.id.desc()).limit(per_page).offset((max(1, page) - 1) * per_page)
            )
        ).all()
    items = [{"payment": p, "tg_id": tg, "username": un, "plan": t, "promo": c} for p, tg, un, t, c in rows]
    return Page(items, max(1, page), per_page, total)


async def list_audit(sm: async_sessionmaker, page: int = 1, per_page: int = 50) -> Page:
    async with sm() as s:
        total = (await s.execute(select(func.count()).select_from(AdminAudit))).scalar_one()
        rows = (
            await s.execute(
                select(AdminAudit).order_by(AdminAudit.id.desc()).limit(per_page).offset((max(1, page) - 1) * per_page)
            )
        ).scalars().all()
    return Page(list(rows), max(1, page), per_page, total)


async def promo_card(sm: async_sessionmaker, code: str, limit: int = 50) -> dict | None:
    async with sm() as s:
        promo = (await s.execute(select(PromoCode).where(PromoCode.code == code.upper()))).scalar_one_or_none()
        if promo is None:
            return None
        reds = (
            await s.execute(
                select(PromoRedemption, User.telegram_id, User.tg_username)
                .join(User, User.id == PromoRedemption.user_id)
                .where(PromoRedemption.promo_id == promo.id).order_by(PromoRedemption.id.desc()).limit(limit)
            )
        ).all()
    stats = next((x for x in await promo_stats(sm) if x["promo"].id == promo.id), None)
    return {"promo": promo, "redemptions": reds, "stats": stats}


@dataclass
class PlanRow:
    plan: Plan
    payments: int = 0
    prices: dict = field(default_factory=dict)


async def plans_with_usage(sm: async_sessionmaker) -> list[PlanRow]:
    async with sm() as s:
        plans = list((await s.execute(select(Plan).order_by(Plan.sort_order, Plan.days, Plan.id))).scalars().all())
        used = dict((await s.execute(select(Payment.plan_id, func.count()).group_by(Payment.plan_id))).all())
    return [PlanRow(p, used.get(p.id, 0), {x.currency: x.amount for x in p.prices}) for p in plans]

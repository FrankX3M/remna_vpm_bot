"""Оплата криптовалютой через Trybit (https://docs.trybit.com/ru).

Поток:
  1. Пользователь выбирает тариф -> TrybitProvider.start() создаёт заказ (trybit_orders, status=pending) и счёт в Trybit,
     показывает кнопку «Оплатить» со ссылкой на страницу Trybit.
  2. Trybit присылает POSTBACK на /trybit/postback (публикуется через Caddy на VPS RU). Подпись JWT проверяется,
     статус и сумма перезапрашиваются у API (см. trybit_api.py), затем TrybitStore.finalize_paid() вызывает
     svc.process_payment(provider="trybit", external_id=<INV-...>) — ровно как Stars.
  3. Страховка: reconcile_loop раз в минуту сверяет зависшие заказы через API, а кнопка «Проверить оплату» делает то же
     по требованию пользователя. Если панель недоступна, платёж остаётся granted=False -> /pending и /regrant.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from decimal import Decimal
from html import escape

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.requests import Request
from starlette.responses import JSONResponse

from ..config import Settings
from ..db import Plan, TrybitOrder, utcnow
from ..services import VPNError, VPNService
from ..utils import ensure_utc
from .base import PaymentProvider, announce_outcome
from .trybit_api import Invoice, PaymentRecord, TrybitConfig, TrybitService

log = logging.getLogger(__name__)

PROVIDER = "trybit"
MAX_BODY = 64 * 1024
NOTIFY_EXPIRED_WITHIN = timedelta(hours=6)     # о давно истёкших счетах пользователю не пишем
POLL_WINDOW = timedelta(days=2)


def make_config(settings: Settings) -> TrybitConfig:
    return TrybitConfig(
        api_key=settings.trybit_api_key, shop_id=settings.trybit_shop_id, secret_key=settings.trybit_secret_key,
        invoice_ttl_minutes=max(5, settings.trybit_invoice_minutes), allow_test=settings.trybit_allow_test,
        available_currencies=tuple(c.strip() for c in settings.trybit_currencies.split(",") if c.strip()),
    )


class TrybitStore:
    """Реализация PaymentStore поверх БД бота."""

    def __init__(self, sm: async_sessionmaker, svc: VPNService, bot: Bot, admin_ids: set[int]):
        self.sm, self.svc, self.bot, self.admin_ids = sm, svc, bot, admin_ids

    # ---- заказы
    async def create_order(self, *, telegram_id: int, chat_id: int, plan_id: int, redemption_id: int | None,
                           amount: int, currency: str,
                           coin: str | None = None) -> TrybitOrder:
        async with self.sm() as s:
            order = TrybitOrder(telegram_id=telegram_id, chat_id=chat_id, plan_id=plan_id, redemption_id=redemption_id,
                                amount=amount, currency=currency, coin=coin)
            s.add(order)
            await s.commit()
            return order

    async def find_reusable(self, *, telegram_id: int, plan_id: int, redemption_id: int | None, amount: int,
                            currency: str, ttl_minutes: int,
                            coin: str | None = None) -> TrybitOrder | None:
        """Тот же счёт повторно (двойной клик, «Купить» ещё раз) — без нового счёта в Trybit."""
        since = utcnow() - timedelta(minutes=max(1, ttl_minutes - 5))
        async with self.sm() as s:
            q = select(TrybitOrder).where(
                TrybitOrder.telegram_id == telegram_id, TrybitOrder.plan_id == plan_id, TrybitOrder.amount == amount,
                TrybitOrder.currency == currency, TrybitOrder.status == "pending", TrybitOrder.link.is_not(None),
                TrybitOrder.created_at > since,
                (TrybitOrder.coin == coin) if coin else TrybitOrder.coin.is_(None),
                (TrybitOrder.redemption_id == redemption_id) if redemption_id else TrybitOrder.redemption_id.is_(None),
            ).order_by(TrybitOrder.id.desc())
            return (await s.execute(q)).scalars().first()

    async def get_order(self, order_id: int) -> TrybitOrder | None:
        async with self.sm() as s:
            return await s.get(TrybitOrder, order_id)

    @staticmethod
    def _record(o: TrybitOrder) -> PaymentRecord:
        return PaymentRecord(str(o.id), o.status, Decimal(o.amount), o.currency, o.invoice_uuid)

    # ---- PaymentStore
    async def get_payment(self, order_id: str) -> PaymentRecord | None:
        if not order_id.isdigit():
            return None
        o = await self.get_order(int(order_id))
        return self._record(o) if o else None

    async def set_invoice(self, order_id: str, invoice_uuid: str, link: str) -> None:
        async with self.sm() as s:
            await s.execute(update(TrybitOrder).where(TrybitOrder.id == int(order_id))
                            .values(invoice_uuid=invoice_uuid, link=link[:255]))
            await s.commit()

    async def finalize_paid(self, order_id: str, invoice: Invoice) -> bool:
        oid = int(order_id)
        async with self.sm() as s:   # атомарный захват: только один обработчик (постбэк/сверка) пройдёт дальше
            res = await s.execute(update(TrybitOrder).where(TrybitOrder.id == oid, TrybitOrder.status == "pending")
                                  .values(status="paid", paid_at=utcnow()))
            await s.commit()
            if res.rowcount != 1:
                return False
            order = await s.get(TrybitOrder, oid)
        try:
            outcome = await self.svc.process_payment(
                telegram_id=order.telegram_id, plan_id=order.plan_id, redemption_id=order.redemption_id,
                provider=PROVIDER, currency=order.currency, amount=order.amount, external_id=invoice.uuid,
            )
        except VPNError as e:   # деньги получены, но выдать нельзя (тариф удалён / пользователя нет) — решает человек
            log.error("Trybit заказ %s оплачен (%s), но не обработан: %s", oid, invoice.uuid, e)
            await self._set_status(oid, "error")
            await self._notify_admins(f"🚨 Trybit: счёт {invoice.uuid} (заказ #{oid}, user {order.telegram_id}) оплачен, "
                                      f"но не обработан: {e}")
            return False
        except Exception:
            await self._set_status(oid, "pending")   # временная ошибка (БД и т.п.): сверка повторит
            raise
        try:
            await announce_outcome(self.bot, order.chat_id, outcome, self.admin_ids)
        except Exception:  # noqa: BLE001  (пользователь мог заблокировать бота — подписка уже выдана)
            log.exception("Не удалось отправить подтверждение оплаты (заказ %s)", oid)
        return True

    async def mark_failed(self, order_id: str, reason: str) -> None:
        oid = int(order_id)
        async with self.sm() as s:
            res = await s.execute(update(TrybitOrder).where(TrybitOrder.id == oid, TrybitOrder.status == "pending")
                                  .values(status="canceled"))
            await s.commit()
            order = await s.get(TrybitOrder, oid)
        if res.rowcount == 1 and order and utcnow() - ensure_utc(order.created_at) < NOTIFY_EXPIRED_WITHIN:
            try:
                await self.bot.send_message(
                    order.chat_id,
                    "⌛ Счёт на оплату истёк. Если вы ещё не платили — просто выберите тариф заново.\n"
                    f"Если оплатили, а подписка не пришла, напишите в поддержку и укажите номер счёта "
                    f"<code>{escape(order.invoice_uuid or '—')}</code>.",
                )
            except Exception:  # noqa: BLE001
                log.warning("Не удалось уведомить об истёкшем счёте (заказ %s)", oid)

    async def notify_partial(self, order_id: str, invoice: Invoice) -> None:
        oid = int(order_id)
        async with self.sm() as s:
            res = await s.execute(update(TrybitOrder).where(TrybitOrder.id == oid, TrybitOrder.partial_notified.is_(False))
                                  .values(partial_notified=True))
            await s.commit()
            order = await s.get(TrybitOrder, oid)
        if res.rowcount != 1 or order is None:
            return
        await self._notify_admins(f"⚠️ Trybit: счёт {invoice.uuid} (заказ #{oid}, user {order.telegram_id}) оплачен ЧАСТИЧНО. "
                                  "Подписка не выдана: подтвердите счёт вручную в кабинете Trybit или дождитесь доплаты.")
        try:
            await self.bot.send_message(
                order.chat_id,
                "⚠️ Поступила только часть суммы. Доплатите разницу по той же ссылке или напишите в поддержку, "
                f"указав номер счёта <code>{escape(invoice.uuid)}</code>.",
            )
        except Exception:  # noqa: BLE001
            log.warning("Не удалось уведомить о частичной оплате (заказ %s)", oid)

    # ---- служебное
    async def _set_status(self, oid: int, status: str) -> None:
        async with self.sm() as s:
            await s.execute(update(TrybitOrder).where(TrybitOrder.id == oid).values(status=status))
            await s.commit()

    async def _notify_admins(self, text: str) -> None:
        for admin in self.admin_ids:
            try:
                await self.bot.send_message(admin, text)
            except Exception:  # noqa: BLE001
                log.exception("Не удалось уведомить админа %s", admin)

    async def list_pending(self) -> list[PaymentRecord]:
        """Заказы со счётом, ждущие оплаты. Заказы без счёта (сбой при создании) закрываются через 10 минут."""
        async with self.sm() as s:
            await s.execute(update(TrybitOrder).where(
                TrybitOrder.status == "pending", TrybitOrder.invoice_uuid.is_(None),
                TrybitOrder.created_at < utcnow() - timedelta(minutes=10)).values(status="canceled"))
            await s.commit()
            rows = (await s.execute(select(TrybitOrder).where(
                TrybitOrder.status == "pending", TrybitOrder.invoice_uuid.is_not(None),
                TrybitOrder.created_at > utcnow() - POLL_WINDOW).order_by(TrybitOrder.id).limit(500))).scalars().all()
        return [self._record(o) for o in rows]


# подписи кнопок выбора монеты; порядок в COIN_ORDER = от дешёвой сети к дорогой (комиссия сети у клиента)
COIN_LABELS = {
    "USDT_TON": "USDT · TON (комиссия сети минимальная)",
    "TON": "TON",
    "USDT_SOL": "USDT · Solana",
    "USDT_BSC": "USDT · BNB Chain (BEP20)",
    "USDT_TRC20": "USDT · TRON (TRC20, дороже)",
    "BTC": "Bitcoin (BTC)",
    "LTC": "Litecoin (LTC)",
    "ETH": "Ethereum (ETH)",
    "USDT_ERC20": "USDT · Ethereum (ERC20, дорого)",
}
COIN_ORDER = ["USDT_TON", "TON", "USDT_SOL", "USDT_BSC", "LTC", "USDT_TRC20", "BTC", "ETH", "USDT_ERC20"]


class TrybitProvider(PaymentProvider):
    name = PROVIDER
    currency = "RUB"
    title = "💎 Криптовалюта (Trybit)"

    def __init__(self, service: TrybitService, store: TrybitStore, ttl_minutes: int):
        self.service, self.store, self.ttl = service, store, ttl_minutes

    def format_price(self, amount: int) -> str:
        return f"{amount} ₽"

    def options(self) -> list[tuple[str, str]]:
        """Кнопки выбора монеты: из TRYBIT_CURRENCIES, дешёвые сети первыми. Пусто = выбор на странице Trybit."""
        codes = [c for c in self.service.cfg.available_currencies if c in COIN_LABELS]
        codes.sort(key=lambda c: COIN_ORDER.index(c) if c in COIN_ORDER else len(COIN_ORDER))
        return [(c, COIN_LABELS[c]) for c in codes]

    async def start(self, bot: Bot, chat_id: int, plan: Plan, amount: int, *, redemption_id: int | None = None,
                    discount: int = 0, description: str | None = None, option: str | None = None) -> None:
        coin = option if option in dict(self.options()) else None
        order = await self.store.find_reusable(telegram_id=chat_id, plan_id=plan.id, redemption_id=redemption_id,
                                               amount=amount, currency=self.currency, ttl_minutes=self.ttl, coin=coin)
        if order is None:
            order = await self.store.create_order(telegram_id=chat_id, chat_id=chat_id, plan_id=plan.id,
                                                  redemption_id=redemption_id, amount=amount, currency=self.currency,
                                                  coin=coin)
            try:
                await self.service.start_payment(str(order.id), Decimal(amount), self.currency, cryptocurrency=coin)
            except Exception:  # noqa: BLE001
                log.exception("Trybit: не удалось создать счёт (заказ %s)", order.id)
                await self.store._set_status(order.id, "canceled")
                await bot.send_message(chat_id, "⚠️ Не удалось создать счёт на оплату. Попробуйте через пару минут "
                                                "или выберите другой способ оплаты.")
                return
            order = await self.store.get_order(order.id)
        note = f" (скидка {discount}%)" if discount else ""
        text = (
            f"💎 <b>Оплата криптовалютой</b>\nТариф: «{escape(plan.title)}» — {self.format_price(amount)}{note}\n\n"
            "Нажмите «Оплатить», выберите монету и сеть — страница покажет адрес и точную сумму. "
            f"Счёт действует {self.ttl} мин. Подписка придёт сама, как только сеть подтвердит перевод "
            "(обычно 1–5 минут, для BTC дольше).\n\n"
            "⚠️ Отправляйте ровно ту сумму и в той сети, что указаны на странице оплаты."
        )
        await bot.send_message(chat_id, text, reply_markup=self.keyboard(order))

    @staticmethod
    def keyboard(order: TrybitOrder) -> InlineKeyboardMarkup:
        b = InlineKeyboardBuilder()
        b.button(text="💎 Оплатить", url=order.link)
        b.button(text="🔄 Проверить оплату", callback_data=f"trybit_check:{order.id}")
        b.button(text="⬅️ Меню", callback_data="menu")
        b.adjust(1)
        return b.as_markup()


# ---------------------------------------------------------------- кнопка «Проверить оплату»
router = Router(name="trybit")


@router.callback_query(F.data.startswith("trybit_check:"))
async def cb_check(cb: CallbackQuery, trybit_service: TrybitService, trybit_store: TrybitStore) -> None:
    try:
        oid = int(cb.data.split(":")[1])
    except ValueError:
        await cb.answer()
        return
    order = await trybit_store.get_order(oid)
    if order is None or order.telegram_id != cb.from_user.id:
        await cb.answer("Заказ не найден.", show_alert=True)
        return
    if order.status == "paid":
        await cb.answer("Оплата уже получена, подписка активна ✅", show_alert=True)
        return
    if order.status != "pending" or not order.invoice_uuid:
        await cb.answer("Этот счёт закрыт. Выберите тариф заново.", show_alert=True)
        return
    result = (await trybit_service.reconcile([trybit_store._record(order)])).get(str(oid), "")
    if result == "paid":
        await cb.answer()    # подтверждение с датой отправил finalize_paid
    elif result == "partial":
        await cb.answer("Поступила только часть суммы. Доплатите по той же ссылке.", show_alert=True)
    elif result == "failed":
        await cb.answer("Счёт истёк. Выберите тариф заново.", show_alert=True)
    else:
        await cb.answer("Оплата пока не найдена. Подтверждение в сети занимает 1–5 минут — "
                        "подписка придёт автоматически.", show_alert=True)


# ---------------------------------------------------------------- POSTBACK и фоновая сверка
def postback_endpoint(service: TrybitService):
    """Starlette-обработчик POST /trybit/postback. Без сессий и CSRF: защита — подпись JWT + перезапрос счёта у API."""
    async def endpoint(request: Request) -> JSONResponse:
        try:
            if int(request.headers.get("content-length") or 0) > MAX_BODY:
                return JSONResponse({"error": "Too large"}, status_code=413)
        except ValueError:
            return JSONResponse({"error": "Bad request"}, status_code=400)
        body = await request.body()
        if len(body) > MAX_BODY:
            return JSONResponse({"error": "Too large"}, status_code=413)
        code, payload = await service.handle_postback(body, request.headers.get("content-type", ""))
        return JSONResponse(payload, status_code=code)
    return endpoint


async def reconcile_loop(service: TrybitService, store: TrybitStore, every: float = 60.0) -> None:
    await asyncio.sleep(20)
    while True:
        try:
            pending = await store.list_pending()
            if pending:
                res = await service.reconcile(pending)
                done = {k: v for k, v in res.items() if not v.startswith(("wait", "skip"))}
                if done:
                    log.info("Trybit сверка: %s", done)
        except Exception:  # noqa: BLE001
            log.exception("Trybit: ошибка сверки")
        await asyncio.sleep(every)

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.types import LabeledPrice, Message, PreCheckoutQuery

from ..config import Settings
from ..db import Plan
from ..services import VPNService
from .base import PaymentProvider, announce_outcome


class StarsProvider(PaymentProvider):
    name = "stars"
    currency = "XTR"
    title = "⭐ Telegram Stars"

    def format_price(self, amount: int) -> str:
        return f"{amount} ⭐"

    async def start(
        self, bot: Bot, chat_id: int, plan: Plan, amount: int, *, redemption_id: int | None = None, discount: int = 0
    ) -> None:
        note = f" (скидка {discount}%)" if discount else ""
        await bot.send_invoice(
            chat_id=chat_id,
            title=plan.title[:32],
            description=f"Подписка VPN на {plan.days} дн.{note}",
            payload=VPNService.make_payload(plan.id, redemption_id),
            currency=self.currency,
            prices=[LabeledPrice(label=plan.title[:32], amount=amount)],
            # provider_token для Stars не нужен
        )


router = Router(name="stars")


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery, svc: VPNService) -> None:
    error = await svc.validate_order(query.from_user.id, query.invoice_payload, query.currency, query.total_amount)
    if error:
        await query.answer(ok=False, error_message=error)
    else:
        await query.answer(ok=True)


@router.message(F.successful_payment)
async def successful_payment(message: Message, bot: Bot, svc: VPNService, settings: Settings) -> None:
    sp = message.successful_payment
    plan_id, redemption_id = svc.parse_order(sp.invoice_payload)
    outcome = await svc.process_payment(
        telegram_id=message.from_user.id,
        plan_id=plan_id,
        redemption_id=redemption_id,
        provider="stars",
        currency=sp.currency,
        amount=sp.total_amount,
        external_id=sp.telegram_payment_charge_id,  # уникален для каждой оплаты; нужен и для возвратов
    )
    await announce_outcome(bot, message.chat.id, outcome, settings.admin_id_set)

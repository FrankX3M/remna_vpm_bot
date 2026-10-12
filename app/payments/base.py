"""
Как добавить новый способ оплаты (например, карты или крипту):
  1. Создать класс-наследник PaymentProvider (name, currency, title, start()).
  2. Зарегистрировать его в app/main.py в словаре providers.
  3. Когда провайдер подтвердил оплату (вебхук или апдейт), вызвать
     svc.process_payment(provider=<name>, external_id=<id платежа у провайдера>, ...)
     и затем announce_outcome(...).
  4. Админ задаёт цены в новой валюте: /setprice ID СУММА ВАЛЮТА
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ..db import Plan
from ..services import PaymentOutcome
from ..utils import fmt_dt

log = logging.getLogger(__name__)


class PaymentProvider(ABC):
    name: str
    currency: str
    title: str

    def format_price(self, amount: int) -> str:
        return f"{amount} {self.currency}"

    def options(self) -> list[tuple[str, str]]:
        """Подвыбор внутри способа оплаты: [(код, подпись кнопки)]. Пусто — подвыбора нет (например, у Stars).
        Выбранный код приходит в start(option=...)."""
        return []

    @abstractmethod
    async def start(
        self, bot: Bot, chat_id: int, plan: Plan, amount: int, *, redemption_id: int | None = None, discount: int = 0,
        description: str | None = None, option: str | None = None,
    ) -> None:
        """Показать пользователю счёт / ссылку на оплату.
        amount — уже с учётом скидки; redemption_id (активация промокода-скидки) нужно положить в payload через
        VPNService.make_payload(plan.id, redemption_id), а при подтверждении оплаты передать в svc.process_payment(redemption_id=...)."""


async def announce_outcome(bot: Bot, chat_id: int, outcome: PaymentOutcome, admin_ids: set[int]) -> None:
    if outcome.status == "duplicate":
        log.info("Повторное уведомление об оплате %s проигнорировано", outcome.payment_id)
        return

    if outcome.status == "error":
        await bot.send_message(
            chat_id,
            "⚠️ Оплата получена, но выдача подписки задержалась. Мы уже разбираемся — ничего повторно оплачивать не нужно.",
        )
        for admin in admin_ids:
            try:
                await bot.send_message(
                    admin, f"🚨 Платёж #{outcome.payment_id} оплачен, но подписка НЕ выдана. После починки панели: /regrant {outcome.payment_id}"
                )
            except Exception:
                log.exception("Не удалось уведомить админа %s", admin)
        return

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="📲 Подключиться", callback_data="connect")]]
    )
    extra = f"\n🎁 Бонус за приглашение: +{outcome.invited_bonus_days} дн." if outcome.invited_bonus_days else ""
    await bot.send_message(
        chat_id, f"✅ Оплата получена! Подписка активна до <b>{fmt_dt(outcome.expire_at)}</b>.{extra}", reply_markup=kb
    )
    if outcome.referrer_telegram_id:
        try:
            await bot.send_message(
                outcome.referrer_telegram_id,
                f"🎉 Приглашённый вами пользователь совершил первую покупку — вам +{outcome.bonus_days} дн.\n"
                f"Подписка действует до <b>{fmt_dt(outcome.referrer_expire_at)}</b>.",
            )
        except Exception:
            log.warning("Не удалось уведомить реферера %s", outcome.referrer_telegram_id)

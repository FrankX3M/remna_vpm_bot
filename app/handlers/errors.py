import logging

from aiogram import Router
from aiogram.filters import ExceptionTypeFilter
from aiogram.types import ErrorEvent, Update

from ..remnawave import RemnawaveError
from ..services import VPNError

router = Router(name="errors")
log = logging.getLogger(__name__)


async def _reply(update: Update, text: str) -> None:
    if update.callback_query:
        await update.callback_query.answer()
        if update.callback_query.message:
            await update.callback_query.message.answer(text)
    elif update.message:
        await update.message.answer(text)


@router.errors(ExceptionTypeFilter(VPNError))
async def vpn_error(event: ErrorEvent) -> bool:
    await _reply(event.update, f"⚠️ {event.exception}")
    return True


@router.errors(ExceptionTypeFilter(RemnawaveError))
async def remna_error(event: ErrorEvent) -> bool:
    log.error("Ошибка панели: %s", event.exception)
    await _reply(event.update, "⚠️ VPN-сервер временно недоступен. Попробуйте через пару минут.")
    return True


@router.errors()
async def unexpected(event: ErrorEvent) -> bool:
    """Последний рубеж: пользователь не должен видеть вечную «крутилку» при непредвиденной ошибке."""
    log.exception("Необработанная ошибка", exc_info=event.exception)
    await _reply(event.update, "⚠️ Что-то пошло не так. Попробуйте ещё раз или напишите в поддержку.")
    return True

import asyncio
import contextlib
import logging
import os
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode

from .config import Settings
from .db import init_db, make_engine
from .handlers import admin, errors, user
from .payments import stars
from .payments.stars import StarsProvider
from .remnawave import RemnawaveClient
from .services import VPNService

log = logging.getLogger(__name__)


def make_bot(settings: Settings) -> Bot:
    # timeout сессии должен заметно превышать long-polling (10 с), иначе через прокси будут ложные обрывы
    session = AiohttpSession(proxy=settings.telegram_proxy or None, timeout=60)
    return Bot(settings.bot_token, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


async def wait_for_telegram(bot: Bot, attempts: int = 30) -> None:
    """Ждёт, пока Telegram станет доступен (прокси/FIN могут подняться позже бота). Не падает сразу."""
    delay = 2.0
    for i in range(1, attempts + 1):
        try:
            me = await bot.get_me()
            log.info("Telegram доступен, бот @%s", me.username)
            return
        except Exception as e:  # noqa: BLE001
            log.warning("Telegram недоступен (попытка %s/%s): %s", i, attempts, e)
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, 30)
    raise RuntimeError(
        "Не удалось связаться с Telegram. Проверьте TELEGRAM_PROXY, контейнер tg-egress и доступность FIN."
    )


async def heartbeat(bot: Bot, path: str, every: float = 60.0) -> None:
    """Раз в минуту проверяет связь с Telegram и обновляет файл-маркер; по нему работает healthcheck."""
    p = Path(path)
    while True:
        try:
            await bot.get_me()
            p.touch()
        except Exception as e:  # noqa: BLE001
            log.warning("heartbeat: нет связи с Telegram: %s", e)
        await asyncio.sleep(every)


async def main() -> None:
    settings = Settings()
    logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    engine, sessionmaker = make_engine(settings.database_url)
    await init_db(engine)

    remna = RemnawaveClient(settings.remnawave_url, settings.remnawave_token, settings.remnawave_behind_proxy)
    svc = VPNService(sessionmaker, remna, settings)

    # Новый способ оплаты = новый провайдер здесь + свой router (если нужен) ниже
    providers = {p.name: p for p in (StarsProvider(),)}

    bot = make_bot(settings)
    dp = Dispatcher(svc=svc, settings=settings, providers=providers)
    dp.include_router(errors.router)
    dp.include_router(admin.router)
    dp.include_router(stars.router)
    dp.include_router(user.router)

    hb: asyncio.Task | None = None
    try:
        await wait_for_telegram(bot)
        try:
            await remna.ping()
            log.info("Панель Remnawave отвечает")
        except Exception as e:  # noqa: BLE001  (панель может подняться позже — бот всё равно стартует)
            log.warning("Панель Remnawave пока недоступна: %s", e)
        hb = asyncio.create_task(heartbeat(bot, settings.heartbeat_file))
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        if hb:
            hb.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await hb
        await remna.close()
        await bot.session.close()
        await engine.dispose()

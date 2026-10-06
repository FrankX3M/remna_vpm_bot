import asyncio
import contextlib
import logging
import os
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

from .config import Settings
from .db import make_engine
from .handlers import admin, errors, user
from .migrate import run_migrations
from .payments import stars
from .payments.stars import StarsProvider
from .remnawave import RemnawaveClient
from .services import VPNService
from .settings_store import SettingsStore

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


USER_COMMANDS = [
    BotCommand(command="start", description="Главное меню"),
    BotCommand(command="promo", description="Ввести промокод"),
    BotCommand(command="support", description="Поддержка"),
]
ADMIN_COMMANDS = USER_COMMANDS + [
    BotCommand(command="web", description="Войти в веб-админку"),
    BotCommand(command="admin", description="Команды админа"),
    BotCommand(command="promos", description="Промокоды"),
    BotCommand(command="stats", description="Сводка"),
]


async def setup_commands(bot: Bot, settings: Settings) -> None:
    """Меню «/» у обычных пользователей — только пользовательские команды; админские — лишь в чатах админов."""
    try:
        await bot.set_my_commands(USER_COMMANDS, scope=BotCommandScopeDefault())
        for lang in ("ru", "en"):  # списки по языку перекрывают общий — убираем, чтобы не торчали старые команды
            await bot.delete_my_commands(scope=BotCommandScopeDefault(), language_code=lang)
    except Exception as e:  # noqa: BLE001
        log.warning("Не удалось обновить список команд бота: %s", e)
    for admin_id in settings.admin_id_set:
        try:
            await bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception as e:  # noqa: BLE001  (админ ещё не писал боту — чата нет; появится после /start)
            log.warning("Команды админа %s не выставлены: %s", admin_id, e)


async def sync_loop(svc: VPNService, store: SettingsStore) -> None:
    """Периодически подтягивает сроки подписок из панели в локальный снимок (для аналитики)."""
    await asyncio.sleep(30)
    while True:
        interval = store.get_int("sync_interval_min")
        if interval > 0:
            try:
                n = await svc.sync_subscriptions()
                log.info("Синхронизировано подписок: %s", n)
            except Exception:  # noqa: BLE001
                log.exception("Ошибка синхронизации подписок")
        await asyncio.sleep(max(interval, 1) * 60)


async def main() -> None:
    settings = Settings()
    logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    engine, sessionmaker = make_engine(settings.database_url)
    await run_migrations(engine, settings.database_url)
    store = SettingsStore(sessionmaker, settings)
    await store.load()

    remna = RemnawaveClient(settings.remnawave_url, settings.remnawave_token, settings.remnawave_behind_proxy)
    svc = VPNService(sessionmaker, remna, settings, store)

    # Новый способ оплаты = новый провайдер здесь + свой router (если нужен) ниже
    providers = {p.name: p for p in (StarsProvider(),)}

    bot = make_bot(settings)
    dp = Dispatcher(svc=svc, settings=settings, store=store, providers=providers)
    dp.include_router(errors.router)
    dp.include_router(admin.router)
    dp.include_router(stars.router)
    dp.include_router(user.router)

    background: list[asyncio.Task] = [
        asyncio.create_task(store.refresh_forever(), name="settings-refresh"),
        asyncio.create_task(sync_loop(svc, store), name="subs-sync"),
    ]
    if settings.web_enabled:
        from .web.server import run_web   # импорт здесь: веб можно выключить, не ставя его зависимости

        background.append(asyncio.create_task(run_web(svc, settings, store, bot), name="web"))
    hb: asyncio.Task | None = None
    try:
        await wait_for_telegram(bot)
        await setup_commands(bot, settings)
        try:
            await remna.ping()
            log.info("Панель Remnawave отвечает")
        except Exception as e:  # noqa: BLE001  (панель может подняться позже — бот всё равно стартует)
            log.warning("Панель Remnawave пока недоступна: %s", e)
        hb = asyncio.create_task(heartbeat(bot, settings.heartbeat_file))
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        for t in [*background, *([hb] if hb else [])]:
            t.cancel()
        for t in [*background, *([hb] if hb else [])]:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        await remna.close()
        await bot.session.close()
        await engine.dispose()

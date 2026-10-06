"""Самопроверка перед запуском: python -m app.selfcheck  (код возврата 0 = всё в порядке)."""
import asyncio
import sys

from .config import Settings
from .main import make_bot
from .remnawave import RemnawaveClient


async def run() -> int:
    s = Settings()
    bad = 0
    bot = make_bot(s)
    try:
        me = await asyncio.wait_for(bot.get_me(), 30)
        print(f"OK    Telegram через {s.telegram_proxy or 'прямое соединение'}: @{me.username}")
    except Exception as e:  # noqa: BLE001
        bad += 1
        print(f"FAIL  Telegram недоступен: {type(e).__name__}: {e}")
    finally:
        await bot.session.close()

    remna = RemnawaveClient(s.remnawave_url, s.remnawave_token, s.remnawave_behind_proxy)
    try:
        await remna.ping()
        print(f"OK    Панель {s.remnawave_url} отвечает, токен принят")
    except Exception as e:  # noqa: BLE001
        bad += 1
        print(f"FAIL  Панель {s.remnawave_url}: {e}")
    finally:
        await remna.close()

    if not s.squad_list:
        print("WARN  SQUAD_UUIDS пуст: новые пользователи не получат доступ к нодам")
    if not s.admin_id_set:
        print("WARN  ADMIN_IDS пуст: админ-команды недоступны")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))

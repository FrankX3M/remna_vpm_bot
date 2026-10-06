"""Применение миграций при старте. PostgreSQL — через Alembic; SQLite (локальная разработка/тесты) — create_all."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine

from .db import Base

log = logging.getLogger(__name__)
_APP_DIR = Path(__file__).resolve().parent


def alembic_config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_APP_DIR / "migrations"))
    cfg.attributes["url"] = url
    return cfg


async def run_migrations(engine: AsyncEngine, url: str) -> None:
    if url.startswith("sqlite"):
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        return
    log.info("Применяю миграции БД (alembic upgrade head)")
    # env.py запускает свой event loop — поэтому в отдельном потоке
    await asyncio.to_thread(command.upgrade, alembic_config(url), "head")

"""Одноразовый перенос данных из старой SQLite-базы в PostgreSQL.

    python -m app.sqlite_import /data/vpnbot.db            # URL целевой БД берётся из DATABASE_URL
    python -m app.sqlite_import old.db --target postgresql+asyncpg://...

Правила безопасности:
  - исходный файл открывается только на чтение и НЕ изменяется;
  - если в целевой БД уже есть пользователи/тарифы/платежи — импорт отказывается работать (код 2);
  - всё копируется в одной транзакции: либо всё, либо ничего;
  - колонки, которых нет в старой базе (источник, скидки и т.д.), заполняются значениями по умолчанию;
    пользователи с реферером получают source='ref'.
"""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from .config import Settings
from .db import Base, make_engine
from .migrate import run_migrations

# порядок важен: родители раньше детей
COPY_ORDER = ["plans", "plan_prices", "promo_codes", "users", "payments", "promo_redemptions"]
CHECK_EMPTY = ["users", "plans", "payments"]


def _parse_dt(v):
    if v is None or isinstance(v, datetime):
        return v
    if isinstance(v, str):
        try:
            d = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return v


def read_source(path: Path) -> dict[str, list[dict]]:
    uri = f"file:{path.resolve()}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    out: dict[str, list[dict]] = {}
    try:
        have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in COPY_ORDER:
            out[t] = [dict(r) for r in con.execute(f'SELECT * FROM "{t}" ORDER BY id')] if t in have else []
    finally:
        con.close()
    return out


def prepare(table_name: str, rows: list[dict]) -> list[dict]:
    """Оставляет только известные колонки, приводит даты и подставляет значения для новых полей."""
    table = Base.metadata.tables[table_name]
    cols = {c.name: c for c in table.columns}
    out = []
    for row in rows:
        r = {k: v for k, v in row.items() if k in cols}
        for k, c in cols.items():
            if k in r and str(c.type).upper().startswith(("DATETIME", "TIMESTAMP")):
                r[k] = _parse_dt(r[k])
            if k in r and str(c.type).upper() == "BOOLEAN" and r[k] is not None:
                r[k] = bool(r[k])
        if table_name == "users":
            r.setdefault("source", "ref" if r.get("referrer_id") else "organic")
            if not r.get("source"):
                r["source"] = "ref" if r.get("referrer_id") else "organic"
        if table_name == "payments":
            r.setdefault("discount_percent", 0)
            if r.get("discount_percent") is None:
                r["discount_percent"] = 0
        out.append(r)
    return out


async def import_all(engine: AsyncEngine, url: str, data: dict[str, list[dict]]) -> dict[str, int]:
    await run_migrations(engine, url)
    counts: dict[str, int] = {}
    async with engine.begin() as conn:
        for t in CHECK_EMPTY:
            n = (await conn.execute(select(func.count()).select_from(Base.metadata.tables[t]))).scalar_one()
            if n:
                raise RuntimeError(f"В целевой БД уже есть данные (таблица {t}: {n} строк) — импорт отменён.")
        for t in COPY_ORDER:
            rows = prepare(t, data[t])
            table = Base.metadata.tables[t]
            deferred: list[tuple[int, int]] = []
            if t == "users":
                # самоссылка referrer_id: сначала без неё, потом проставляем
                for r in rows:
                    if r.get("referrer_id"):
                        deferred.append((r["id"], r["referrer_id"]))
                        r["referrer_id"] = None
            if rows:
                await conn.execute(insert(table), rows)
            for uid, ref in deferred:
                await conn.execute(update(table).where(table.c.id == uid).values(referrer_id=ref))
            counts[t] = len(rows)
        if conn.dialect.name == "postgresql":   # сдвигаем sequence, иначе следующий INSERT упрётся в занятый id
            for t in COPY_ORDER:
                await conn.execute(text(
                    f"SELECT setval(pg_get_serial_sequence('{t}', 'id'), COALESCE((SELECT MAX(id) FROM {t}), 0) + 1, false)"
                ))
    return counts


async def main_async(path: Path, target: str) -> int:
    if not path.is_file():
        print(f"Нет файла {path}", file=sys.stderr)
        return 1
    if target.startswith("sqlite"):
        print("Целевая БД должна быть PostgreSQL (DATABASE_URL).", file=sys.stderr)
        return 1
    data = read_source(path)
    print("Прочитано из SQLite:", ", ".join(f"{t}={len(v)}" for t, v in data.items()))
    engine, _ = make_engine(target)
    try:
        counts = await import_all(engine, target, data)
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 2
    finally:
        await engine.dispose()
    print("Перенесено в PostgreSQL:", ", ".join(f"{t}={n}" for t, n in counts.items()))
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sqlite_file", type=Path)
    ap.add_argument("--target", default=None, help="URL PostgreSQL (по умолчанию DATABASE_URL из окружения/.env)")
    a = ap.parse_args()
    target = a.target or Settings().database_url
    sys.exit(asyncio.run(main_async(a.sqlite_file, target)))


if __name__ == "__main__":
    main()

from __future__ import annotations

from datetime import datetime, timezone


def ensure_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return ensure_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def iso(dt: datetime) -> str:
    return ensure_utc(dt).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def fmt_dt(dt: datetime | None) -> str:
    dt = ensure_utc(dt)
    return dt.strftime("%d.%m.%Y %H:%M UTC") if dt else "—"


def fmt_bytes(n: int) -> str:
    return f"{n / 1024**3:.2f} ГБ"


def fmt_money(currency: str, amount: int) -> str:
    if currency == "XTR":
        return f"{amount} ⭐"
    return f"{amount} {currency}"

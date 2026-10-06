from __future__ import annotations

from html import escape

from .db import Plan, utcnow
from .services import Account, VPNService
from .utils import fmt_bytes, fmt_dt, fmt_money, parse_dt


def _used_traffic(ru: dict) -> int:
    return ru.get("usedTrafficBytes") or (ru.get("userTraffic") or {}).get("usedTrafficBytes") or 0


def account_text(svc: VPNService, acc: Account) -> str:
    ru = acc.remna
    lines = ["👤 <b>Ваш аккаунт</b>", ""]
    if not ru:
        lines.append("Подписки пока нет. Получите пробный период или купите тариф.")
    else:
        expire = parse_dt(ru.get("expireAt"))
        now = utcnow()
        status = ru.get("status")
        if status == "DISABLED":
            lines.append("Статус: 🚫 отключена")
        elif status == "LIMITED":
            lines.append("Статус: ⚠️ исчерпан трафик")
        elif expire and expire > now:
            lines.append("Статус: ✅ активна")
        else:
            lines.append("Статус: ⛔ срок истёк")

        if expire:
            lines.append(f"Действует до: <b>{fmt_dt(expire)}</b>")
            if expire > now:
                left = expire - now
                lines.append(f"Осталось: {left.days} дн. {left.seconds // 3600} ч.")

        used = _used_traffic(ru)
        limit = ru.get("trafficLimitBytes") or 0
        lines.append(f"Трафик: {fmt_bytes(used)} / {'без лимита' if not limit else fmt_bytes(limit)}")

    if acc.last_payment:
        p = acc.last_payment
        lines.append(f"Последняя оплата: {fmt_dt(p.created_at)} — {fmt_money(p.currency, p.amount)}")
    else:
        lines.append("Оплат пока не было.")
    return "\n".join(lines)


def config_text(url: str | None) -> str:
    if not url:
        return "Ссылка пока недоступна. Попробуйте позже."
    return (
        "🔗 <b>Ваша ссылка подписки</b> (нажмите, чтобы скопировать):\n\n"
        f"<code>{escape(url)}</code>\n\n"
        "Добавьте её в VPN-клиент как подписку — серверы подтянутся автоматически и будут обновляться сами."
    )


def plan_line(plan: Plan) -> str:
    prices = ", ".join(fmt_money(p.currency, p.amount) for p in plan.prices) or "нет цены"
    state = "✅" if plan.is_active else "⏸"
    return f"{state} #{plan.id} {escape(plan.title)} — {plan.days} дн. — {prices}"

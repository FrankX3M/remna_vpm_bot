"""Trybit (криптоэквайринг): клиент API v2, проверка JWT и логика приёма POSTBACK. Не зависит от aiogram и БД.

Документация: https://docs.trybit.com/ru  (API v2: https://api.trybit.com/v2/)

Модуль самостоятельный: не знает про aiogram, БД и Remnawave. С ботом его связывает
протокол PaymentStore (см. ниже) — 5 небольших методов.

Принципы безопасности:
  * JWT из POSTBACK (HS256, SECRET KEY проекта) проверяется ОБЯЗАТЕЛЬНО (в примерах Trybit токен
    необязателен — это дыра, здесь пустой токен = 401), алг. none/другие отклоняются, exp обязателен.
  * Состав JWT-payload в документации не описан и с счётом не связан, поэтому телу POSTBACK
    верить нельзя: после проверки подписи статус и сумма ПЕРЕЗАПРАШИВАЮТСЯ у API
    (POST /invoice/merchant/info) и только они считаются истиной.
  * Счёт привязан к заказу: invoice_uuid сохраняется при создании и сверяется в постбэке.
  * Сумма и валюта счёта сверяются с тем, что бот просил при создании.
  * Идемпотентность: finalize_paid() вызывается безусловно, но обязан сам атомарно переводить
    платёж pending -> paid и возвращать True только в первый раз (UPDATE ... WHERE status='pending').
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable, Optional, Protocol
from urllib.parse import parse_qsl

log = logging.getLogger("trybit")

PAID_STATUSES = {"paid", "overpaid"}   # выдаём подписку
FAILED_STATUSES = {"canceled"}         # платёж не состоялся
# created -> ждём; partial -> уведомляем, не выдаём (админ решает вручную в кабинете Trybit)


class TrybitError(Exception):
    pass


# --------------------------------------------------------------------------- конфигурация и модели
@dataclass(frozen=True)
class TrybitConfig:
    api_key: str
    shop_id: str
    secret_key: str
    base_url: str = "https://api.trybit.com/v2"
    invoice_ttl_minutes: int = 60            # add_fields.time_to_pay
    allow_test: bool = False                 # принимать счета с test_mode=true (только на время тестов!)
    amount_tolerance: Decimal = Decimal("0.01")
    available_currencies: tuple[str, ...] = ()   # напр. ("USDT_TRC20","USDT_TON","TON","BTC"); пусто = все
    timeout: float = 20.0


@dataclass
class PaymentRecord:
    """Что бот хранит про платёж у себя (поля, нужные модулю)."""
    order_id: str
    status: str                 # pending | paid | failed
    amount: Decimal             # сумма со скидкой в валюте `currency`
    currency: str               # RUB, USD ...
    invoice_uuid: Optional[str] = None


@dataclass
class Invoice:
    uuid: str
    status: str
    link: str = ""
    expiry_date: str = ""
    amount_in_fiat: Optional[Decimal] = None
    fiat_currency: str = ""
    test_mode: bool = False
    raw: dict = field(default_factory=dict, repr=False)

    @staticmethod
    def from_api(d: dict) -> "Invoice":
        def dec(v: Any) -> Optional[Decimal]:
            try:
                return Decimal(str(v)) if v is not None else None
            except InvalidOperation:
                return None
        return Invoice(
            uuid=str(d.get("uuid", "")), status=str(d.get("status", "")), link=str(d.get("link") or ""),
            expiry_date=str(d.get("expiry_date") or ""), amount_in_fiat=dec(d.get("amount_in_fiat")),
            fiat_currency=str(d.get("fiat_currency") or "").upper(), test_mode=bool(d.get("test_mode")), raw=d,
        )


class PaymentStore(Protocol):
    """Адаптер к БД и логике бота. Реализуется в приложении."""
    async def get_payment(self, order_id: str) -> Optional[PaymentRecord]: ...
    async def set_invoice(self, order_id: str, invoice_uuid: str, link: str) -> None: ...
    async def finalize_paid(self, order_id: str, invoice: Invoice) -> bool:
        """Атомарно pending->paid, выдать подписку (process_payment), уведомить. True — только в первый раз."""
    async def mark_failed(self, order_id: str, reason: str) -> None: ...
    async def notify_partial(self, order_id: str, invoice: Invoice) -> None: ...


# HTTP-слой подменяемый (для тестов): (url, headers, json_body) -> (status_code, parsed_json)
HttpPost = Callable[[str, dict, dict], Awaitable[tuple[int, Any]]]


async def _aiohttp_post(url: str, headers: dict, body: dict, timeout: float = 20.0) -> tuple[int, Any]:
    import aiohttp  # ставится вместе с aiogram
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
        async with s.post(url, headers=headers, json=body) as r:
            text = await r.text()
            try:
                return r.status, json.loads(text)
            except ValueError:
                return r.status, {"raw": text[:300]}


# --------------------------------------------------------------------------- JWT HS256 без зависимостей
def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def verify_jwt(token: Any, secret: str, *, leeway: int = 30, now: Optional[float] = None) -> Optional[dict]:
    """Вернуть payload, если подпись HS256 верна и срок не истёк; иначе None."""
    if not isinstance(token, str) or not token.strip() or not secret:
        return None
    parts = token.strip().split(".")
    if len(parts) != 3:
        return None
    try:
        header = json.loads(_b64d(parts[0]))
        payload = json.loads(_b64d(parts[1]))
        sig = _b64d(parts[2])
    except (ValueError, TypeError):
        return None
    if not isinstance(header, dict) or header.get("alg") != "HS256" or not isinstance(payload, dict):
        return None
    expected = hmac.new(secret.encode(), f"{parts[0]}.{parts[1]}".encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        return None
    t = time.time() if now is None else now
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or t > exp + leeway:
        return None
    nbf = payload.get("nbf")
    if isinstance(nbf, (int, float)) and t < nbf - leeway:
        return None
    return payload


# --------------------------------------------------------------------------- клиент API
class TrybitClient:
    def __init__(self, cfg: TrybitConfig, http_post: Optional[HttpPost] = None):
        self.cfg = cfg
        self._post = http_post or (lambda u, h, b: _aiohttp_post(u, h, b, cfg.timeout))

    def _headers(self) -> dict:
        return {"Authorization": f"Token {self.cfg.api_key}", "Content-Type": "application/json"}

    async def _call(self, path: str, body: dict) -> Any:
        status, data = await self._post(f"{self.cfg.base_url}{path}", self._headers(), body)
        if status != 200 or not isinstance(data, dict) or data.get("status") != "success":
            # тело ошибки не логируем целиком: там могут быть данные счёта
            raise TrybitError(f"Trybit {path}: HTTP {status}, {str(data)[:200]}")
        return data.get("result")

    async def create_invoice(self, order_id: str, amount: Decimal, currency: str = "RUB",
                             email: Optional[str] = None) -> Invoice:
        ttl = self.cfg.invoice_ttl_minutes
        add_fields: dict = {"time_to_pay": {"hours": ttl // 60, "minutes": ttl % 60}}
        if self.cfg.available_currencies:
            add_fields["available_currencies"] = list(self.cfg.available_currencies)
        body: dict = {"shop_id": self.cfg.shop_id, "amount": float(amount), "currency": currency.upper(),
                      "order_id": order_id, "add_fields": add_fields}
        if email:
            body["email"] = email
        inv = Invoice.from_api(await self._call("/invoice/create", body))
        if not inv.uuid or not inv.link:
            raise TrybitError("Trybit: в ответе нет uuid/link")
        # страховка от неверного понимания единиц amount: сумма в валюте создания должна совпасть
        if inv.fiat_currency and inv.fiat_currency != currency.upper():
            raise TrybitError(f"Trybit: валюта счёта {inv.fiat_currency} != {currency}")
        if inv.amount_in_fiat is not None and abs(inv.amount_in_fiat - amount) > self.cfg.amount_tolerance:
            raise TrybitError(f"Trybit: сумма счёта {inv.amount_in_fiat} != запрошенной {amount}")
        return inv

    async def get_invoices(self, uuids: list[str]) -> list[Invoice]:
        out: list[Invoice] = []
        for i in range(0, len(uuids), 100):   # лимит API — 100 счетов за запрос
            res = await self._call("/invoice/merchant/info", {"uuids": uuids[i:i + 100]})
            out += [Invoice.from_api(x) for x in (res or [])]
        return out

    async def get_invoice(self, uuid: str) -> Optional[Invoice]:
        res = await self.get_invoices([uuid])
        return res[0] if res else None


# --------------------------------------------------------------------------- сервис: создание и приём оплаты
class TrybitService:
    def __init__(self, cfg: TrybitConfig, store: PaymentStore, http_post: Optional[HttpPost] = None):
        self.cfg, self.store = cfg, store
        self.client = TrybitClient(cfg, http_post)

    async def start_payment(self, order_id: str, amount: Decimal, currency: str = "RUB") -> str:
        """Создать счёт для платежа бота и вернуть ссылку на оплату (кнопка «Оплатить»)."""
        inv = await self.client.create_invoice(order_id, amount, currency)
        await self.store.set_invoice(order_id, inv.uuid, inv.link)
        return inv.link

    async def _apply(self, pay: PaymentRecord, inv: Invoice) -> str:
        """Применить актуальное состояние счёта к платежу. Возвращает краткий итог для лога."""
        if not pay.invoice_uuid or pay.invoice_uuid != inv.uuid:
            raise TrybitError("счёт не привязан к заказу")
        if inv.test_mode and not self.cfg.allow_test:
            return "ignored:test_mode"
        if pay.status != "pending":
            return f"skip:{pay.status}"                       # уже обработан — идемпотентно
        if inv.status in PAID_STATUSES:
            if inv.fiat_currency and inv.fiat_currency != pay.currency.upper():
                raise TrybitError("валюта счёта не совпала с заказом")
            if inv.amount_in_fiat is not None and abs(inv.amount_in_fiat - pay.amount) > self.cfg.amount_tolerance:
                raise TrybitError("сумма счёта не совпала с заказом")
            first = await self.store.finalize_paid(pay.order_id, inv)
            return "paid" if first else "skip:duplicate"
        if inv.status in FAILED_STATUSES:
            await self.store.mark_failed(pay.order_id, inv.status)
            return "failed"
        if inv.status == "partial":
            await self.store.notify_partial(pay.order_id, inv)
            return "partial"
        return f"wait:{inv.status}"

    @staticmethod
    def _parse(body: bytes, content_type: str) -> Optional[dict]:
        try:
            if "json" in (content_type or "").lower():
                d = json.loads(body.decode("utf-8"))
                return d if isinstance(d, dict) else None
            return dict(parse_qsl(body.decode("utf-8"), keep_blank_values=True))
        except (ValueError, UnicodeDecodeError):
            return None

    async def handle_postback(self, body: bytes, content_type: str = "application/json") -> tuple[int, dict]:
        """Обработчик POSTBACK. Вернуть (HTTP-код, JSON-ответ). Должен быть доступен снаружи по 443/80."""
        data = self._parse(body, content_type)
        if data is None:
            return 400, {"error": "Invalid body"}
        invoice_id, order_id = str(data.get("invoice_id") or ""), str(data.get("order_id") or "")
        if not invoice_id or not order_id:
            return 400, {"error": "Missing invoice_id or order_id"}
        if verify_jwt(data.get("token"), self.cfg.secret_key) is None:
            log.warning("postback: неверный/просроченный token (order=%s)", order_id)
            return 401, {"error": "Invalid token"}
        uuid = invoice_id if invoice_id.startswith("INV-") else f"INV-{invoice_id}"
        try:
            pay = await self.store.get_payment(order_id)
            if pay is None:
                log.warning("postback: неизвестный order_id=%s", order_id)
                return 400, {"error": "Unknown order"}
            if pay.invoice_uuid and pay.invoice_uuid != uuid:
                log.warning("postback: счёт %s не принадлежит заказу %s", uuid, order_id)
                return 400, {"error": "Invoice mismatch"}
            if not pay.invoice_uuid:   # заказ ещё не получил счёт (гонка при создании) — пусть повторят/сверка добьёт
                return 500, {"error": "Order not ready"}
            inv = await self.client.get_invoice(uuid)          # истина — у API, не в теле запроса
            if inv is None:
                return 400, {"error": "Invoice not found"}
            result = await self._apply(pay, inv)
        except Exception as e:  # noqa: BLE001
            log.error("postback order=%s: %s", order_id, e)
            return 500, {"error": "Processing failed"}         # не 200: пусть Trybit/мы повторим сверку
        log.info("postback order=%s invoice=%s -> %s", order_id, uuid, result)
        return 200, {"message": "Postback received"}

    async def reconcile(self, pending: list[PaymentRecord]) -> dict[str, str]:
        """Страховка к POSTBACK: сверить зависшие pending-платежи через API (запускать раз в 1–2 минуты)."""
        by_uuid = {p.invoice_uuid: p for p in pending if p.invoice_uuid}
        results: dict[str, str] = {}
        if not by_uuid:
            return results
        for inv in await self.client.get_invoices(list(by_uuid)):
            pay = by_uuid.get(inv.uuid)
            if pay is None:
                continue
            try:
                results[pay.order_id] = await self._apply(pay, inv)
            except Exception as e:  # noqa: BLE001
                results[pay.order_id] = f"error:{e}"
        return results

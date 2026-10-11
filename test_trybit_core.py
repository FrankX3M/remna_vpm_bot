"""Тесты trybit.py без сети и без зависимостей: python test_trybit_core.py  (или python -m unittest test_trybit_core)"""
import asyncio
import base64
import hashlib
import hmac
import json
import time
import unittest
from decimal import Decimal
from urllib.parse import urlencode

from app.payments.trybit_api import (Invoice, PaymentRecord, TrybitConfig, TrybitError, TrybitService, verify_jwt)

SECRET = "test-secret"


def b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def make_jwt(secret=SECRET, alg="HS256", exp=None, payload=None):
    h = b64(json.dumps({"alg": alg, "typ": "JWT"}).encode())
    p = b64(json.dumps(payload or {"id": 1, "exp": exp if exp is not None else int(time.time()) + 300}).encode())
    s = b64(hmac.new(secret.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest())
    return f"{h}.{p}.{s}"


class FakeStore:
    def __init__(self, pay):
        self.pay, self.paid_calls, self.failed, self.partial = pay, 0, [], []

    async def get_payment(self, order_id):
        return self.pay if self.pay and self.pay.order_id == order_id else None

    async def set_invoice(self, order_id, invoice_uuid, link):
        self.pay.invoice_uuid = invoice_uuid

    async def finalize_paid(self, order_id, invoice):
        if self.pay.status != "pending":
            return False
        self.pay.status = "paid"
        self.paid_calls += 1
        return True

    async def mark_failed(self, order_id, reason):
        self.pay.status = "failed"
        self.failed.append(reason)

    async def notify_partial(self, order_id, invoice):
        self.partial.append(order_id)


def api_invoice(**kw):
    d = {"uuid": "INV-ABC12345", "status": "paid", "link": "https://pay.trybit.com/ABC12345",
         "amount_in_fiat": 150.0, "fiat_currency": "RUB", "test_mode": False, "expiry_date": "2026-10-09 15:00:00"}
    d.update(kw)
    return d


def make(pay_status="pending", api=None, allow_test=False, uuid="INV-ABC12345"):
    pay = PaymentRecord("42", pay_status, Decimal("150"), "RUB", uuid)
    store = FakeStore(pay)
    calls = []

    async def http(url, headers, body):
        calls.append((url, headers, body))
        res = api if api is not None else api_invoice()
        return 200, {"status": "success", "result": res if isinstance(res, dict) and "uuid" in res and url.endswith("create") else [res]}

    cfg = TrybitConfig("KEY", "SHOP", SECRET, allow_test=allow_test)
    return TrybitService(cfg, store, http), store, calls


def postback(order="42", inv="ABC12345", token=None, **extra):
    d = {"status": "success", "invoice_id": inv, "order_id": order, "token": token or make_jwt(), **extra}
    return json.dumps(d).encode()


def run(c):
    return asyncio.run(c)


class JwtTests(unittest.TestCase):
    def test_valid(self):
        self.assertIsNotNone(verify_jwt(make_jwt(), SECRET))

    def test_rejects(self):
        self.assertIsNone(verify_jwt(make_jwt(secret="other"), SECRET))
        self.assertIsNone(verify_jwt(make_jwt(exp=int(time.time()) - 3600), SECRET))
        self.assertIsNone(verify_jwt(make_jwt(alg="none"), SECRET))
        self.assertIsNone(verify_jwt(make_jwt(payload={"id": 1}), SECRET))     # без exp
        for bad in (None, "", "   ", "a.b", 123, "a.b.c"):
            self.assertIsNone(verify_jwt(bad, SECRET))


class PostbackTests(unittest.TestCase):
    def test_paid_once(self):
        svc, store, _ = make()
        self.assertEqual(run(svc.handle_postback(postback()))[0], 200)
        self.assertEqual(run(svc.handle_postback(postback()))[0], 200)   # повтор уведомления
        self.assertEqual(store.paid_calls, 1)

    def test_overpaid_is_paid(self):
        svc, store, _ = make(api=api_invoice(status="overpaid"))
        run(svc.handle_postback(postback()))
        self.assertEqual(store.paid_calls, 1)

    def test_bad_token(self):
        svc, store, calls = make()
        self.assertEqual(run(svc.handle_postback(postback(token="x.y.z")))[0], 401)
        self.assertEqual(run(svc.handle_postback(postback(token=" ")))[0], 401)
        self.assertEqual((store.paid_calls, len(calls)), (0, 0))        # до проверки подписи в API не ходим

    def test_missing_fields_and_garbage(self):
        svc, _, _ = make()
        self.assertEqual(run(svc.handle_postback(postback(order="")))[0], 400)
        self.assertEqual(run(svc.handle_postback(b"not json"))[0], 400)
        self.assertEqual(run(svc.handle_postback(b"[1]"))[0], 400)

    def test_replay_token_for_other_invoice(self):
        # валидный токен от чужого счёта, но invoice_id не тот, что привязан к заказу
        svc, store, _ = make()
        self.assertEqual(run(svc.handle_postback(postback(inv="ZZZ99999")))[0], 400)
        self.assertEqual(store.paid_calls, 0)

    def test_body_lies_api_says_created(self):
        svc, store, _ = make(api=api_invoice(status="created"))
        run(svc.handle_postback(postback(invoice_info={"status": "paid"}, invoice_status="success")))
        self.assertEqual(store.paid_calls, 0)

    def test_amount_mismatch(self):
        svc, store, _ = make(api=api_invoice(amount_in_fiat=1.0))
        self.assertEqual(run(svc.handle_postback(postback()))[0], 500)
        self.assertEqual(store.paid_calls, 0)

    def test_currency_mismatch(self):
        svc, store, _ = make(api=api_invoice(fiat_currency="USD"))
        self.assertEqual(run(svc.handle_postback(postback()))[0], 500)
        self.assertEqual(store.paid_calls, 0)

    def test_test_mode(self):
        svc, store, _ = make(api=api_invoice(test_mode=True))
        run(svc.handle_postback(postback()))
        self.assertEqual(store.paid_calls, 0)
        svc, store, _ = make(api=api_invoice(test_mode=True), allow_test=True)
        run(svc.handle_postback(postback()))
        self.assertEqual(store.paid_calls, 1)

    def test_partial_and_canceled(self):
        svc, store, _ = make(api=api_invoice(status="partial"))
        run(svc.handle_postback(postback()))
        self.assertEqual((store.partial, store.paid_calls), (["42"], 0))
        svc, store, _ = make(api=api_invoice(status="canceled"))
        run(svc.handle_postback(postback()))
        self.assertEqual((store.failed, store.paid_calls), (["canceled"], 0))

    def test_form_urlencoded(self):
        svc, store, _ = make()
        body = urlencode({"status": "success", "invoice_id": "ABC12345", "order_id": "42", "token": make_jwt()}).encode()
        self.assertEqual(run(svc.handle_postback(body, "application/x-www-form-urlencoded"))[0], 200)
        self.assertEqual(store.paid_calls, 1)

    def test_unknown_order(self):
        svc, _, _ = make()
        self.assertEqual(run(svc.handle_postback(postback(order="999")))[0], 400)

    def test_api_down_returns_500(self):
        svc, store, _ = make()

        async def down(url, headers, body):
            return 502, {"raw": "bad gateway"}
        svc.client._post = down
        self.assertEqual(run(svc.handle_postback(postback()))[0], 500)
        self.assertEqual(store.paid_calls, 0)


class ClientTests(unittest.TestCase):
    def test_create_invoice_request(self):
        svc, store, calls = make(uuid=None)
        link = run(svc.start_payment("42", Decimal("150"), "RUB"))
        url, headers, body = calls[0]
        self.assertEqual(url, "https://api.trybit.com/v2/invoice/create")
        self.assertEqual(headers["Authorization"], "Token KEY")
        self.assertEqual((body["shop_id"], body["amount"], body["currency"], body["order_id"]), ("SHOP", 150.0, "RUB", "42"))
        self.assertEqual(body["add_fields"]["time_to_pay"], {"hours": 1, "minutes": 0})
        self.assertEqual((link, store.pay.invoice_uuid), ("https://pay.trybit.com/ABC12345", "INV-ABC12345"))

    def test_create_invoice_amount_sanity(self):
        svc, _, _ = make(api=api_invoice(amount_in_fiat=1.5), uuid=None)
        with self.assertRaises(TrybitError):
            run(svc.start_payment("42", Decimal("150"), "RUB"))

    def test_reconcile(self):
        svc, store, calls = make()
        res = run(svc.reconcile([store.pay]))
        self.assertEqual((res, store.paid_calls), ({"42": "paid"}, 1))
        self.assertEqual(calls[0][2], {"uuids": ["INV-ABC12345"]})


if __name__ == "__main__":
    unittest.main()

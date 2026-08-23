import hashlib
import hmac
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fulfillment import COOKIE_NAME, FulfillmentApp


NOW = 1_700_000_000
SECRET = "fixture_signing_secret"


class FulfillmentTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.dataset = root / "statusfeed.db"
        with sqlite3.connect(self.dataset) as db:
            db.execute("""CREATE TABLE incidents(
                id TEXT, source TEXT, name TEXT, status TEXT, impact TEXT,
                started_at TEXT, resolved_at TEXT, fetched_at TEXT)""")
            db.execute("INSERT INTO incidents VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (
                "inc_1", "fixture", "Test incident", "resolved", "minor",
                "2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z", "2026-01-01T01:01:00Z",
            ))
        self.ops = root / "ops.db"
        self.app = FulfillmentApp(self.ops, self.dataset, SECRET, clock=lambda: NOW)

    def tearDown(self):
        self.tempdir.cleanup()

    @staticmethod
    def event(event_id="evt_1"):
        return {
            "id": event_id,
            "type": "checkout.session.completed",
            "livemode": False,
            "data": {"object": {
                "id": "cs_test_fixture", "mode": "subscription",
                "payment_status": "paid", "subscription": "sub_test_fixture",
            }},
        }

    def call(self, method, path, body=b"", headers=None, query=""):
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "CONTENT_LENGTH": str(len(body)),
            "wsgi.input": io.BytesIO(body),
        }
        environ.update(headers or {})
        captured = {}

        def start_response(status, response_headers):
            captured["status"] = status
            captured["headers"] = dict(response_headers)

        captured["body"] = b"".join(self.app(environ, start_response))
        return captured

    def signed_webhook(self, event=None, signature=None):
        payload = json.dumps(event or self.event(), separators=(",", ":")).encode()
        if signature is None:
            digest = hmac.new(SECRET.encode(), str(NOW).encode() + b"." + payload, hashlib.sha256).hexdigest()
            signature = f"t={NOW},v1={digest}"
        return self.call("POST", "/stripe/webhook", payload, {"HTTP_STRIPE_SIGNATURE": signature})

    def authenticate(self):
        self.signed_webhook()
        response = self.call("GET", "/checkout/success", query="session_id=cs_test_fixture")
        self.assertEqual("200 OK", response["status"])
        return response["headers"]["Set-Cookie"].split(";", 1)[0]

    def test_valid_signature_provisions_entitlement(self):
        response = self.signed_webhook()
        self.assertEqual("200 OK", response["status"])
        with sqlite3.connect(self.ops) as db:
            self.assertEqual(1, db.execute("SELECT count(*) FROM entitlements WHERE active = 1").fetchone()[0])

    def test_invalid_signature_is_rejected(self):
        response = self.signed_webhook(signature=f"t={NOW},v1={'0' * 64}")
        self.assertEqual("400 Bad Request", response["status"])
        with sqlite3.connect(self.ops) as db:
            self.assertEqual(0, db.execute("SELECT count(*) FROM entitlements").fetchone()[0])

    def test_duplicate_event_is_idempotent(self):
        self.assertEqual("200 OK", self.signed_webhook()["status"])
        response = self.signed_webhook()
        self.assertEqual(b"duplicate\n", response["body"])
        with sqlite3.connect(self.ops) as db:
            self.assertEqual(1, db.execute("SELECT count(*) FROM stripe_events").fetchone()[0])
            self.assertEqual(1, db.execute("SELECT count(*) FROM entitlements").fetchone()[0])

    def test_unauthorized_download_is_rejected(self):
        response = self.call("GET", "/download/statuspulse.csv")
        self.assertEqual("401 Unauthorized", response["status"])
        self.assertEqual("no-store", response["headers"]["Cache-Control"])

    def test_authenticated_csv_and_sqlite_downloads_succeed(self):
        cookie = self.authenticate()
        csv_response = self.call("GET", "/download/statuspulse.csv", headers={"HTTP_COOKIE": cookie})
        self.assertEqual("200 OK", csv_response["status"])
        self.assertIn(b"Test incident", csv_response["body"])
        self.assertIn("attachment", csv_response["headers"]["Content-Disposition"])

        db_response = self.call("GET", "/download/statusfeed.db", headers={"HTTP_COOKIE": cookie})
        self.assertEqual("200 OK", db_response["status"])
        self.assertEqual(self.dataset.read_bytes(), db_response["body"])

    def test_unfulfilled_checkout_cannot_authenticate(self):
        response = self.call("GET", "/checkout/success", query="session_id=cs_test_unknown")
        self.assertEqual("403 Forbidden", response["status"])
        self.assertNotIn("Set-Cookie", response["headers"])


if __name__ == "__main__":
    unittest.main()

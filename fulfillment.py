#!/usr/bin/env python3
"""Minimal Stripe Checkout fulfillment and authenticated dataset downloads."""

import csv
import hashlib
import hmac
import io
import json
import os
import secrets
import sqlite3
import time
from http import cookies
from pathlib import Path
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parent
COOKIE_NAME = "statuspulse_access"
SESSION_LIFETIME = 24 * 60 * 60
SIGNATURE_TOLERANCE = 5 * 60


def verify_stripe_signature(payload, header, secret, now=None):
    """Verify Stripe's v1 webhook signature and return the decoded event."""
    if not secret or not header:
        raise ValueError("missing webhook signature configuration")
    parts = {}
    for item in header.split(","):
        key, separator, value = item.partition("=")
        if separator:
            parts.setdefault(key, []).append(value)
    try:
        timestamp = int(parts["t"][0])
        signatures = parts["v1"]
    except (KeyError, ValueError, IndexError) as exc:
        raise ValueError("malformed webhook signature") from exc
    current = int(time.time() if now is None else now)
    if abs(current - timestamp) > SIGNATURE_TOLERANCE:
        raise ValueError("expired webhook signature")
    signed = str(timestamp).encode("ascii") + b"." + payload
    expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, signature) for signature in signatures):
        raise ValueError("invalid webhook signature")
    try:
        return json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid webhook JSON") from exc


class FulfillmentApp:
    def __init__(self, ops_db, dataset_db, webhook_secret, clock=time.time, allow_live=False):
        self.ops_db = Path(ops_db)
        self.dataset_db = Path(dataset_db)
        self.webhook_secret = webhook_secret
        self.clock = clock
        self.allow_live = allow_live
        self._init_db()

    def _connect(self):
        db = sqlite3.connect(self.ops_db)
        db.row_factory = sqlite3.Row
        return db

    def _init_db(self):
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS stripe_events (
                    event_id TEXT PRIMARY KEY,
                    processed_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS entitlements (
                    checkout_session_id TEXT PRIMARY KEY,
                    subscription_id TEXT NOT NULL UNIQUE,
                    active INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS access_sessions (
                    token_hash TEXT PRIMARY KEY,
                    checkout_session_id TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    FOREIGN KEY(checkout_session_id) REFERENCES entitlements(checkout_session_id)
                );
            """)

    @staticmethod
    def _response(start_response, status, body=b"", headers=()):
        base = [("Content-Length", str(len(body))), ("X-Content-Type-Options", "nosniff")]
        start_response(status, list(headers) + base)
        return [body]

    def __call__(self, environ, start_response):
        method = environ.get("REQUEST_METHOD", "GET").upper()
        path = environ.get("PATH_INFO", "/")
        try:
            if method == "POST" and path == "/stripe/webhook":
                return self._webhook(environ, start_response)
            if method == "GET" and path == "/checkout/success":
                return self._checkout_success(environ, start_response)
            if method == "GET" and path in ("/download/statuspulse.csv", "/download/statusfeed.db"):
                return self._download(environ, start_response, path)
            if method == "GET" and path == "/healthz":
                return self._response(start_response, "200 OK", b"ok\n", [("Content-Type", "text/plain")])
            return self._response(start_response, "404 Not Found", b"not found\n", [("Content-Type", "text/plain")])
        except Exception:
            return self._response(start_response, "500 Internal Server Error", b"internal error\n", [("Content-Type", "text/plain")])

    def _webhook(self, environ, start_response):
        try:
            length = int(environ.get("CONTENT_LENGTH") or "0")
        except ValueError:
            length = 0
        if length > 1024 * 1024:
            return self._response(start_response, "413 Payload Too Large", b"payload too large\n")
        payload = environ["wsgi.input"].read(length)
        try:
            event = verify_stripe_signature(
                payload,
                environ.get("HTTP_STRIPE_SIGNATURE", ""),
                self.webhook_secret,
                self.clock(),
            )
            event_id = event["id"]
            if not isinstance(event_id, str) or not event_id:
                raise ValueError("missing event id")
        except (ValueError, KeyError, TypeError):
            return self._response(start_response, "400 Bad Request", b"invalid webhook\n")
        if event.get("livemode") is not False and not self.allow_live:
            return self._response(start_response, "400 Bad Request", b"test mode required\n")

        now = int(self.clock())
        with self._connect() as db:
            if db.execute("SELECT 1 FROM stripe_events WHERE event_id = ?", (event_id,)).fetchone():
                return self._response(start_response, "200 OK", b"duplicate\n")
            event_type = event.get("type")
            obj = event.get("data", {}).get("object", {})
            if event_type == "checkout.session.completed":
                session_id = obj.get("id")
                subscription_id = obj.get("subscription")
                if (obj.get("mode") != "subscription" or obj.get("payment_status") != "paid"
                        or not isinstance(session_id, str) or not isinstance(subscription_id, str)):
                    return self._response(start_response, "400 Bad Request", b"incomplete checkout\n")
                db.execute("""
                    INSERT INTO entitlements(checkout_session_id, subscription_id, active, created_at)
                    VALUES (?, ?, 1, ?)
                    ON CONFLICT(checkout_session_id) DO UPDATE SET
                        subscription_id = excluded.subscription_id, active = 1
                """, (session_id, subscription_id, now))
            elif event_type == "customer.subscription.deleted":
                subscription_id = obj.get("id")
                if isinstance(subscription_id, str):
                    db.execute("UPDATE entitlements SET active = 0 WHERE subscription_id = ?", (subscription_id,))
            db.execute("INSERT INTO stripe_events(event_id, processed_at) VALUES (?, ?)", (event_id, now))
        return self._response(start_response, "200 OK", b"ok\n")

    def _checkout_success(self, environ, start_response):
        session_id = parse_qs(environ.get("QUERY_STRING", "")).get("session_id", [""])[0]
        with self._connect() as db:
            entitlement = db.execute(
                "SELECT 1 FROM entitlements WHERE checkout_session_id = ? AND active = 1", (session_id,)
            ).fetchone()
            if not entitlement:
                body = b"Payment confirmation is still processing. Refresh this page shortly.\n"
                return self._response(start_response, "403 Forbidden", body, [("Content-Type", "text/plain")])
            token = secrets.token_urlsafe(32)
            token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
            expires = int(self.clock()) + SESSION_LIFETIME
            db.execute("DELETE FROM access_sessions WHERE expires_at <= ?", (int(self.clock()),))
            db.execute(
                "INSERT INTO access_sessions(token_hash, checkout_session_id, expires_at) VALUES (?, ?, ?)",
                (token_hash, session_id, expires),
            )
        cookie = f"{COOKIE_NAME}={token}; Path=/; Max-Age={SESSION_LIFETIME}; HttpOnly; Secure; SameSite=Lax"
        body = (b"<!doctype html><meta charset=utf-8><title>StatusPulse downloads</title>"
                b"<h1>Your StatusPulse downloads</h1>"
                b"<p><a href='/download/statuspulse.csv'>Full CSV dataset</a></p>"
                b"<p><a href='/download/statusfeed.db'>Full SQLite dataset</a></p>")
        return self._response(start_response, "200 OK", body, [
            ("Content-Type", "text/html; charset=utf-8"), ("Set-Cookie", cookie),
            ("Cache-Control", "no-store"), ("Referrer-Policy", "no-referrer"),
        ])

    def _authorized(self, environ):
        jar = cookies.SimpleCookie()
        try:
            jar.load(environ.get("HTTP_COOKIE", ""))
            token = jar[COOKIE_NAME].value
        except (KeyError, cookies.CookieError):
            return False
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self._connect() as db:
            return db.execute("""
                SELECT 1 FROM access_sessions s
                JOIN entitlements e ON e.checkout_session_id = s.checkout_session_id
                WHERE s.token_hash = ? AND s.expires_at > ? AND e.active = 1
            """, (token_hash, int(self.clock()))).fetchone() is not None

    def _download(self, environ, start_response, path):
        if not self._authorized(environ):
            return self._response(start_response, "401 Unauthorized", b"authentication required\n", [
                ("Content-Type", "text/plain"), ("Cache-Control", "no-store"),
            ])
        if path.endswith(".db"):
            body = self.dataset_db.read_bytes()
            content_type = "application/vnd.sqlite3"
            filename = "statuspulse.db"
        else:
            output = io.StringIO(newline="")
            with sqlite3.connect(self.dataset_db) as db:
                db.row_factory = sqlite3.Row
                rows = db.execute("SELECT * FROM incidents ORDER BY started_at DESC")
                writer = csv.writer(output)
                writer.writerow([description[0] for description in rows.description])
                writer.writerows(rows)
            body = output.getvalue().encode("utf-8")
            content_type = "text/csv; charset=utf-8"
            filename = "statuspulse.csv"
        return self._response(start_response, "200 OK", body, [
            ("Content-Type", content_type),
            ("Content-Disposition", f'attachment; filename="{filename}"'),
            ("Cache-Control", "private, no-store"),
        ])


def create_app():
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    if not secret:
        raise RuntimeError("STRIPE_WEBHOOK_SECRET is required")
    return FulfillmentApp(
        os.environ.get("STATUSPULSE_OPS_DB", ROOT / "ops.db"),
        os.environ.get("STATUSPULSE_DATASET_DB", ROOT / "statusfeed.db"),
        secret,
        allow_live=os.environ.get("STATUSPULSE_ALLOW_LIVE", "").lower() == "true",
    )


if __name__ == "__main__":
    from wsgiref.simple_server import make_server

    port = int(os.environ.get("PORT", "8000"))
    with make_server("127.0.0.1", port, create_app()) as server:
        print(f"StatusPulse fulfillment listening on http://127.0.0.1:{port}")
        server.serve_forever()

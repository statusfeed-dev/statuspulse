#!/usr/bin/env python3
"""Record paid pilot orders and alert the owner's Telegram channel."""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DEFAULT_DATABASE = ROOT / "ops.db"
STRIPE_API_VERSION = "2026-07-29.dahlia"
EXPECTED_OFFER_ID = "vendor_reliability_pilot"
EXPECTED_OFFER_VERSION = "1"
EXPECTED_CURRENCY = "usd"
EXPECTED_SUBTOTAL = 7900
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
INTAKE_RETENTION_SECONDS = 90 * 24 * 60 * 60
MAX_STRIPE_PAGES = 10
RETRYABLE_HTTP_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS revenue (
    id INTEGER PRIMARY KEY,
    ts TEXT,
    amount REAL,
    currency TEXT,
    source TEXT,
    transaction_id TEXT,
    note TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS revenue_transaction_idx
    ON revenue(transaction_id) WHERE transaction_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS funnel_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_name TEXT NOT NULL CHECK(event_name IN (
        'payment_succeeded', 'fulfillment_queued',
        'delivery_completed', 'delivery_failed', 'refund'
    )),
    occurred_at INTEGER NOT NULL,
    source_id TEXT NOT NULL,
    metadata TEXT,
    UNIQUE(event_name, source_id)
);
CREATE TABLE IF NOT EXISTS pilot_order_notifications (
    checkout_session_id TEXT PRIMARY KEY,
    payment_intent_id TEXT NOT NULL UNIQUE,
    amount_total INTEGER NOT NULL,
    currency TEXT NOT NULL,
    business_name TEXT,
    dependencies TEXT,
    report_focus TEXT,
    created_at INTEGER NOT NULL,
    discovered_at INTEGER NOT NULL,
    telegram_message_id TEXT,
    notified_at INTEGER,
    fulfillment_state TEXT NOT NULL DEFAULT 'queued'
        CHECK(fulfillment_state IN (
            'queued', 'in_progress', 'delivered', 'on_hold', 'canceled'
        )),
    delivery_started_at INTEGER,
    delivered_at INTEGER,
    intake_purged_at INTEGER
);
CREATE TABLE IF NOT EXISTS pilot_order_reviews (
    checkout_session_id TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    observed_at INTEGER NOT NULL,
    telegram_message_id TEXT,
    notified_at INTEGER,
    resolved_at INTEGER
);
CREATE TABLE IF NOT EXISTS pilot_payment_risk (
    payment_intent_id TEXT PRIMARY KEY,
    amount_refunded INTEGER NOT NULL,
    currency TEXT NOT NULL,
    disputed INTEGER NOT NULL CHECK(disputed IN (0, 1)),
    under_review INTEGER NOT NULL CHECK(under_review IN (0, 1)),
    observed_at INTEGER NOT NULL
);
"""


class NotifierError(RuntimeError):
    """Raised when configuration or a remote response is unsafe to use."""


@dataclass(frozen=True)
class Configuration:
    stripe_api_key: str
    payment_link_id: str
    telegram_bot_token: str
    telegram_channel: str
    database: Path


@dataclass(frozen=True)
class PaidOrder:
    checkout_session_id: str
    payment_intent_id: str
    amount_total: int
    currency: str
    business_name: str | None
    dependencies: str
    report_focus: str | None
    created_at: int

    @property
    def dependency_count(self) -> int:
        return len(
            [value for value in re.split(r"[\n,]", self.dependencies) if value.strip()]
        )


@dataclass(frozen=True)
class ReviewNotice:
    checkout_session_id: str
    reason: str
    observed_at: int


@dataclass(frozen=True)
class PaymentRisk:
    payment_intent_id: str
    amount_refunded: int
    currency: str
    disputed: bool
    under_review: bool


def load_configuration(environment: Mapping[str, str] = os.environ) -> Configuration:
    stripe_key = environment.get("STRIPE_API_KEY", "")
    if not stripe_key.startswith("rk_live_"):
        raise NotifierError("a restricted live Stripe key is required")
    payment_link = environment.get("STATUSPULSE_PAYMENT_LINK_ID", "")
    if not re.fullmatch(r"plink_[A-Za-z0-9]+", payment_link):
        raise NotifierError("STATUSPULSE_PAYMENT_LINK_ID is missing or invalid")
    bot_token = environment.get("TELEGRAM_BOT_TOKEN", "")
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", bot_token):
        raise NotifierError("TELEGRAM_BOT_TOKEN is missing or invalid")
    channel = environment.get("TELEGRAM_HOME_CHANNEL", "")
    if not re.fullmatch(r"-?\d+", channel):
        raise NotifierError("TELEGRAM_HOME_CHANNEL is missing or invalid")
    database = Path(environment.get("STATUSPULSE_OPS_DB", DEFAULT_DATABASE))
    return Configuration(stripe_key, payment_link, bot_token, channel, database)


def _read_json(response: Any) -> dict[str, Any]:
    raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise NotifierError("remote response exceeded the size limit")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise NotifierError("remote service returned invalid JSON") from error
    if not isinstance(value, dict):
        raise NotifierError("remote service returned an unexpected JSON value")
    return value


class StripeClient:
    def __init__(
        self,
        api_key: str,
        opener: Callable[..., Any] = urllib.request.urlopen,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        encoded = base64.b64encode(f"{api_key}:".encode()).decode()
        self._headers = {
            "Authorization": f"Basic {encoded}",
            "Stripe-Version": STRIPE_API_VERSION,
            "User-Agent": "StatusPulse-order-notifier/1.0",
        }
        self._opener = opener
        self._sleeper = sleeper

    def _request_json(self, request: urllib.request.Request) -> dict[str, Any]:
        for attempt in range(3):
            try:
                with self._opener(request, timeout=30) as response:
                    if response.geturl() != request.full_url:
                        raise NotifierError("Stripe API redirected unexpectedly")
                    return _read_json(response)
            except urllib.error.HTTPError as error:
                if error.code not in RETRYABLE_HTTP_STATUS or attempt == 2:
                    raise NotifierError(
                        f"Stripe request failed with HTTP {error.code}"
                    ) from error
                retry_after = (
                    error.headers.get("Retry-After") if error.headers else None
                )
                try:
                    delay = float(retry_after) if retry_after else float(2**attempt)
                except ValueError:
                    delay = float(2**attempt)
                if not math.isfinite(delay):
                    delay = float(2**attempt)
                self._sleeper(min(max(delay, 0.0), 30.0))
            except OSError as error:
                if attempt == 2:
                    raise NotifierError("Stripe request failed") from error
                self._sleeper(float(2**attempt))
        raise AssertionError("Stripe retry loop exhausted unexpectedly")

    def list_paid_sessions(self, payment_link_id: str) -> list[dict[str, Any]]:
        sessions: list[dict[str, Any]] = []
        parameters = {
            "payment_link": payment_link_id,
            "status": "complete",
            "limit": "100",
            "expand[]": "data.payment_intent.latest_charge",
        }
        seen_cursors: set[str] = set()
        for _ in range(MAX_STRIPE_PAGES):
            url = (
                "https://api.stripe.com/v1/checkout/sessions?"
                + urllib.parse.urlencode(parameters)
            )
            request = urllib.request.Request(url, headers=self._headers)
            page = self._request_json(request)
            data = page.get("data")
            if not isinstance(data, list) or any(
                not isinstance(item, dict) for item in data
            ):
                raise NotifierError("Stripe response is missing a session list")
            has_more = page.get("has_more")
            if not isinstance(has_more, bool):
                raise NotifierError("Stripe pagination flag is invalid")
            sessions.extend(data)
            if not has_more:
                return sessions
            if not data:
                raise NotifierError("Stripe pagination returned an empty page")
            last_id = data[-1].get("id")
            if not isinstance(last_id, str) or not last_id:
                raise NotifierError("Stripe pagination cursor is invalid")
            if last_id in seen_cursors:
                raise NotifierError("Stripe pagination cursor repeated")
            seen_cursors.add(last_id)
            parameters["starting_after"] = last_id
        raise NotifierError("Stripe pagination exceeded its page limit")

    def retrieve_session(self, checkout_session_id: str) -> dict[str, Any]:
        """Retrieve one Checkout Session with its current payment risk state."""
        encoded_id = urllib.parse.quote(checkout_session_id, safe="")
        query = urllib.parse.urlencode({"expand[]": "payment_intent.latest_charge"})
        request = urllib.request.Request(
            f"https://api.stripe.com/v1/checkout/sessions/{encoded_id}?{query}",
            headers=self._headers,
        )
        return self._request_json(request)


class TelegramClient:
    def __init__(
        self,
        bot_token: str,
        channel: str,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._channel = channel
        self._opener = opener

    def send_order(self, order: PaidOrder) -> str:
        message = (
            "Paid StatusPulse pilot order\n"
            f"Checkout: {order.checkout_session_id}\n"
            f"Dependency count: {order.dependency_count}\n"
            f"Total: {order.amount_total / 100:.2f} {order.currency.upper()}\n"
            "Open Stripe to verify the receipt and retrieve the customer intake."
        )
        return self._send(message)

    def send_review(self, notice: ReviewNotice) -> str:
        message = (
            "StatusPulse order needs manual review\n"
            f"Checkout: {notice.checkout_session_id}\n"
            f"Reason: {notice.reason}\n"
            "Open Stripe before fulfilling or refunding the order."
        )
        return self._send(message)

    def _send(self, message: str) -> str:
        body = urllib.parse.urlencode(
            {
                "chat_id": self._channel,
                "text": message,
                "disable_web_page_preview": "true",
            }
        ).encode()
        request = urllib.request.Request(
            self._url,
            data=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "StatusPulse-order-notifier/1.0",
            },
            method="POST",
        )
        try:
            with self._opener(request, timeout=30) as response:
                result = _read_json(response)
        except (OSError, urllib.error.HTTPError) as error:
            raise NotifierError(f"Telegram request failed: {error}") from error
        message_id = result.get("result", {}).get("message_id")
        if result.get("ok") is not True or not isinstance(message_id, int):
            raise NotifierError("Telegram did not accept the notification")
        return str(message_id)


def _custom_field(session: Mapping[str, Any], key: str) -> str | None:
    fields = session.get("custom_fields")
    if not isinstance(fields, list):
        return None
    for field in fields:
        if not isinstance(field, dict) or field.get("key") != key:
            continue
        field_type = field.get("type")
        value_container = field.get(field_type) if isinstance(field_type, str) else None
        if isinstance(value_container, dict):
            value = value_container.get("value")
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _require_non_boolean_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise NotifierError(f"{field} is invalid")
    return value


def _validate_payment_state(session: Mapping[str, Any], amount_total: int) -> str:
    """Reject payments with incomplete, refunded, or disputed charge state."""
    risk = _payment_risk(session)
    payment_intent = session.get("payment_intent")
    if not isinstance(payment_intent, dict):
        raise NotifierError("PaymentIntent risk state was not expanded")
    if (
        payment_intent.get("object") != "payment_intent"
        or payment_intent.get("status") != "succeeded"
        or payment_intent.get("currency") != EXPECTED_CURRENCY
        or payment_intent.get("amount_received") != amount_total
    ):
        raise NotifierError("PaymentIntent is not safely settled")

    charge = payment_intent.get("latest_charge")
    if not isinstance(charge, dict) or charge.get("object") != "charge":
        raise NotifierError("latest Charge risk state was not expanded")
    if (
        charge.get("paid") is not True
        or charge.get("status") != "succeeded"
        or charge.get("currency") != EXPECTED_CURRENCY
        or charge.get("amount") != amount_total
        or risk.disputed
        or risk.under_review
        or charge.get("refunded") is not False
        or risk.amount_refunded != 0
    ):
        raise NotifierError("Charge is refunded, disputed, or not safely settled")
    return risk.payment_intent_id


def _payment_risk(session: Mapping[str, Any]) -> PaymentRisk:
    payment_intent = session.get("payment_intent")
    if not isinstance(payment_intent, dict):
        raise NotifierError("PaymentIntent risk state was not expanded")
    payment_intent_id = payment_intent.get("id")
    if not isinstance(payment_intent_id, str) or not payment_intent_id:
        raise NotifierError("PaymentIntent ID is missing")
    charge = payment_intent.get("latest_charge")
    if not isinstance(charge, dict) or charge.get("object") != "charge":
        raise NotifierError("latest Charge risk state was not expanded")
    amount_refunded = _require_non_boolean_int(
        charge.get("amount_refunded"), "refunded amount"
    )
    disputed = charge.get("disputed")
    currency = charge.get("currency")
    review = charge.get("review")
    outcome = charge.get("outcome")
    if amount_refunded < 0 or not isinstance(disputed, bool):
        raise NotifierError("Charge risk state is invalid")
    if not isinstance(currency, str) or not currency:
        raise NotifierError("Charge currency is invalid")
    if review is not None and (not isinstance(review, str) or not review):
        raise NotifierError("Charge review state is invalid")
    if outcome is not None and not isinstance(outcome, dict):
        raise NotifierError("Charge outcome state is invalid")
    under_review = review is not None or (
        isinstance(outcome, dict) and outcome.get("type") == "manual_review"
    )
    return PaymentRisk(
        payment_intent_id=payment_intent_id,
        amount_refunded=amount_refunded,
        currency=currency,
        disputed=disputed,
        under_review=under_review,
    )


def _validate_offer_contract(session: Mapping[str, Any], payment_link_id: str) -> None:
    if session.get("object") != "checkout.session":
        raise NotifierError("unexpected Stripe object")
    if session.get("payment_link") != payment_link_id:
        raise NotifierError("unexpected Payment Link")
    if session.get("livemode") is not True:
        raise NotifierError("session is not live")
    if (
        session.get("mode") != "payment"
        or session.get("status") != "complete"
        or session.get("payment_status") != "paid"
    ):
        raise NotifierError("session is not a paid one-time order")
    metadata = session.get("metadata")
    if not isinstance(metadata, dict) or (
        metadata.get("offer_id") != EXPECTED_OFFER_ID
        or metadata.get("offer_version") != EXPECTED_OFFER_VERSION
    ):
        raise NotifierError("unexpected offer metadata")
    if session.get("currency") != EXPECTED_CURRENCY:
        raise NotifierError("unexpected order currency")
    if session.get("amount_subtotal") != EXPECTED_SUBTOTAL:
        raise NotifierError("unexpected order subtotal")


def _validated_order_total(session: Mapping[str, Any]) -> int:
    automatic_tax = session.get("automatic_tax")
    if not isinstance(automatic_tax, dict) or (
        automatic_tax.get("enabled") is not True
        or automatic_tax.get("status") != "complete"
    ):
        raise NotifierError("automatic tax is not enabled and complete")
    tax_id_collection = session.get("tax_id_collection")
    if not isinstance(tax_id_collection, dict) or (
        tax_id_collection.get("enabled") is not True
    ):
        raise NotifierError("tax ID collection is not enabled")
    if session.get("billing_address_collection") != "required":
        raise NotifierError("required billing address collection is missing")
    total_details = session.get("total_details")
    if not isinstance(total_details, dict):
        raise NotifierError("order totals are missing")
    tax = total_details.get("amount_tax")
    if (
        isinstance(tax, bool)
        or not isinstance(tax, int)
        or tax < 0
        or total_details.get("amount_discount") != 0
        or total_details.get("amount_shipping") != 0
    ):
        raise NotifierError("unexpected discount, shipping, or tax totals")
    amount_total = session.get("amount_total")
    if amount_total != EXPECTED_SUBTOTAL + tax:
        raise NotifierError("unexpected order total")
    consent = session.get("consent")
    if not isinstance(consent, dict) or consent.get("terms_of_service") != "accepted":
        raise NotifierError("Terms acceptance is missing")
    return amount_total


def _validated_customer(session: Mapping[str, Any]) -> tuple[str, int]:
    session_id = session.get("id")
    created_at = session.get("created")
    if not isinstance(session_id, str) or not session_id:
        raise NotifierError("Checkout Session ID is missing")
    if (
        isinstance(created_at, bool)
        or not isinstance(created_at, int)
        or created_at <= 0
    ):
        raise NotifierError("order creation time is invalid")
    customer_details = session.get("customer_details")
    email = (
        customer_details.get("email") if isinstance(customer_details, dict) else None
    )
    if not isinstance(email, str) or not email.strip():
        raise NotifierError("customer email is missing")
    address = (
        customer_details.get("address") if isinstance(customer_details, dict) else None
    )
    if not isinstance(address, dict) or address.get("country") != "US":
        raise NotifierError("pilot orders are limited to US billing addresses")
    return session_id, created_at


def _validated_intake(
    session: Mapping[str, Any],
) -> tuple[str, str, str | None]:
    dependencies = _custom_field(session, "dependencies")
    if not dependencies or len(dependencies) > 255:
        raise NotifierError("dependency intake is missing or invalid")
    dependency_count = len(
        [value for value in re.split(r"[\n,]", dependencies) if value.strip()]
    )
    if not 1 <= dependency_count <= 20:
        raise NotifierError("dependency count is outside the pilot scope")
    collected_information = session.get("collected_information")
    business_name = None
    if isinstance(collected_information, dict):
        candidate = collected_information.get("business_name")
        if isinstance(candidate, str) and candidate.strip():
            business_name = candidate.strip()
    if not business_name or len(business_name) > 150:
        raise NotifierError("business name is missing or invalid")
    return business_name, dependencies, _custom_field(session, "reportfocus")


def parse_paid_order(session: Mapping[str, Any], payment_link_id: str) -> PaidOrder:
    """Validate the complete pilot contract and return its minimal private intake."""
    _validate_offer_contract(session, payment_link_id)
    amount_total = _validated_order_total(session)
    session_id, created_at = _validated_customer(session)
    payment_intent = _validate_payment_state(session, amount_total)
    business_name, dependencies, report_focus = _validated_intake(session)
    return PaidOrder(
        checkout_session_id=session_id,
        payment_intent_id=payment_intent,
        amount_total=amount_total,
        currency=EXPECTED_CURRENCY,
        business_name=business_name,
        dependencies=dependencies,
        report_focus=report_focus,
        created_at=created_at,
    )


class OrderLedger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with closing(self.connect()) as database, database:
            database.executescript(LEDGER_SCHEMA)
        try:
            self.path.chmod(0o600)
        except OSError as error:
            raise NotifierError("cannot secure the private order database") from error

    def connect(self) -> sqlite3.Connection:
        database = sqlite3.connect(self.path, timeout=30)
        database.execute("PRAGMA busy_timeout=30000")
        database.execute("PRAGMA secure_delete=ON")
        return database

    @staticmethod
    def _order_values(order: PaidOrder) -> tuple[object, ...]:
        return (
            order.checkout_session_id,
            order.payment_intent_id,
            order.amount_total,
            order.currency,
            order.business_name,
            order.dependencies,
            order.report_focus,
            order.created_at,
        )

    def _order_exists(self, database: sqlite3.Connection, order: PaidOrder) -> bool:
        existing = database.execute(
            """SELECT checkout_session_id, payment_intent_id, amount_total,
                      currency, business_name, dependencies, report_focus,
                      created_at
                 FROM pilot_order_notifications
                WHERE checkout_session_id=? OR payment_intent_id=?""",
            (order.checkout_session_id, order.payment_intent_id),
        ).fetchone()
        if existing is not None and existing != self._order_values(order):
            raise NotifierError("conflicting immutable order data")
        return existing is not None

    @staticmethod
    def _insert_order(
        database: sqlite3.Connection, order: PaidOrder, discovered_at: int
    ) -> None:
        database.execute(
            """INSERT INTO pilot_order_notifications(
                   checkout_session_id, payment_intent_id, amount_total,
                   currency, business_name, dependencies, report_focus,
                   created_at, discovered_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (*OrderLedger._order_values(order), discovered_at),
        )

    @staticmethod
    def _record_gross_revenue(database: sqlite3.Connection, order: PaidOrder) -> None:
        values = (
            order.amount_total / 100,
            order.currency.upper(),
            "stripe_statuspulse_pilot",
        )
        existing = database.execute(
            "SELECT amount, currency, source FROM revenue WHERE transaction_id=?",
            (order.payment_intent_id,),
        ).fetchone()
        if existing is not None and existing != values:
            raise NotifierError("conflicting immutable revenue data")
        if existing is None:
            database.execute(
                """INSERT INTO revenue(
                       ts, amount, currency, source, transaction_id, note
                   ) VALUES (datetime(?, 'unixepoch'), ?, ?, ?, ?, ?)""",
                (
                    order.created_at,
                    *values,
                    order.payment_intent_id,
                    f"Checkout {order.checkout_session_id}",
                ),
            )

    @staticmethod
    def _record_payment_event(database: sqlite3.Connection, order: PaidOrder) -> None:
        metadata = json.dumps(
            {
                "offer_id": EXPECTED_OFFER_ID,
                "payment_intent_id": order.payment_intent_id,
            },
            sort_keys=True,
        )
        database.execute(
            """INSERT OR IGNORE INTO funnel_events(
                   event_name, occurred_at, source_id, metadata
               ) VALUES ('payment_succeeded', ?, ?, ?)""",
            (order.created_at, order.checkout_session_id, metadata),
        )

    def record(self, order: PaidOrder, discovered_at: int) -> bool:
        with closing(self.connect()) as database, database:
            if self._order_exists(database, order):
                return False
            self._insert_order(database, order, discovered_at)
            self._record_gross_revenue(database, order)
            self._record_payment_event(database, order)
            return True

    def pending(self) -> list[PaidOrder]:
        with closing(self.connect()) as database, database:
            rows = database.execute(
                """SELECT checkout_session_id, payment_intent_id, amount_total,
                          currency, business_name, dependencies, report_focus,
                          created_at
                     FROM pilot_order_notifications
                    WHERE notified_at IS NULL AND fulfillment_state='queued'
                    ORDER BY created_at"""
            ).fetchall()
        return [PaidOrder(*row) for row in rows]

    @staticmethod
    def _upsert_payment_risk(
        database: sqlite3.Connection, risk: PaymentRisk, observed_at: int
    ) -> None:
        database.execute(
            """INSERT INTO pilot_payment_risk(
                   payment_intent_id, amount_refunded, currency,
                   disputed, under_review, observed_at
               ) VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(payment_intent_id) DO UPDATE SET
                   amount_refunded=excluded.amount_refunded,
                   currency=excluded.currency,
                   disputed=excluded.disputed,
                   under_review=excluded.under_review,
                   observed_at=excluded.observed_at""",
            (
                risk.payment_intent_id,
                risk.amount_refunded,
                risk.currency,
                int(risk.disputed),
                int(risk.under_review),
                observed_at,
            ),
        )

    @staticmethod
    def _reconcile_refund(
        database: sqlite3.Connection, risk: PaymentRisk, observed_at: int
    ) -> None:
        base_revenue = database.execute(
            "SELECT 1 FROM revenue WHERE transaction_id=?",
            (risk.payment_intent_id,),
        ).fetchone()
        if risk.amount_refunded <= 0 or base_revenue is None:
            return
        adjustment_id = f"refund:{risk.payment_intent_id}"
        values = (
            observed_at,
            -(risk.amount_refunded / 100),
            risk.currency.upper(),
            "stripe_statuspulse_refund",
            f"Refund adjustment for {risk.payment_intent_id}",
        )
        existing = database.execute(
            "SELECT 1 FROM revenue WHERE transaction_id=?", (adjustment_id,)
        ).fetchone()
        if existing is None:
            database.execute(
                """INSERT INTO revenue(
                       ts, amount, currency, source, transaction_id, note
                   ) VALUES (datetime(?, 'unixepoch'), ?, ?, ?, ?, ?)""",
                (*values[:4], adjustment_id, values[4]),
            )
        else:
            database.execute(
                """UPDATE revenue
                      SET ts=datetime(?, 'unixepoch'), amount=?, currency=?,
                          source=?, note=?
                    WHERE transaction_id=?""",
                (*values, adjustment_id),
            )
        metadata = json.dumps({"amount_refunded": risk.amount_refunded}, sort_keys=True)
        database.execute(
            """INSERT INTO funnel_events(
                   event_name, occurred_at, source_id, metadata
               ) VALUES ('refund', ?, ?, ?)
               ON CONFLICT(event_name, source_id) DO UPDATE SET
                   occurred_at=excluded.occurred_at,
                   metadata=excluded.metadata""",
            (observed_at, risk.payment_intent_id, metadata),
        )

    def record_payment_risk(self, risk: PaymentRisk, observed_at: int) -> None:
        """Persist exact refund/dispute state and reconcile known refund revenue."""
        with closing(self.connect()) as database, database:
            self._upsert_payment_risk(database, risk, observed_at)
            self._reconcile_refund(database, risk, observed_at)

    def record_review(self, notice: ReviewNotice) -> bool:
        with closing(self.connect()) as database, database:
            cursor = database.execute(
                """INSERT OR IGNORE INTO pilot_order_reviews(
                       checkout_session_id, reason, observed_at
                   ) VALUES (?, ?, ?)""",
                (notice.checkout_session_id, notice.reason, notice.observed_at),
            )
            return cursor.rowcount == 1

    def pending_reviews(self) -> list[ReviewNotice]:
        with closing(self.connect()) as database, database:
            rows = database.execute(
                """SELECT checkout_session_id, reason, observed_at
                     FROM pilot_order_reviews
                    WHERE notified_at IS NULL AND resolved_at IS NULL
                    ORDER BY observed_at, checkout_session_id"""
            ).fetchall()
        return [ReviewNotice(*row) for row in rows]

    def mark_notified(
        self, checkout_session_id: str, message_id: str, now: int
    ) -> None:
        with closing(self.connect()) as database, database:
            database.execute(
                """UPDATE pilot_order_notifications
                      SET telegram_message_id=?, notified_at=?
                    WHERE checkout_session_id=? AND notified_at IS NULL""",
                (message_id, now, checkout_session_id),
            )

    def mark_review_notified(
        self, checkout_session_id: str, message_id: str, now: int
    ) -> None:
        with closing(self.connect()) as database, database:
            database.execute(
                """UPDATE pilot_order_reviews
                      SET telegram_message_id=?, notified_at=?
                    WHERE checkout_session_id=? AND notified_at IS NULL""",
                (message_id, now, checkout_session_id),
            )

    def place_on_hold(self, checkout_session_id: str) -> bool:
        """Prevent an existing unsettled order from entering fulfillment."""
        with closing(self.connect()) as database, database:
            cursor = database.execute(
                """UPDATE pilot_order_notifications
                      SET fulfillment_state='on_hold'
                    WHERE checkout_session_id=?
                      AND fulfillment_state IN ('queued', 'in_progress')""",
                (checkout_session_id,),
            )
            return cursor.rowcount == 1

    def release_hold(self, checkout_session_id: str, resolved_at: int) -> bool:
        """Release a held order only after the caller revalidates Stripe state."""
        with closing(self.connect()) as database, database:
            cursor = database.execute(
                """UPDATE pilot_order_notifications
                      SET fulfillment_state='queued'
                    WHERE checkout_session_id=?
                      AND fulfillment_state='on_hold'
                      AND intake_purged_at IS NULL""",
                (checkout_session_id,),
            )
            database.execute(
                """UPDATE pilot_order_reviews
                      SET resolved_at=?
                    WHERE checkout_session_id=? AND resolved_at IS NULL""",
                (resolved_at, checkout_session_id),
            )
            return cursor.rowcount == 1

    def mark_delivered(self, checkout_session_id: str, delivered_at: int) -> bool:
        with closing(self.connect()) as database, database:
            cursor = database.execute(
                """UPDATE pilot_order_notifications
                      SET fulfillment_state='delivered', delivered_at=?
                    WHERE checkout_session_id=?
                      AND fulfillment_state IN ('queued', 'in_progress')""",
                (delivered_at, checkout_session_id),
            )
            if cursor.rowcount != 1:
                return False
            database.execute(
                """INSERT OR IGNORE INTO funnel_events(
                       event_name, occurred_at, source_id, metadata
                   ) VALUES ('delivery_completed', ?, ?, '{}')""",
                (delivered_at, checkout_session_id),
            )
            return True

    def purge_delivered_intake(self, now: int) -> int:
        cutoff = now - INTAKE_RETENTION_SECONDS
        with closing(self.connect()) as database, database:
            cursor = database.execute(
                """UPDATE pilot_order_notifications
                      SET business_name=NULL,
                          dependencies=NULL,
                          report_focus=NULL,
                          intake_purged_at=?
                    WHERE (
                            (fulfillment_state='delivered'
                             AND delivered_at IS NOT NULL
                             AND delivered_at <= ?)
                         OR (fulfillment_state IN ('on_hold', 'canceled')
                             AND discovered_at <= ?)
                          )
                      AND intake_purged_at IS NULL""",
                (now, cutoff, cutoff),
            )
            return cursor.rowcount


def run(
    configuration: Configuration,
    stripe: StripeClient | None = None,
    telegram: TelegramClient | None = None,
    clock: Callable[[], float] = time.time,
) -> tuple[int, int, int]:
    ledger = OrderLedger(configuration.database)
    discovered_at = int(clock())
    ledger.purge_delivered_intake(discovered_at)
    stripe_client = stripe or StripeClient(configuration.stripe_api_key)
    telegram_client = telegram or TelegramClient(
        configuration.telegram_bot_token,
        configuration.telegram_channel,
    )
    recorded = 0
    reviews = 0
    for session in stripe_client.list_paid_sessions(configuration.payment_link_id):
        if session.get("payment_status") == "unpaid":
            continue
        try:
            risk = _payment_risk(session)
            ledger.record_payment_risk(risk, discovered_at)
            order = parse_paid_order(session, configuration.payment_link_id)
        except NotifierError as error:
            session_id = session.get("id") if isinstance(session, dict) else None
            if isinstance(session_id, str) and session_id:
                ledger.place_on_hold(session_id)
                notice = ReviewNotice(session_id, str(error), discovered_at)
                if ledger.record_review(notice):
                    reviews += 1
            else:
                print("Stripe returned a session without an ID", file=sys.stderr)
            continue
        ledger.release_hold(order.checkout_session_id, discovered_at)
        if ledger.record(order, discovered_at):
            recorded += 1

    notified = 0
    for order in ledger.pending():
        message_id = telegram_client.send_order(order)
        ledger.mark_notified(order.checkout_session_id, message_id, int(clock()))
        notified += 1
    for notice in ledger.pending_reviews():
        message_id = telegram_client.send_review(notice)
        ledger.mark_review_notified(
            notice.checkout_session_id, message_id, int(clock())
        )
        notified += 1
    return recorded, reviews, notified


def parse_arguments(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=False)
    subparsers.add_parser("poll", help="poll Stripe and notify the owner")
    delivered = subparsers.add_parser(
        "mark-delivered", help="record delivery and start the intake retention window"
    )
    delivered.add_argument("checkout_session_id")
    return parser.parse_args(arguments)


def main(arguments: Sequence[str] | None = None) -> int:
    try:
        options = parse_arguments(arguments)
        configuration = load_configuration()
        if options.command == "mark-delivered":
            if not re.fullmatch(
                r"cs_(?:live|test)_[A-Za-z0-9]+", options.checkout_session_id
            ):
                raise NotifierError("invalid Checkout Session ID")
            stripe = StripeClient(configuration.stripe_api_key)
            session = stripe.retrieve_session(options.checkout_session_id)
            parse_paid_order(session, configuration.payment_link_id)
            ledger = OrderLedger(configuration.database)
            now = int(time.time())
            ledger.release_hold(options.checkout_session_id, now)
            if not ledger.mark_delivered(options.checkout_session_id, now):
                raise NotifierError("order is missing or is not deliverable")
            print(f"marked {options.checkout_session_id} delivered")
            return 0
        recorded, reviews, notified = run(configuration)
    except NotifierError as error:
        print(f"order notifier failed: {error}", file=sys.stderr)
        return 1
    print(
        "order notifier complete: "
        f"recorded={recorded}, reviews={reviews}, notified={notified}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

import json
import sqlite3
import stat
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path

from order_notifier import (
    INTAKE_RETENTION_SECONDS,
    Configuration,
    NotifierError,
    OrderLedger,
    StripeClient,
    parse_paid_order,
    run,
)

PAYMENT_LINK = "plink_fixture123"


def payment_intent(**charge_overrides):
    charge = {
        "id": "ch_live_fixture",
        "object": "charge",
        "paid": True,
        "status": "succeeded",
        "currency": "usd",
        "amount": 8374,
        "amount_refunded": 0,
        "disputed": False,
        "refunded": False,
    }
    charge.update(charge_overrides)
    return {
        "id": "pi_live_fixture",
        "object": "payment_intent",
        "status": "succeeded",
        "currency": "usd",
        "amount_received": 8374,
        "latest_charge": charge,
    }


def session(**overrides):
    value = {
        "id": "cs_live_fixture",
        "object": "checkout.session",
        "payment_link": PAYMENT_LINK,
        "mode": "payment",
        "livemode": True,
        "status": "complete",
        "payment_status": "paid",
        "metadata": {
            "offer_id": "vendor_reliability_pilot",
            "offer_version": "1",
        },
        "currency": "usd",
        "amount_subtotal": 7900,
        "amount_total": 8374,
        "automatic_tax": {"enabled": True, "status": "complete"},
        "tax_id_collection": {"enabled": True},
        "billing_address_collection": "required",
        "total_details": {
            "amount_discount": 0,
            "amount_shipping": 0,
            "amount_tax": 474,
        },
        "consent": {"terms_of_service": "accepted"},
        "payment_intent": payment_intent(),
        "created": 1_800_000_000,
        "collected_information": {"business_name": "Fixture LLC"},
        "customer_details": {
            "email": "buyer@example.com",
            "address": {"country": "US"},
        },
        "custom_fields": [
            {
                "key": "dependencies",
                "type": "text",
                "text": {"value": "GitHub, Cloudflare"},
            },
            {
                "key": "reportfocus",
                "type": "dropdown",
                "dropdown": {"value": "renewal"},
            },
        ],
    }
    value.update(overrides)
    return value


class FakeStripe:
    def __init__(self, sessions):
        self.sessions = sessions

    def list_paid_sessions(self, payment_link_id):
        self.payment_link_id = payment_link_id
        return self.sessions


class FakeTelegram:
    def __init__(self):
        self.orders = []
        self.reviews = []

    def send_order(self, order):
        self.orders.append(order)
        return f"message-{len(self.orders)}"

    def send_review(self, notice):
        self.reviews.append(notice)
        return f"review-{len(self.reviews)}"


class FailingStripe:
    def list_paid_sessions(self, payment_link_id):
        raise NotifierError("fixture Stripe outage")


class JSONResponse:
    def __init__(self, value, url):
        self.body = json.dumps(value).encode()
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def geturl(self):
        return self.url

    def read(self, limit):
        return self.body[:limit]


class OrderNotifierTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = Path(self.tempdir.name) / "ops.db"
        self.configuration = Configuration(
            stripe_api_key="rk_live_fixture",
            payment_link_id=PAYMENT_LINK,
            telegram_bot_token="123:fixture",
            telegram_channel="123",
            database=self.database,
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def test_valid_paid_order_is_recorded_and_notified_once(self):
        stripe = FakeStripe([session()])
        telegram = FakeTelegram()

        self.assertEqual(
            (1, 0, 1),
            run(self.configuration, stripe, telegram, clock=lambda: 1_800_000_100),
        )
        self.assertEqual(
            (0, 0, 0),
            run(self.configuration, stripe, telegram, clock=lambda: 1_800_000_200),
        )
        self.assertEqual(1, len(telegram.orders))
        with closing(sqlite3.connect(self.database)) as database, database:
            revenue = database.execute(
                "SELECT amount, currency, transaction_id FROM revenue"
            ).fetchall()
            events = database.execute(
                "SELECT event_name, source_id FROM funnel_events"
            ).fetchall()
        self.assertEqual([(83.74, "USD", "pi_live_fixture")], revenue)
        self.assertEqual([("payment_succeeded", "cs_live_fixture")], events)

    def test_wrong_offer_or_amount_is_rejected(self):
        with self.assertRaisesRegex(NotifierError, "metadata"):
            parse_paid_order(session(metadata={"offer_id": "other"}), PAYMENT_LINK)
        with self.assertRaisesRegex(NotifierError, "subtotal"):
            parse_paid_order(session(amount_subtotal=7800), PAYMENT_LINK)
        with self.assertRaisesRegex(NotifierError, "automatic tax"):
            parse_paid_order(
                session(automatic_tax={"enabled": True, "status": "failed"}),
                PAYMENT_LINK,
            )
        with self.assertRaisesRegex(NotifierError, "tax ID collection"):
            parse_paid_order(
                session(tax_id_collection={"enabled": False}),
                PAYMENT_LINK,
            )
        with self.assertRaisesRegex(NotifierError, "customer email"):
            parse_paid_order(
                session(customer_details={"address": {"country": "US"}}),
                PAYMENT_LINK,
            )
        with self.assertRaisesRegex(NotifierError, "safely settled"):
            parse_paid_order(
                session(payment_intent=payment_intent(disputed=True)),
                PAYMENT_LINK,
            )

    def test_oversized_dependency_scope_is_rejected(self):
        dependencies = ",".join(f"vendor{number}" for number in range(21))
        modified = session(
            custom_fields=[
                {
                    "key": "dependencies",
                    "type": "text",
                    "text": {"value": dependencies},
                }
            ]
        )

        with self.assertRaisesRegex(NotifierError, "outside"):
            parse_paid_order(modified, PAYMENT_LINK)

    def test_invalid_expected_order_is_durably_alerted_once(self):
        stripe = FakeStripe([session(amount_subtotal=7800)])
        telegram = FakeTelegram()

        self.assertEqual(
            (0, 1, 1),
            run(self.configuration, stripe, telegram, clock=lambda: 1_800_000_100),
        )
        self.assertEqual(
            (0, 0, 0),
            run(self.configuration, stripe, telegram, clock=lambda: 1_800_000_200),
        )
        self.assertEqual(1, len(telegram.reviews))
        self.assertIn("subtotal", telegram.reviews[0].reason)
        with closing(sqlite3.connect(self.database)) as database, database:
            reviews = database.execute(
                """SELECT checkout_session_id, notified_at
                     FROM pilot_order_reviews"""
            ).fetchall()
        self.assertEqual([("cs_live_fixture", 1_800_000_100)], reviews)

    def test_unpaid_async_session_waits_without_false_review(self):
        stripe = FakeStripe([session(payment_status="unpaid")])
        telegram = FakeTelegram()

        self.assertEqual(
            (0, 0, 0),
            run(self.configuration, stripe, telegram, clock=lambda: 1_800_000_100),
        )
        self.assertEqual([], telegram.orders)
        self.assertEqual([], telegram.reviews)

    def test_later_refund_places_queued_order_on_hold(self):
        telegram = FakeTelegram()
        run(
            self.configuration,
            FakeStripe([session()]),
            telegram,
            clock=lambda: 1_800_000_100,
        )
        refunded_payment = payment_intent(
            amount_refunded=8374,
            refunded=True,
        )

        self.assertEqual(
            (0, 1, 1),
            run(
                self.configuration,
                FakeStripe([session(payment_intent=refunded_payment)]),
                telegram,
                clock=lambda: 1_800_000_200,
            ),
        )
        ledger = OrderLedger(self.database)
        self.assertFalse(ledger.mark_delivered("cs_live_fixture", 1_800_000_300))
        with closing(sqlite3.connect(self.database)) as database:
            state = database.execute(
                "SELECT fulfillment_state FROM pilot_order_notifications"
            ).fetchone()[0]
            revenue = database.execute(
                "SELECT amount, source FROM revenue ORDER BY id"
            ).fetchall()
            risk = database.execute(
                """SELECT amount_refunded, disputed, under_review
                     FROM pilot_payment_risk"""
            ).fetchone()
        self.assertEqual("on_hold", state)
        self.assertEqual(
            [
                (83.74, "stripe_statuspulse_pilot"),
                (-83.74, "stripe_statuspulse_refund"),
            ],
            revenue,
        )
        self.assertEqual((8374, 0, 0), risk)

        self.assertEqual(
            1,
            ledger.purge_delivered_intake(1_800_000_100 + INTAKE_RETENTION_SECONDS),
        )

    def test_manual_review_places_existing_order_on_hold(self):
        telegram = FakeTelegram()
        run(
            self.configuration,
            FakeStripe([session()]),
            telegram,
            clock=lambda: 1_800_000_100,
        )
        reviewed_payment = payment_intent(review="prv_live_fixture")

        self.assertEqual(
            (0, 1, 1),
            run(
                self.configuration,
                FakeStripe([session(payment_intent=reviewed_payment)]),
                telegram,
                clock=lambda: 1_800_000_200,
            ),
        )
        with closing(sqlite3.connect(self.database)) as database:
            state = database.execute(
                "SELECT fulfillment_state FROM pilot_order_notifications"
            ).fetchone()[0]
            under_review = database.execute(
                "SELECT under_review FROM pilot_payment_risk"
            ).fetchone()[0]
        self.assertEqual("on_hold", state)
        self.assertEqual(1, under_review)

        manual_outcome = payment_intent(outcome={"type": "manual_review"})
        with self.assertRaisesRegex(NotifierError, "safely settled"):
            parse_paid_order(
                session(payment_intent=manual_outcome),
                PAYMENT_LINK,
            )

        self.assertEqual(
            (0, 0, 0),
            run(
                self.configuration,
                FakeStripe([session()]),
                telegram,
                clock=lambda: 1_800_000_300,
            ),
        )
        with closing(sqlite3.connect(self.database)) as database:
            state = database.execute(
                "SELECT fulfillment_state FROM pilot_order_notifications"
            ).fetchone()[0]
            resolved_at = database.execute(
                "SELECT resolved_at FROM pilot_order_reviews"
            ).fetchone()[0]
        self.assertEqual("queued", state)
        self.assertEqual(1_800_000_300, resolved_at)

    def test_conflicting_immutable_order_is_rejected_atomically(self):
        ledger = OrderLedger(self.database)
        order = parse_paid_order(session(), PAYMENT_LINK)
        self.assertTrue(ledger.record(order, 1_800_000_100))

        with self.assertRaisesRegex(NotifierError, "conflicting immutable"):
            ledger.record(
                replace(order, dependencies="Different vendor"), 1_800_000_200
            )

        with closing(sqlite3.connect(self.database)) as database:
            self.assertEqual(
                1,
                database.execute(
                    "SELECT COUNT(*) FROM pilot_order_notifications"
                ).fetchone()[0],
            )

    def test_private_ledger_permissions_are_enforced(self):
        OrderLedger(self.database)

        self.assertEqual(0o600, stat.S_IMODE(self.database.stat().st_mode))

    def test_stripe_pagination_rejects_repeated_cursor(self):
        calls = 0

        def opener(request, timeout):
            nonlocal calls
            calls += 1
            return JSONResponse(
                {"data": [{"id": "cs_live_repeat"}], "has_more": True},
                request.full_url,
            )

        client = StripeClient(
            "rk_live_fixture",
            opener=opener,
            sleeper=lambda _: None,
        )

        with self.assertRaisesRegex(NotifierError, "cursor repeated"):
            client.list_paid_sessions(PAYMENT_LINK)
        self.assertEqual(2, calls)

    def test_delivery_starts_retention_window_and_purges_intake(self):
        stripe = FakeStripe([session()])
        telegram = FakeTelegram()
        run(self.configuration, stripe, telegram, clock=lambda: 1_800_000_100)
        ledger = OrderLedger(self.database)

        self.assertTrue(ledger.mark_delivered("cs_live_fixture", 1_800_000_200))
        self.assertEqual(
            0,
            ledger.purge_delivered_intake(1_800_000_200 + INTAKE_RETENTION_SECONDS - 1),
        )
        self.assertEqual(
            1,
            ledger.purge_delivered_intake(1_800_000_200 + INTAKE_RETENTION_SECONDS),
        )
        with closing(sqlite3.connect(self.database)) as database, database:
            order = database.execute(
                """SELECT fulfillment_state, business_name, dependencies,
                          report_focus, intake_purged_at
                     FROM pilot_order_notifications"""
            ).fetchone()
            events = database.execute(
                "SELECT event_name FROM funnel_events ORDER BY event_name"
            ).fetchall()
        self.assertEqual("delivered", order[0])
        self.assertIsNone(order[1])
        self.assertIsNone(order[2])
        self.assertIsNone(order[3])
        self.assertIsNotNone(order[4])
        self.assertEqual([("delivery_completed",), ("payment_succeeded",)], events)

    def test_overdue_intake_purges_before_network_failure(self):
        run(
            self.configuration,
            FakeStripe([session()]),
            FakeTelegram(),
            clock=lambda: 1_800_000_100,
        )
        ledger = OrderLedger(self.database)
        self.assertTrue(ledger.mark_delivered("cs_live_fixture", 1_800_000_200))

        with self.assertRaisesRegex(NotifierError, "fixture Stripe outage"):
            run(
                self.configuration,
                FailingStripe(),
                FakeTelegram(),
                clock=lambda: 1_800_000_200 + INTAKE_RETENTION_SECONDS,
            )

        with closing(sqlite3.connect(self.database)) as database:
            values = database.execute(
                """SELECT business_name, dependencies, report_focus
                     FROM pilot_order_notifications"""
            ).fetchone()
        self.assertEqual((None, None, None), values)


if __name__ == "__main__":
    unittest.main()

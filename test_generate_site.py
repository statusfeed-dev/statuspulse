import csv
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import generate_site


class GenerateSiteTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = Path(self.tempdir.name) / "statusfeed.db"
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("""CREATE TABLE incidents(
                id TEXT PRIMARY KEY,
                source TEXT,
                name TEXT,
                status TEXT,
                impact TEXT,
                started_at TEXT,
                resolved_at TEXT,
                fetched_at TEXT,
                source_url TEXT,
                details TEXT
            )""")
            db.executemany(
                "INSERT INTO incidents VALUES (?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        "fixture:active",
                        "fixture",
                        "Active incident",
                        "investigating",
                        "major",
                        "2026-08-26T10:00:00Z",
                        None,
                        "2026-08-27T10:00:00Z",
                        "https://status.example/incidents/active",
                        '{"private_detail":"not in public sample"}',
                    ),
                    (
                        "fixture:postmortem",
                        "fixture",
                        "Published postmortem",
                        "postmortem",
                        "minor",
                        "2026-08-25T10:00:00Z",
                        None,
                        "2026-08-27T10:00:00Z",
                        "https://status.example/incidents/postmortem",
                        "{}",
                    ),
                    (
                        "fixture:stale",
                        "fixture",
                        "Stale active label",
                        "monitoring",
                        "minor",
                        "2026-01-01T10:00:00Z",
                        None,
                        "2026-08-27T10:00:00Z",
                        "https://status.example/incidents/stale",
                        "{}",
                    ),
                ],
            )
        self.rows = generate_site.load_rows(self.database)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_active_count_excludes_postmortems_and_stale_records(self):
        metrics = generate_site.compute_metrics(
            self.rows,
            datetime(2026, 8, 27, 12, tzinfo=timezone.utc),
        )
        self.assertEqual(["fixture:active"], [row["id"] for row in metrics["active"]])

    def test_public_sample_includes_provenance_but_not_raw_details(self):
        sample = Path(self.tempdir.name) / "sample.csv"
        generate_site.write_sample(self.rows, sample)

        with sample.open(newline="", encoding="utf-8") as source:
            reader = csv.DictReader(source)
            records = list(reader)
        self.assertIn("source_url", reader.fieldnames)
        self.assertNotIn("details", reader.fieldnames)
        self.assertTrue(records[0]["source_url"].startswith("https://status.example/"))

    def test_public_sample_neutralizes_spreadsheet_formulas(self):
        with closing(sqlite3.connect(self.database)) as database, database:
            database.execute(
                "UPDATE incidents SET name=? WHERE id=?",
                ('=HYPERLINK("https://malicious.example")', "fixture:active"),
            )
        rows = generate_site.load_rows(self.database)
        sample = Path(self.tempdir.name) / "formula-safe.csv"

        generate_site.write_sample(rows, sample)

        with sample.open(newline="", encoding="utf-8") as source:
            records = list(csv.DictReader(source))
        formula_row = next(row for row in records if row["id"] == "fixture:active")
        self.assertTrue(formula_row["name"].startswith("'="))

    def test_page_has_one_fixed_price_offer_and_no_subscription_copy(self):
        now = datetime(2026, 8, 27, 12, tzinfo=timezone.utc)
        page = generate_site.render_page(
            self.rows,
            generate_site.compute_metrics(self.rows, now),
            now,
            pilot_link="https://buy.stripe.com/fixture",
        )
        self.assertIn("$79 fixed-scope pilot", page)
        self.assertIn("One payment. No subscription.", page)
        self.assertIn("source-policy-gated", page)
        self.assertIn("Policy-gated normalized incidents", page)
        self.assertIn("limited to US customers", page)
        self.assertEqual(2, page.count("https://buy.stripe.com/fixture"))
        self.assertNotIn("rights-cleared", page.lower())
        self.assertNotIn("commercial use is documented", page.lower())
        self.assertNotIn("$3/month", page)
        self.assertNotIn("$5/month", page)
        self.assertNotIn("$9/month", page)

    def test_pilot_link_validation_rejects_noncanonical_or_scriptable_urls(self):
        self.assertEqual(
            "https://buy.stripe.com/fixture_123",
            generate_site.configured_pilot_link(
                {"STATUSPULSE_PILOT_URL": "https://buy.stripe.com/fixture_123"}
            ),
        )
        malicious = "https://buy.stripe.com/fixture?x=</script><script>alert(1)"
        self.assertEqual(
            generate_site.PILOT_LINK_FALLBACK,
            generate_site.configured_pilot_link({"STATUSPULSE_PILOT_URL": malicious}),
        )

        now = datetime(2026, 8, 27, 12, tzinfo=timezone.utc)
        page = generate_site.render_page(
            self.rows,
            generate_site.compute_metrics(self.rows, now),
            now,
            pilot_link=malicious,
        )
        self.assertNotIn(malicious, page)
        self.assertEqual(1, page.count("</script>"))
        self.assertIn("Checkout temporarily unavailable", page)
        self.assertNotIn("Order the pilot", page)
        self.assertNotIn('"@type":"Offer"', page)
        self.assertNotIn(generate_site.PILOT_LINK_FALLBACK, page)


if __name__ == "__main__":
    unittest.main()

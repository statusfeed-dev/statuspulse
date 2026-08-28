"""Tests for the policy-gated incident collector."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path

import collect
from source_policy import (
    DEFAULT_POLICY_PATH,
    SourcePolicyError,
    load_policy,
    sanitize_database,
)


class CollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.database = self.root / "statusfeed.db"
        self.policy = load_policy()
        self.atlassian_url = self.policy.rule("atlassian").endpoint
        self.google_url = self.policy.rule("google-cloud").endpoint

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def _atlassian_feed(
        *, status: str = "resolved", resolved_at: str | None = "2026-08-27T11:00:00Z"
    ) -> dict[str, object]:
        return {
            "incidents": [
                {
                    "id": "fixture-1",
                    "name": "Expressive outage title that must be omitted",
                    "status": status,
                    "impact": "critical",
                    "started_at": "2026-08-27T10:00:00Z",
                    "resolved_at": resolved_at,
                    "incident_updates": [
                        {"body": "Provider narrative that must never be retained"}
                    ],
                    "components": [{"name": "Expressive component name"}],
                }
            ]
        }

    @staticmethod
    def _google_feed() -> list[dict[str, object]]:
        return [
            {
                "id": "gcp-fixture-1",
                "number": "123456",
                "begin": "2026-08-26T10:00:00+00:00",
                "end": "2026-08-26T11:00:00+00:00",
                "external_desc": "Provider narrative that must never be retained",
                "updates": [{"text": "Raw update narrative"}],
                "affected_products": [
                    {"id": "stable-product", "title": "Display name omitted"}
                ],
                "currently_affected_locations": [],
                "previously_affected_locations": [
                    {"id": "us-east1", "title": "Display name omitted"}
                ],
            }
        ]

    def _successful_fetcher(self, url: str) -> object:
        if url == self.atlassian_url:
            return self._atlassian_feed()
        if url == self.google_url:
            return self._google_feed()
        self.fail(f"collector attempted an unapproved URL: {url}")

    def test_policy_is_fail_closed_and_expires_on_review_date(self) -> None:
        manifest = json.loads(DEFAULT_POLICY_PATH.read_text(encoding="utf-8"))
        manifest["sources"]["github"]["state"] = "enabled"
        changed = self.root / "changed-policy.json"
        changed.write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaisesRegex(SourcePolicyError, "differs from reviewed"):
            load_policy(changed)
        with self.assertRaisesRegex(SourcePolicyError, "expired"):
            load_policy(DEFAULT_POLICY_PATH, as_of=date(2026, 11, 27))

    def test_policy_rejects_broadened_enabled_source_fields(self) -> None:
        manifest = json.loads(DEFAULT_POLICY_PATH.read_text(encoding="utf-8"))
        manifest["sources"]["google-cloud"]["details_fields"].append("external_desc")
        changed = self.root / "broadened-policy.json"
        changed.write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaisesRegex(SourcePolicyError, "detail fields differ"):
            load_policy(changed)

        manifest = json.loads(DEFAULT_POLICY_PATH.read_text(encoding="utf-8"))
        manifest["required_public_disclosures"]["analysis"] = "Unreviewed rights claim"
        changed.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(SourcePolicyError, "disclosures differ"):
            load_policy(changed)

        manifest = json.loads(DEFAULT_POLICY_PATH.read_text(encoding="utf-8"))
        manifest["next_review_on"] = "2027-08-27"
        changed.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(SourcePolicyError, "interval exceeds"):
            load_policy(changed)

    def test_collector_never_calls_pending_or_disabled_sources(self) -> None:
        calls: list[str] = []

        def fetcher(url: str) -> object:
            calls.append(url)
            return self._successful_fetcher(url)

        collect.collect(fetcher=fetcher, db_path=self.database)

        self.assertEqual(calls, sorted([self.atlassian_url, self.google_url]))
        self.assertNotIn("githubstatus.com", " ".join(calls))
        self.assertNotIn("status.openai.com", " ".join(calls))

    def test_collection_omits_narratives_and_uses_exact_citations(self) -> None:
        collect.collect(fetcher=self._successful_fetcher, db_path=self.database)

        with closing(sqlite3.connect(self.database)) as database:
            rows = database.execute(
                "SELECT id, name, impact, source_url, details FROM incidents ORDER BY id"
            ).fetchall()
        serialized = json.dumps(rows)
        self.assertNotIn("Expressive", serialized)
        self.assertNotIn("Provider narrative", serialized)
        self.assertNotIn("Raw update", serialized)
        self.assertEqual(rows[0][1], "Atlassian incident fixture-1")
        self.assertIsNone(rows[0][2])
        self.assertEqual(rows[0][3], "https://status.atlassian.com/incidents/fixture-1")
        google_details = json.loads(rows[1][4])
        self.assertEqual(
            google_details,
            {
                "affected_location_ids": ["us-east1"],
                "affected_product_ids": ["stable-product"],
                "provider_number": "123456",
            },
        )
        self.assertEqual(
            rows[1][3],
            "https://status.cloud.google.com/incidents/gcp-fixture-1",
        )

    def test_postmortem_is_normalized_to_resolved(self) -> None:
        def fetcher(url: str) -> object:
            if url == self.atlassian_url:
                return self._atlassian_feed(status="postmortem")
            return self._google_feed()

        collect.collect(fetcher=fetcher, db_path=self.database)

        with closing(sqlite3.connect(self.database)) as database:
            status = database.execute(
                "SELECT status FROM incidents WHERE id='atlassian:fixture-1'"
            ).fetchone()[0]
        self.assertEqual(status, "resolved")

    def test_unknown_status_still_fails_closed(self) -> None:
        def fetcher(url: str) -> object:
            if url == self.atlassian_url:
                return self._atlassian_feed(status="unexpected")
            return self._google_feed()

        with self.assertRaisesRegex(RuntimeError, "source quorum failed"):
            collect.collect(fetcher=fetcher, db_path=self.database)

    def test_empty_enabled_source_still_fails_closed(self) -> None:
        def fetcher(url: str) -> object:
            if url == self.atlassian_url:
                return {"incidents": []}
            return self._google_feed()

        with self.assertRaisesRegex(RuntimeError, "source quorum failed"):
            collect.collect(fetcher=fetcher, db_path=self.database)

        with closing(sqlite3.connect(self.database)) as database:
            count = database.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
        self.assertEqual(count, 0)

    def test_retry_delay_rejects_negative_and_non_finite_values(self) -> None:
        self.assertEqual(0.0, collect.HTTPJSONFetcher._retry_delay("-5", 1))
        self.assertEqual(1.0, collect.HTTPJSONFetcher._retry_delay("nan", 1))

    def test_sanitize_deletes_gated_rows_and_rewrites_enabled_rows(self) -> None:
        with closing(collect.init_db(self.database)) as database:
            rows = [
                (
                    "github:unsafe",
                    "github",
                    "GitHub narrative",
                    "resolved",
                    "major",
                    "2026-08-25T00:00:00+00:00",
                    "2026-08-25T01:00:00+00:00",
                    "2026-08-27T00:00:00+00:00",
                    "https://www.githubstatus.com/api/v2/incidents.json",
                    '{"raw":"narrative"}',
                ),
                (
                    "atlassian:safe-id",
                    "atlassian",
                    "Raw Atlassian title",
                    "resolved",
                    "major",
                    "2026-08-25T00:00:00+00:00",
                    "2026-08-25T01:00:00+00:00",
                    "2026-08-27T00:00:00+00:00",
                    "https://status.atlassian.com/api/v2/incidents.json",
                    '{"page_url":"raw","update_count":2}',
                ),
                (
                    "gcp:gcp-id",
                    "google-cloud",
                    "Raw Google description",
                    "resolved",
                    "major",
                    "2026-08-24T00:00:00+00:00",
                    "2026-08-24T01:00:00+00:00",
                    "2026-08-27T00:00:00+00:00",
                    "https://status.cloud.google.com/incidents.json",
                    json.dumps(
                        {
                            "number": "42",
                            "external_desc": "must disappear",
                            "products": [{"id": "product-id", "title": "omit"}],
                            "locations": [{"id": "region-id", "title": "omit"}],
                        }
                    ),
                ),
            ]
            database.executemany(
                "INSERT INTO incidents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows
            )
            database.commit()

        result = sanitize_database(self.database)

        self.assertEqual(result.before, 3)
        self.assertEqual(result.retained, 2)
        self.assertEqual(result.deleted_gated, 1)
        with closing(sqlite3.connect(self.database)) as database:
            rows = database.execute(
                "SELECT source, name, impact, source_url, details "
                "FROM incidents ORDER BY source"
            ).fetchall()
            google_id = database.execute(
                "SELECT id FROM incidents WHERE source='google-cloud'"
            ).fetchone()[0]
        self.assertEqual([row[0] for row in rows], ["atlassian", "google-cloud"])
        self.assertEqual(google_id, "google-cloud:gcp-id")
        self.assertEqual(rows[0][1], "Atlassian incident safe-id")
        self.assertIsNone(rows[0][2])
        self.assertIsNone(rows[0][4])
        self.assertNotIn("external_desc", rows[1][4])
        self.assertEqual(
            json.loads(rows[1][4]),
            {
                "affected_location_ids": ["region-id"],
                "affected_product_ids": ["product-id"],
                "provider_number": "42",
            },
        )

    def test_refresh_requires_all_enabled_sources_and_rolls_back(self) -> None:
        def fetcher(url: str) -> object:
            if url == self.atlassian_url:
                raise OSError("fixture outage")
            return self._successful_fetcher(url)

        with self.assertRaisesRegex(RuntimeError, "source quorum failed"):
            collect.collect(fetcher=fetcher, db_path=self.database)

        with closing(sqlite3.connect(self.database)) as database:
            count = database.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
        self.assertEqual(count, 0)

    def test_caller_cannot_weaken_the_enabled_source_quorum(self) -> None:
        with self.assertRaisesRegex(ValueError, "all enabled sources"):
            collect.collect(
                fetcher=self._successful_fetcher,
                db_path=self.database,
                minimum_successes=1,
            )

    def test_resolution_update_does_not_duplicate_incident(self) -> None:
        state = {"status": "investigating", "resolved_at": None}

        def fetcher(url: str) -> object:
            if url == self.atlassian_url:
                return self._atlassian_feed(**state)
            return self._google_feed()

        collect.collect(fetcher=fetcher, db_path=self.database)
        state.update(status="resolved", resolved_at="2026-08-27T11:00:00Z")
        collect.collect(fetcher=fetcher, db_path=self.database)

        with closing(sqlite3.connect(self.database)) as database:
            rows = database.execute(
                "SELECT id, status, resolved_at FROM incidents WHERE source='atlassian'"
            ).fetchall()
        self.assertEqual(
            [("atlassian:fixture-1", "resolved", "2026-08-27T11:00:00Z")],
            rows,
        )


if __name__ == "__main__":
    unittest.main()

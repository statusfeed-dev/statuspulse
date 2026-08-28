"""Tests for the private release export and publication contract."""

from __future__ import annotations

import csv
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from export_release import export_release
from generate_site import load_rows, write_sample
from release_contract import (
    INCIDENT_COLUMNS,
    PUBLIC_FILES,
    SAMPLE_COLUMNS,
    ReleaseValidationError,
    inspect_database,
    safe_csv_cell,
    stage_public_artifacts,
    validate_csv_rows,
    validate_public_artifacts,
    validate_release,
)


class ReleaseToolsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.database = self.root / "statusfeed.db"
        self.rows = [
            (
                "atlassian:2",
                "atlassian",
                "Atlassian incident 2",
                "investigating",
                None,
                "2026-08-27T02:00:00+00:00",
                None,
                "2026-08-27T03:00:00+00:00",
                "https://status.atlassian.com/incidents/2",
                None,
            ),
            (
                "google-cloud:1",
                "google-cloud",
                "Google Cloud incident 1",
                "resolved",
                None,
                "2026-08-26T01:00:00+00:00",
                "2026-08-26T02:00:00+00:00",
                "2026-08-27T03:00:00+00:00",
                "https://status.cloud.google.com/incidents/1",
                '{"affected_location_ids":[],"affected_product_ids":[],"provider_number":"1"}',
            ),
        ]
        self._create_database(self.rows)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _create_database(self, rows: list[tuple[object, ...]]) -> None:
        with closing(sqlite3.connect(self.database)) as database:
            database.execute(
                """
                CREATE TABLE incidents(
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
                )
                """
            )
            database.executemany(
                "INSERT INTO incidents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            database.commit()

    def _write_stats(self, path: Path, *, total: int = 2) -> None:
        value = {
            "total": total,
            "last30d": total,
            "coverage_start": "2026-08-26T01:00:00+00:00",
            "coverage_end": "2026-08-27T02:00:00+00:00",
            "computed_at": "2026-08-27T04:00:00+00:00",
            "by_source": {"atlassian": 1, "google-cloud": 1},
            "unresolved": 1,
            "active_reports_30d": 1,
        }
        path.write_text(json.dumps(value), encoding="utf-8")

    def _write_mttr_stats(self, path: Path) -> None:
        value = {
            "google-cloud": {"median_mttr_hrs": 1.0, "n": 1},
        }
        path.write_text(json.dumps(value), encoding="utf-8")

    def _write_sample(self, path: Path) -> None:
        with path.open("w", newline="", encoding="utf-8") as output:
            writer = csv.writer(output)
            writer.writerow(SAMPLE_COLUMNS)
            for row in self.rows:
                writer.writerow(row[: len(SAMPLE_COLUMNS)])

    def _write_public_directory(self, directory: Path) -> None:
        directory.mkdir()
        for name in PUBLIC_FILES:
            (directory / name).write_text("fixture\n", encoding="utf-8")
        self._write_stats(directory / "stats.json")
        self._write_mttr_stats(directory / "mttr_stats.json")
        self._write_sample(directory / "statuspulse-sample.csv")

    def test_inspect_database_reports_validated_counts(self) -> None:
        summary = inspect_database(self.database, minimum_rows=2, minimum_sources=2)

        self.assertEqual(summary.row_count, 2)
        self.assertEqual(summary.source_count, 2)
        self.assertEqual(summary.unresolved_count, 1)
        self.assertEqual(dict(summary.by_source), {"atlassian": 1, "google-cloud": 1})

    def test_inspect_database_rejects_row_regression(self) -> None:
        with self.assertRaisesRegex(ReleaseValidationError, "below minimum"):
            inspect_database(self.database, minimum_rows=3)

    def test_inspect_database_rejects_non_database_file(self) -> None:
        invalid = self.root / "invalid.db"
        invalid.write_text("not sqlite", encoding="utf-8")

        with self.assertRaisesRegex(ReleaseValidationError, "invalid SQLite"):
            inspect_database(invalid)

    def test_inspect_database_rejects_gated_source(self) -> None:
        with closing(sqlite3.connect(self.database)) as database:
            database.execute(
                "UPDATE incidents SET source='github' WHERE id='atlassian:2'"
            )
            database.commit()

        with self.assertRaisesRegex(ReleaseValidationError, "source policy"):
            inspect_database(self.database)

    def test_inspect_database_rejects_resolution_before_start(self) -> None:
        with closing(sqlite3.connect(self.database)) as database:
            database.execute(
                """UPDATE incidents
                      SET resolved_at='2026-08-26T00:00:00+00:00'
                    WHERE id='google-cloud:1'"""
            )
            database.commit()

        with self.assertRaisesRegex(ReleaseValidationError, "precedes"):
            inspect_database(self.database)

    def test_inspect_database_requires_every_enabled_source(self) -> None:
        with closing(sqlite3.connect(self.database)) as database:
            database.execute("DELETE FROM incidents WHERE source='google-cloud'")
            database.commit()

        with self.assertRaisesRegex(ReleaseValidationError, "source set differs"):
            inspect_database(self.database)

    def test_export_release_is_deterministic_and_complete(self) -> None:
        destination = self.root / "release" / "statuspulse.csv"

        exported = export_release(self.database, destination)

        self.assertEqual(exported, 2)
        with destination.open(newline="", encoding="utf-8") as source:
            rows = list(csv.reader(source))
        self.assertEqual(tuple(rows[0]), INCIDENT_COLUMNS)
        self.assertEqual(
            [row[0] for row in rows[1:]],
            ["atlassian:2", "google-cloud:1"],
        )
        self.assertEqual(list(destination.parent.glob("*.tmp")), [])

    def test_export_release_rejects_retained_provider_narrative(self) -> None:
        with closing(sqlite3.connect(self.database)) as database:
            database.execute(
                "UPDATE incidents SET details=? WHERE id='atlassian:2'",
                ('{"incident_update":"provider narrative"}',),
            )
            database.commit()
        destination = self.root / "unsafe.csv"

        with self.assertRaisesRegex(ReleaseValidationError, "must not retain"):
            export_release(self.database, destination)

        self.assertFalse(destination.exists())

    def test_sample_tie_breaker_is_stable_at_row_limit(self) -> None:
        tied_rows = [
            (
                f"atlassian:{index:03d}",
                "atlassian",
                f"Atlassian incident {index:03d}",
                "resolved",
                None,
                "2026-08-27T02:00:00+00:00",
                "2026-08-27T03:00:00+00:00",
                "2026-08-27T04:00:00+00:00",
                f"https://status.atlassian.com/incidents/{index:03d}",
                None,
            )
            for index in reversed(range(101))
        ]
        with closing(sqlite3.connect(self.database)) as database:
            database.execute("DELETE FROM incidents")
            database.executemany(
                "INSERT INTO incidents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                tied_rows,
            )
            database.commit()

        sample = self.root / "tied-sample.csv"
        write_sample(load_rows(self.database), sample)
        columns = ", ".join(SAMPLE_COLUMNS)
        with closing(sqlite3.connect(self.database)) as database:
            expected_rows = database.execute(
                f"SELECT {columns} FROM incidents "
                "ORDER BY started_at DESC, id ASC LIMIT 100"
            )
            self.assertEqual(
                validate_csv_rows(sample, SAMPLE_COLUMNS, expected_rows), 100
            )
        with sample.open(newline="", encoding="utf-8") as source:
            sample_ids = [row["id"] for row in csv.DictReader(source)]
        self.assertEqual(sample_ids[-1], "atlassian:099")
        self.assertNotIn("atlassian:100", sample_ids)

    def test_validate_release_cross_checks_private_and_public_data(self) -> None:
        stats = self.root / "stats.json"
        sample = self.root / "sample.csv"
        full_csv = self.root / "full.csv"
        self._write_stats(stats)
        self._write_mttr_stats(self.root / "mttr_stats.json")
        self._write_sample(sample)
        export_release(self.database, full_csv)

        summary = validate_release(
            database_path=self.database,
            stats_path=stats,
            mttr_stats_path=self.root / "mttr_stats.json",
            sample_path=sample,
            full_csv_path=full_csv,
            minimum_rows=2,
            minimum_sources=2,
        )

        self.assertEqual(summary.row_count, 2)

    def test_validate_release_rejects_stats_mismatch(self) -> None:
        stats = self.root / "stats.json"
        sample = self.root / "sample.csv"
        full_csv = self.root / "full.csv"
        self._write_stats(stats, total=3)
        self._write_mttr_stats(self.root / "mttr_stats.json")
        self._write_sample(sample)
        export_release(self.database, full_csv)

        with self.assertRaises(ReleaseValidationError):
            validate_release(
                database_path=self.database,
                stats_path=stats,
                mttr_stats_path=self.root / "mttr_stats.json",
                sample_path=sample,
                full_csv_path=full_csv,
                minimum_rows=2,
                minimum_sources=2,
            )

    def test_validate_release_rejects_equal_length_csv_tampering(self) -> None:
        stats = self.root / "stats.json"
        sample = self.root / "sample.csv"
        full_csv = self.root / "full.csv"
        self._write_stats(stats)
        self._write_mttr_stats(self.root / "mttr_stats.json")
        self._write_sample(sample)
        export_release(self.database, full_csv)
        content = full_csv.read_text(encoding="utf-8")
        full_csv.write_text(
            content.replace("Atlassian incident 2", "Tampered content", 1),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ReleaseValidationError, "content mismatch"):
            validate_release(
                database_path=self.database,
                stats_path=stats,
                mttr_stats_path=self.root / "mttr_stats.json",
                sample_path=sample,
                full_csv_path=full_csv,
                minimum_rows=2,
                minimum_sources=2,
            )

    def test_validate_release_rejects_false_activity_metrics(self) -> None:
        stats = self.root / "stats.json"
        sample = self.root / "sample.csv"
        full_csv = self.root / "full.csv"
        self._write_stats(stats)
        value = json.loads(stats.read_text(encoding="utf-8"))
        value["active_reports_30d"] = 0
        value["unresolved"] = 0
        stats.write_text(json.dumps(value), encoding="utf-8")
        self._write_mttr_stats(self.root / "mttr_stats.json")
        self._write_sample(sample)
        export_release(self.database, full_csv)

        with self.assertRaisesRegex(ReleaseValidationError, "does not match"):
            validate_release(
                database_path=self.database,
                stats_path=stats,
                mttr_stats_path=self.root / "mttr_stats.json",
                sample_path=sample,
                full_csv_path=full_csv,
                minimum_rows=2,
                minimum_sources=2,
            )

    def test_validate_release_rejects_false_mttr_metrics(self) -> None:
        stats = self.root / "stats.json"
        sample = self.root / "sample.csv"
        full_csv = self.root / "full.csv"
        self._write_stats(stats)
        (self.root / "mttr_stats.json").write_text(
            '{"google-cloud":{"median_mttr_hrs":999999,"n":999}}',
            encoding="utf-8",
        )
        self._write_sample(sample)
        export_release(self.database, full_csv)

        with self.assertRaisesRegex(ReleaseValidationError, "do not match"):
            validate_release(
                database_path=self.database,
                stats_path=stats,
                mttr_stats_path=self.root / "mttr_stats.json",
                sample_path=sample,
                full_csv_path=full_csv,
                minimum_rows=2,
                minimum_sources=2,
            )

    def test_safe_csv_cell_neutralizes_formula_cells(self) -> None:
        self.assertEqual(
            safe_csv_cell('=HYPERLINK("https://malicious.example")'),
            '\'=HYPERLINK("https://malicious.example")',
        )

    def test_validate_public_artifacts_rejects_non_finite_mttr(self) -> None:
        public = self.root / "public-non-finite"
        self._write_public_directory(public)
        (public / "mttr_stats.json").write_text(
            '{"google-cloud":{"median_mttr_hrs":NaN,"n":1}}',
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ReleaseValidationError, "non-finite"):
            validate_public_artifacts(public)

    def test_validate_public_artifacts_rejects_formula_cells(self) -> None:
        public = self.root / "public-formula"
        self._write_public_directory(public)
        sample = public / "statuspulse-sample.csv"
        sample.write_text(
            sample.read_text(encoding="utf-8").replace(
                "Atlassian incident 2",
                '=HYPERLINK("https://malicious.example")',
                1,
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ReleaseValidationError, "formula risk"):
            validate_public_artifacts(public)

    def test_validate_public_artifacts_enforces_exact_allowlist(self) -> None:
        public = self.root / "public"
        self._write_public_directory(public)

        self.assertEqual(validate_public_artifacts(public), (2, 2))

        (public / "statusfeed.db").write_bytes(b"private")
        with self.assertRaisesRegex(ReleaseValidationError, "allowlist mismatch"):
            validate_public_artifacts(public)

    def test_stage_public_artifacts_copies_only_allowlisted_files(self) -> None:
        source = self.root / "source"
        destination = self.root / "staged"
        self._write_public_directory(source)
        (source / "statusfeed.db").write_bytes(b"private")

        result = stage_public_artifacts(source, destination)

        self.assertEqual(result, (2, 2))
        self.assertEqual(
            {path.name for path in destination.iterdir()}, set(PUBLIC_FILES)
        )
        self.assertFalse((destination / "statusfeed.db").exists())


if __name__ == "__main__":
    unittest.main()

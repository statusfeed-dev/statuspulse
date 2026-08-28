"""Shared release schema and validation primitives for StatusPulse."""

from __future__ import annotations

import csv
import json
import math
import shutil
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from itertools import zip_longest
from pathlib import Path
from statistics import median
from typing import Any

from source_policy import (
    SourcePolicy,
    SourcePolicyError,
    load_policy,
    validate_incident_record,
    validate_public_incident_record,
)

INCIDENT_COLUMNS = (
    "id",
    "source",
    "name",
    "status",
    "impact",
    "started_at",
    "resolved_at",
    "fetched_at",
    "source_url",
    "details",
)
SAMPLE_COLUMNS = INCIDENT_COLUMNS[:8] + ("source_url",)
ACTIVE_STATUSES = frozenset({"investigating", "identified", "monitoring"})
CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
PUBLIC_FILES = frozenset(
    {
        "favicon.svg",
        "index.html",
        "mttr_stats.json",
        "privacy.html",
        "refunds.html",
        "robots.txt",
        "sitemap.xml",
        "stats.json",
        "statuspulse-sample.csv",
        "support.html",
        "terms.html",
        "thanks.html",
        "vendor-incident-history.html",
    }
)


class ReleaseValidationError(ValueError):
    """Raised when a database or generated artifact violates the release contract."""


@dataclass(frozen=True)
class DatabaseSummary:
    """Validated facts used to compare private and public release artifacts."""

    row_count: int
    source_count: int
    unresolved_count: int
    by_source: tuple[tuple[str, int], ...]


def safe_csv_cell(value: object) -> str:
    """Return a spreadsheet-safe textual representation of one CSV cell."""
    text = "" if value is None else str(value)
    stripped = text.lstrip(" \t\r\n")
    if text.startswith(("\t", "\r")) or stripped.startswith(CSV_FORMULA_PREFIXES[:4]):
        return "'" + text
    return text


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def _read_only_connection(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise ReleaseValidationError(f"database does not exist: {path}")
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def _require_incident_table(database: sqlite3.Connection) -> None:
    """Require a healthy database with the complete incident schema."""
    quick_check = [row[0] for row in database.execute("PRAGMA quick_check")]
    if quick_check != ["ok"]:
        raise ReleaseValidationError(f"SQLite quick_check failed: {quick_check!r}")

    table = database.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='incidents'"
    ).fetchone()
    if table is None:
        raise ReleaseValidationError("database is missing incidents table")

    columns = {row[1] for row in database.execute("PRAGMA table_info(incidents)")}
    missing_columns = sorted(set(INCIDENT_COLUMNS) - columns)
    if missing_columns:
        raise ReleaseValidationError(
            "incidents table is missing columns: " + ", ".join(missing_columns)
        )


def _release_policy() -> SourcePolicy:
    """Load the source policy with release-contract error semantics."""
    try:
        return load_policy()
    except SourcePolicyError as error:
        raise ReleaseValidationError(f"source policy is invalid: {error}") from error


def _validate_incident_rows(database: sqlite3.Connection, policy: SourcePolicy) -> None:
    """Validate every incident row against the active source policy."""
    incident_columns = ", ".join(INCIDENT_COLUMNS)
    rows = database.execute(f"SELECT {incident_columns} FROM incidents ORDER BY id")
    for line_number, row in enumerate(rows, start=1):
        try:
            validate_incident_record(
                dict(zip(INCIDENT_COLUMNS, row, strict=True)), policy=policy
            )
        except SourcePolicyError as error:
            raise ReleaseValidationError(
                f"incident row {line_number} violates source policy: {error}"
            ) from error


def _summarize_database(
    database: sqlite3.Connection,
    policy: SourcePolicy,
    minimum_rows: int,
    minimum_sources: int,
) -> DatabaseSummary:
    """Calculate release facts and enforce policy/quorum constraints."""
    row_count = database.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
    source_rows = database.execute(
        """
        SELECT source, COUNT(*)
        FROM incidents
        WHERE source IS NOT NULL AND source != ''
        GROUP BY source
        ORDER BY source
        """
    ).fetchall()
    unresolved_count = database.execute(
        """
        SELECT COUNT(*)
        FROM incidents
        WHERE status IS NULL OR status != 'resolved'
        """
    ).fetchone()[0]

    observed_sources = {str(source) for source, _ in source_rows}
    expected_sources = set(policy.enabled_sources)
    if observed_sources != expected_sources:
        raise ReleaseValidationError(
            "database source set differs from policy; "
            f"expected={sorted(expected_sources)}, "
            f"observed={sorted(observed_sources)}"
        )
    if row_count < minimum_rows:
        raise ReleaseValidationError(
            f"database row count {row_count} is below minimum {minimum_rows}"
        )
    source_count = len(source_rows)
    if source_count < minimum_sources:
        raise ReleaseValidationError(
            f"database source count {source_count} is below minimum {minimum_sources}"
        )
    return DatabaseSummary(
        row_count=row_count,
        source_count=source_count,
        unresolved_count=unresolved_count,
        by_source=tuple((str(source), count) for source, count in source_rows),
    )


def inspect_connection(
    database: sqlite3.Connection,
    *,
    minimum_rows: int = 1,
    minimum_sources: int = 1,
) -> DatabaseSummary:
    """Validate an open SQLite snapshot and return its release facts."""
    if minimum_rows < 0 or minimum_sources < 0:
        raise ValueError("minimum constraints must be non-negative")
    _require_incident_table(database)
    policy = _release_policy()
    _validate_incident_rows(database, policy)
    return _summarize_database(database, policy, minimum_rows, minimum_sources)


def inspect_database(
    path: Path,
    *,
    minimum_rows: int = 1,
    minimum_sources: int = 1,
) -> DatabaseSummary:
    """Validate the private SQLite database and return its release facts."""
    try:
        with closing(_read_only_connection(path)) as database:
            database.execute("BEGIN")
            return inspect_connection(
                database,
                minimum_rows=minimum_rows,
                minimum_sources=minimum_sources,
            )
    except sqlite3.Error as error:
        raise ReleaseValidationError(f"invalid SQLite database: {error}") from error


def count_csv_rows(path: Path, expected_columns: Iterable[str]) -> int:
    """Count CSV rows while enforcing the exact header and row shape."""
    columns = tuple(expected_columns)
    if not path.is_file():
        raise ReleaseValidationError(f"CSV does not exist: {path}")

    try:
        with path.open(newline="", encoding="utf-8") as source:
            reader = csv.DictReader(source)
            if tuple(reader.fieldnames or ()) != columns:
                raise ReleaseValidationError(
                    f"unexpected CSV header in {path}: {reader.fieldnames!r}"
                )
            row_count = 0
            for line_number, row in enumerate(reader, start=2):
                if None in row or any(value is None for value in row.values()):
                    raise ReleaseValidationError(
                        f"malformed CSV row at {path}:{line_number}"
                    )
                unsafe_fields = [
                    name for name, value in row.items() if safe_csv_cell(value) != value
                ]
                if unsafe_fields:
                    raise ReleaseValidationError(
                        f"spreadsheet formula risk at {path}:{line_number} "
                        f"in fields {unsafe_fields}"
                    )
                if columns == SAMPLE_COLUMNS:
                    try:
                        validate_public_incident_record(row)
                    except SourcePolicyError as error:
                        raise ReleaseValidationError(
                            f"source-policy violation at {path}:{line_number}: {error}"
                        ) from error
                row_count += 1
            return row_count
    except (OSError, csv.Error) as error:
        raise ReleaseValidationError(f"cannot read CSV {path}: {error}") from error


def validate_csv_rows(
    path: Path,
    expected_columns: Iterable[str],
    expected_rows: Iterable[Iterable[object]],
) -> int:
    """Compare a CSV exactly with spreadsheet-safe rows from one DB snapshot."""
    columns = tuple(expected_columns)
    if not path.is_file():
        raise ReleaseValidationError(f"CSV does not exist: {path}")
    missing = object()
    row_count = 0
    try:
        with path.open(newline="", encoding="utf-8") as source:
            reader = csv.reader(source)
            header = next(reader, None)
            if tuple(header or ()) != columns:
                raise ReleaseValidationError(
                    f"unexpected CSV header in {path}: {header!r}"
                )
            for line_number, pair in enumerate(
                zip_longest(reader, expected_rows, fillvalue=missing), start=2
            ):
                actual_row, expected_row = pair
                if actual_row is missing:
                    raise ReleaseValidationError(
                        f"CSV {path} is missing expected row at line {line_number}"
                    )
                if expected_row is missing:
                    raise ReleaseValidationError(
                        f"CSV {path} has an unexpected row at line {line_number}"
                    )
                expected_cells = [safe_csv_cell(value) for value in expected_row]
                if actual_row != expected_cells:
                    raise ReleaseValidationError(
                        f"CSV content mismatch at {path}:{line_number}"
                    )
                row_count += 1
    except (OSError, csv.Error) as error:
        raise ReleaseValidationError(f"cannot read CSV {path}: {error}") from error
    return row_count


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _derived_metrics(
    database: sqlite3.Connection, as_of: datetime
) -> tuple[dict[str, Any], dict[str, dict[str, int | float]]]:
    rows = database.execute(
        "SELECT source, status, started_at, resolved_at FROM incidents"
    ).fetchall()
    cutoff = as_of - timedelta(days=30)
    recent_count = 0
    active_count = 0
    durations: dict[str, list[float]] = {}
    started_values: list[str] = []

    for source, status, started_at, resolved_at in rows:
        started = _parse_datetime(started_at)
        resolved = _parse_datetime(resolved_at)
        if isinstance(started_at, str) and started_at:
            started_values.append(started_at)
        if started is not None and started >= cutoff:
            recent_count += 1
            if str(status or "").lower() in ACTIVE_STATUSES:
                active_count += 1
        if started is not None and resolved is not None:
            duration = max(0.0, (resolved - started).total_seconds() / 3600)
            durations.setdefault(str(source), []).append(duration)

    mttr = {
        source: {
            "median_mttr_hrs": round(median(values), 2),
            "n": len(values),
        }
        for source, values in sorted(durations.items())
        if values
    }
    metrics: dict[str, Any] = {
        "last30d": recent_count,
        "active_reports_30d": active_count,
        "unresolved": active_count,
        "coverage_start": min(started_values, default=None),
        "coverage_end": max(started_values, default=None),
    }
    return metrics, mttr


def _read_stats_object(path: Path) -> dict[str, Any]:
    """Read a finite-number JSON object from the stats path."""
    if not path.is_file():
        raise ReleaseValidationError(f"stats file does not exist: {path}")
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (OSError, ValueError) as error:
        message = f"cannot read stats JSON {path}: {error}"
        raise ReleaseValidationError(message) from error
    if not isinstance(value, dict):
        raise ReleaseValidationError("stats JSON must be an object")
    return value


def _validate_stats_counts(value: dict[str, Any]) -> None:
    """Validate non-negative counts and their arithmetic invariants."""
    count_fields = ("total", "last30d", "active_reports_30d", "unresolved")
    for field in count_fields:
        number = value.get(field)
        if isinstance(number, bool) or not isinstance(number, int) or number < 0:
            raise ReleaseValidationError(
                f"stats field {field!r} must be a non-negative integer"
            )
    if value["last30d"] > value["total"]:
        raise ReleaseValidationError("stats last30d exceeds total")
    if value["unresolved"] > value["total"]:
        raise ReleaseValidationError("stats unresolved exceeds total")
    if value["active_reports_30d"] > value["last30d"]:
        raise ReleaseValidationError("stats active_reports_30d exceeds last30d")
    if value["unresolved"] != value["active_reports_30d"]:
        raise ReleaseValidationError("stats unresolved must equal active_reports_30d")


def _validate_stats_sources(value: dict[str, Any]) -> None:
    """Require exactly the source-policy providers and consistent totals."""
    by_source = value.get("by_source")
    if not isinstance(by_source, dict) or not by_source:
        raise ReleaseValidationError("stats by_source must be a non-empty object")
    enabled_sources = set(_release_policy().enabled_sources)
    observed_sources = set(by_source)
    if observed_sources != enabled_sources:
        raise ReleaseValidationError(
            "stats source set differs from policy; "
            f"expected={sorted(enabled_sources)}, "
            f"observed={sorted(observed_sources)}"
        )
    for source, count in by_source.items():
        if not isinstance(source, str) or not source:
            raise ReleaseValidationError("stats by_source has an invalid source name")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ReleaseValidationError(
                f"stats count for {source!r} must be a non-negative integer"
            )
    if sum(by_source.values()) != value["total"]:
        raise ReleaseValidationError("stats by_source counts do not sum to total")


def _validate_stats_timestamps(value: dict[str, Any]) -> None:
    """Validate the generation instant and optional ordered coverage range."""
    computed_at = value.get("computed_at")
    if not isinstance(computed_at, str):
        raise ReleaseValidationError("stats computed_at must be an ISO 8601 string")
    try:
        parsed = datetime.fromisoformat(computed_at.replace("Z", "+00:00"))
    except ValueError as error:
        message = "stats computed_at is not valid ISO 8601"
        raise ReleaseValidationError(message) from error
    if parsed.tzinfo is None:
        raise ReleaseValidationError("stats computed_at must include a timezone")

    coverage: dict[str, datetime | None] = {}
    for field in ("coverage_start", "coverage_end"):
        raw_timestamp = value.get(field)
        if raw_timestamp is None:
            coverage[field] = None
            continue
        timestamp = _parse_datetime(raw_timestamp)
        if timestamp is None:
            raise ReleaseValidationError(
                f"stats {field} must be a timezone-aware ISO 8601 timestamp"
            )
        coverage[field] = timestamp
    start, end = coverage["coverage_start"], coverage["coverage_end"]
    if start is not None and end is not None and start > end:
        raise ReleaseValidationError("stats coverage range is reversed")


def read_stats(path: Path) -> dict[str, Any]:
    """Read and validate the shape and internal arithmetic of public stats."""
    value = _read_stats_object(path)
    _validate_stats_counts(value)
    _validate_stats_sources(value)
    _validate_stats_timestamps(value)
    return value


def read_mttr_stats(path: Path, expected_sources: set[str]) -> dict[str, Any]:
    """Validate generated MTTR statistics and their provider references."""
    if not path.is_file():
        raise ReleaseValidationError(f"MTTR stats file does not exist: {path}")
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (OSError, ValueError) as error:
        message = f"cannot read MTTR stats JSON {path}: {error}"
        raise ReleaseValidationError(message) from error
    if not isinstance(value, dict) or not value:
        raise ReleaseValidationError("MTTR stats JSON must be a non-empty object")
    unknown_sources = sorted(set(value) - expected_sources)
    if unknown_sources:
        raise ReleaseValidationError(
            f"MTTR stats contains unknown sources: {unknown_sources}"
        )
    for source, metrics in value.items():
        if not isinstance(source, str) or not source:
            raise ReleaseValidationError("MTTR stats has an invalid source name")
        if not isinstance(metrics, dict):
            raise ReleaseValidationError(f"MTTR stats for {source!r} must be an object")
        median = metrics.get("median_mttr_hrs")
        sample_size = metrics.get("n")
        if (
            isinstance(median, bool)
            or not isinstance(median, (int, float))
            or not math.isfinite(median)
            or median < 0
        ):
            raise ReleaseValidationError(
                f"MTTR median for {source!r} must be non-negative"
            )
        if (
            isinstance(sample_size, bool)
            or not isinstance(sample_size, int)
            or sample_size < 1
        ):
            raise ReleaseValidationError(
                f"MTTR sample size for {source!r} must be a positive integer"
            )
    return value


def validate_public_artifacts(directory: Path) -> tuple[int, int]:
    """Ensure a Pages staging directory contains only approved public files."""
    if not directory.is_dir():
        raise ReleaseValidationError(f"public directory does not exist: {directory}")

    symlinks = [path for path in directory.rglob("*") if path.is_symlink()]
    if symlinks:
        raise ReleaseValidationError(
            "public directory contains symlinks: "
            + ", ".join(str(path.relative_to(directory)) for path in symlinks)
        )

    actual_files = {
        path.relative_to(directory).as_posix()
        for path in directory.rglob("*")
        if path.is_file()
    }
    if actual_files != PUBLIC_FILES:
        missing = sorted(PUBLIC_FILES - actual_files)
        extra = sorted(actual_files - PUBLIC_FILES)
        raise ReleaseValidationError(
            f"public artifact allowlist mismatch; missing={missing}, extra={extra}"
        )

    stats = read_stats(directory / "stats.json")
    read_mttr_stats(directory / "mttr_stats.json", set(stats["by_source"]))
    sample_row_count = count_csv_rows(
        directory / "statuspulse-sample.csv", SAMPLE_COLUMNS
    )
    expected_sample_rows = min(100, stats["total"])
    if sample_row_count != expected_sample_rows:
        raise ReleaseValidationError(
            f"sample row count {sample_row_count} does not equal expected "
            f"{expected_sample_rows}"
        )
    return stats["total"], sample_row_count


def stage_public_artifacts(source: Path, destination: Path) -> tuple[int, int]:
    """Copy the approved Pages files into a new, validated staging directory."""
    if not source.is_dir():
        raise ReleaseValidationError(f"public source does not exist: {source}")
    try:
        destination.mkdir(parents=True, exist_ok=False)
        for relative_name in sorted(PUBLIC_FILES):
            source_path = source / relative_name
            if source_path.is_symlink() or not source_path.is_file():
                raise ReleaseValidationError(
                    f"approved public source is missing or unsafe: {source_path}"
                )
            destination_path = destination / relative_name
            shutil.copyfile(source_path, destination_path)
            destination_path.chmod(0o644)
    except OSError as error:
        raise ReleaseValidationError(
            f"cannot stage public artifacts in {destination}: {error}"
        ) from error
    return validate_public_artifacts(destination)


def _validate_derived_metrics(
    database: sqlite3.Connection,
    summary: DatabaseSummary,
    stats: dict[str, Any],
    mttr_stats: dict[str, Any],
    as_of: datetime,
) -> None:
    """Cross-check JSON metrics with values derived from the DB snapshot."""
    derived_stats, derived_mttr = _derived_metrics(database, as_of)
    expected_stats: dict[str, Any] = {
        "total": summary.row_count,
        "by_source": dict(summary.by_source),
        **derived_stats,
    }
    for field, expected_value in expected_stats.items():
        if stats.get(field) != expected_value:
            raise ReleaseValidationError(
                f"stats field {field!r} does not match database; "
                f"expected {expected_value!r}, got {stats.get(field)!r}"
            )
    if mttr_stats != derived_mttr:
        raise ReleaseValidationError("MTTR stats do not match database")


def _validate_sample_csv(
    database: sqlite3.Connection, sample_path: Path, expected_rows: int
) -> None:
    """Cross-check the public sample with the first 100 canonical rows."""
    sample_columns = ", ".join(SAMPLE_COLUMNS)
    sample_rows = database.execute(
        f"SELECT {sample_columns} FROM incidents "
        "ORDER BY started_at DESC, id ASC LIMIT 100"
    )
    sample_row_count = validate_csv_rows(sample_path, SAMPLE_COLUMNS, sample_rows)
    if sample_row_count != expected_rows:
        raise ReleaseValidationError(
            f"sample row count {sample_row_count} does not equal "
            f"expected {expected_rows}"
        )


def _validate_full_csv(
    database: sqlite3.Connection, full_csv_path: Path, expected_rows: int
) -> None:
    """Cross-check the private full export with every canonical row."""
    full_columns = ", ".join(INCIDENT_COLUMNS)
    full_rows = database.execute(
        f"SELECT {full_columns} FROM incidents ORDER BY started_at DESC, id ASC"
    )
    full_row_count = validate_csv_rows(full_csv_path, INCIDENT_COLUMNS, full_rows)
    if full_row_count != expected_rows:
        raise ReleaseValidationError(
            f"full CSV row count {full_row_count} does not equal "
            f"database row count {expected_rows}"
        )


def validate_release(
    *,
    database_path: Path,
    stats_path: Path,
    mttr_stats_path: Path,
    sample_path: Path,
    full_csv_path: Path,
    minimum_rows: int,
    minimum_sources: int,
) -> DatabaseSummary:
    """Cross-check all generated artifacts against the canonical database."""
    stats = read_stats(stats_path)
    mttr_stats = read_mttr_stats(mttr_stats_path, set(stats["by_source"]))
    as_of = _parse_datetime(stats["computed_at"])
    if as_of is None:
        raise ReleaseValidationError("stats computed_at is not a valid timestamp")

    try:
        with closing(_read_only_connection(database_path)) as database:
            database.execute("BEGIN")
            summary = inspect_connection(
                database,
                minimum_rows=minimum_rows,
                minimum_sources=minimum_sources,
            )
            _validate_derived_metrics(database, summary, stats, mttr_stats, as_of)
            _validate_sample_csv(database, sample_path, min(100, summary.row_count))
            _validate_full_csv(database, full_csv_path, summary.row_count)
            return summary
    except sqlite3.Error as error:
        raise ReleaseValidationError(f"invalid SQLite database: {error}") from error

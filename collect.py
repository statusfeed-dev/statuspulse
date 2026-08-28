#!/usr/bin/env python3
"""Collect policy-approved factual incident records into private SQLite."""

from __future__ import annotations

import datetime
import email.utils
import json
import math
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from release_contract import INCIDENT_COLUMNS
from source_policy import (
    SourcePolicy,
    SourcePolicyError,
    load_policy,
    sanitize_database,
    validate_incident_record,
)

BASE = Path(__file__).resolve().parent
DB = BASE / "statusfeed.db"
UA = "StatusPulse-collector/1.0 (+https://statusfeed-dev.github.io/statuspulse/)"
MAX_RESPONSE_BYTES = 20 * 1024 * 1024
MAX_RETRY_DELAY_SECONDS = 60.0
RETRYABLE_HTTP_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

Incident = tuple[
    str,
    str,
    str,
    str,
    None,
    str,
    str | None,
    str,
    str,
    str | None,
]
InjectedFetcher = Callable[[str], Any]
Normalizer = Callable[[SourcePolicy, Any, str], Iterable[Incident]]


class CollectionError(RuntimeError):
    """Raised when an approved source cannot be fetched or normalized safely."""


@dataclass(frozen=True)
class FetchResult:
    """Decoded response plus conditional-request metadata."""

    data: Any | None
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False


def _decode_json_response(response: Any, expected_url: str) -> FetchResult:
    if response.geturl() != expected_url:
        raise CollectionError("source endpoint redirected unexpectedly")
    status = getattr(response, "status", response.getcode())
    if status == 304:
        return FetchResult(data=None, not_modified=True)
    if status != 200:
        raise CollectionError(f"unexpected HTTP status {status}")
    content_type = response.headers.get_content_type()
    if content_type not in {"application/json", "text/json"}:
        raise CollectionError(f"unexpected response content type {content_type!r}")
    raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise CollectionError("JSON response exceeds size limit")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CollectionError("response is not valid UTF-8 JSON") from error
    return FetchResult(
        data=data,
        etag=response.headers.get("ETag"),
        last_modified=response.headers.get("Last-Modified"),
    )


class HTTPJSONFetcher:
    """Bounded JSON fetcher with conditional requests and polite retry handling."""

    def __init__(
        self,
        *,
        attempts: int = 3,
        timeout_seconds: float = 30.0,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if attempts < 1:
            raise ValueError("attempts must be positive")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.attempts = attempts
        self.timeout_seconds = timeout_seconds
        self.sleeper = sleeper

    @staticmethod
    def _retry_delay(value: str | None, attempt: int) -> float:
        if value:
            try:
                seconds = float(value)
            except ValueError:
                try:
                    retry_at = email.utils.parsedate_to_datetime(value)
                except (TypeError, ValueError):
                    retry_at = None
                if retry_at is not None:
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=datetime.timezone.utc)
                    seconds = max(
                        0.0,
                        (
                            retry_at - datetime.datetime.now(datetime.timezone.utc)
                        ).total_seconds(),
                    )
                else:
                    seconds = float(2 ** (attempt - 1))
        else:
            seconds = float(2 ** (attempt - 1))
        if not math.isfinite(seconds):
            seconds = float(2 ** (attempt - 1))
        return min(max(0.0, seconds), MAX_RETRY_DELAY_SECONDS)

    def fetch(
        self,
        url: str,
        *,
        conditional_headers: Mapping[str, str] | None = None,
    ) -> FetchResult:
        """Fetch one JSON document, retrying only transient failures."""
        headers = {"Accept": "application/json", "User-Agent": UA}
        headers.update(conditional_headers or {})
        last_error: Exception | None = None
        for attempt in range(1, self.attempts + 1):
            request = urllib.request.Request(url, headers=headers, method="GET")
            try:
                with urllib.request.urlopen(
                    request, timeout=self.timeout_seconds
                ) as response:
                    return _decode_json_response(response, url)
            except urllib.error.HTTPError as error:
                if error.code == 304:
                    return FetchResult(data=None, not_modified=True)
                last_error = error
                if error.code not in RETRYABLE_HTTP_STATUS or attempt == self.attempts:
                    break
                self.sleeper(
                    self._retry_delay(error.headers.get("Retry-After"), attempt)
                )
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                last_error = error
                if attempt == self.attempts:
                    break
                self.sleeper(self._retry_delay(None, attempt))
        raise CollectionError(
            f"request failed after {self.attempts} attempts: {last_error}"
        )


def init_db(db_path: Path = DB) -> sqlite3.Connection:
    """Create or migrate the private collector tables."""
    database = sqlite3.connect(db_path)
    try:
        database.execute(
            """
            CREATE TABLE IF NOT EXISTS incidents(
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                name TEXT NOT NULL,
                status TEXT NOT NULL,
                impact TEXT,
                started_at TEXT NOT NULL,
                resolved_at TEXT,
                fetched_at TEXT NOT NULL,
                source_url TEXT NOT NULL,
                details TEXT
            )
            """
        )
        columns = {row[1] for row in database.execute("PRAGMA table_info(incidents)")}
        for name, kind in (("source_url", "TEXT"), ("details", "TEXT")):
            if name not in columns:
                database.execute(f"ALTER TABLE incidents ADD COLUMN {name} {kind}")
        database.execute(
            """
            CREATE TABLE IF NOT EXISTS source_fetch_state(
                source TEXT PRIMARY KEY,
                endpoint TEXT NOT NULL,
                etag TEXT,
                last_modified TEXT,
                checked_at TEXT NOT NULL
            )
            """
        )
        database.commit()
    except BaseException:
        database.close()
        raise
    return database


def _normalize_status(value: object) -> str:
    status = str(value or "").strip().lower()
    if status == "postmortem":
        return "resolved"
    if status not in {"investigating", "identified", "monitoring", "resolved"}:
        raise CollectionError(f"unknown incident status {value!r}")
    return status


def normalize_atlassian(
    policy: SourcePolicy, data: Any, now: str
) -> Iterable[Incident]:
    """Retain only approved factual fields from Atlassian's documented API."""
    if not isinstance(data, dict) or not isinstance(data.get("incidents"), list):
        raise CollectionError("Atlassian response has an unexpected shape")
    rule = policy.rule("atlassian")
    for incident in data["incidents"]:
        if not isinstance(incident, dict):
            raise CollectionError("Atlassian response contains a non-object incident")
        provider_id = incident.get("id")
        if not isinstance(provider_id, str) or not provider_id:
            raise CollectionError("Atlassian incident is missing an ID")
        status = _normalize_status(incident.get("status"))
        started_at = incident.get("started_at") or incident.get("created_at")
        if not isinstance(started_at, str) or not started_at:
            raise CollectionError("Atlassian incident is missing its start time")
        resolved_at = incident.get("resolved_at") if status == "resolved" else None
        if resolved_at is not None and not isinstance(resolved_at, str):
            raise CollectionError("Atlassian incident has an invalid end time")
        yield (
            f"atlassian:{provider_id}",
            "atlassian",
            f"{rule.provider} incident {provider_id}",
            status,
            None,
            started_at,
            resolved_at,
            now,
            rule.permalink(provider_id),
            None,
        )


def _stable_ids(*values: object) -> list[str]:
    identifiers: set[str] = set()
    for value in values:
        if not isinstance(value, list):
            continue
        for item in value:
            identifier = item.get("id") if isinstance(item, dict) else None
            if isinstance(identifier, str) and identifier:
                identifiers.add(identifier)
    return sorted(identifiers)


def normalize_google_cloud(
    policy: SourcePolicy, data: Any, now: str
) -> Iterable[Incident]:
    """Retain only stable IDs and factual timestamps from Google's JSON history."""
    if not isinstance(data, list):
        raise CollectionError("Google Cloud response has an unexpected shape")
    rule = policy.rule("google-cloud")
    for incident in data:
        if not isinstance(incident, dict):
            raise CollectionError("Google Cloud response has a non-object incident")
        provider_id = incident.get("id")
        started_at = incident.get("begin")
        if not isinstance(provider_id, str) or not provider_id:
            raise CollectionError("Google Cloud incident is missing an ID")
        if not isinstance(started_at, str) or not started_at:
            raise CollectionError("Google Cloud incident is missing its start time")
        resolved_at = incident.get("end")
        if resolved_at is not None and not isinstance(resolved_at, str):
            raise CollectionError("Google Cloud incident has an invalid end time")
        details: dict[str, object] = {
            "affected_location_ids": _stable_ids(
                incident.get("currently_affected_locations"),
                incident.get("previously_affected_locations"),
            ),
            "affected_product_ids": _stable_ids(incident.get("affected_products")),
        }
        number = incident.get("number")
        if isinstance(number, (str, int)) and str(number).isdigit():
            details["provider_number"] = str(number)
        yield (
            f"google-cloud:{provider_id}",
            "google-cloud",
            f"{rule.provider} incident {provider_id}",
            "resolved" if resolved_at else "investigating",
            None,
            started_at,
            resolved_at,
            now,
            rule.permalink(provider_id),
            json.dumps(details, separators=(",", ":"), sort_keys=True),
        )


NORMALIZERS: Mapping[str, Normalizer] = {
    "atlassian": normalize_atlassian,
    "google-cloud": normalize_google_cloud,
}


def _source_specs(policy: SourcePolicy) -> list[tuple[str, str, Normalizer]]:
    enabled = set(policy.enabled_sources)
    if enabled != set(NORMALIZERS):
        raise SourcePolicyError(
            "collector implementation and policy enabled-source set differ"
        )
    specs: list[tuple[str, str, Normalizer]] = []
    for source in policy.enabled_sources:
        rule = policy.rule(source)
        if rule.endpoint is None:
            raise SourcePolicyError(f"enabled source {source!r} has no endpoint")
        specs.append((source, rule.endpoint, NORMALIZERS[source]))
    return specs


def _conditional_headers(database: sqlite3.Connection, source: str) -> dict[str, str]:
    if not _has_source_rows(database, source):
        return {}
    row = database.execute(
        "SELECT etag, last_modified FROM source_fetch_state WHERE source=?",
        (source,),
    ).fetchone()
    if row is None:
        return {}
    headers: dict[str, str] = {}
    if row[0]:
        headers["If-None-Match"] = row[0]
    if row[1]:
        headers["If-Modified-Since"] = row[1]
    return headers


def _has_source_rows(database: sqlite3.Connection, source: str) -> bool:
    return (
        database.execute(
            "SELECT 1 FROM incidents WHERE source=? LIMIT 1", (source,)
        ).fetchone()
        is not None
    )


def _update_fetch_state(
    database: sqlite3.Connection,
    source: str,
    endpoint: str,
    result: FetchResult,
    now: str,
) -> None:
    previous = database.execute(
        "SELECT etag, last_modified FROM source_fetch_state WHERE source=?",
        (source,),
    ).fetchone()
    etag = result.etag or (previous[0] if previous else None)
    modified = result.last_modified or (previous[1] if previous else None)
    database.execute(
        """
        INSERT INTO source_fetch_state(source, endpoint, etag, last_modified, checked_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(source) DO UPDATE SET
            endpoint=excluded.endpoint,
            etag=excluded.etag,
            last_modified=excluded.last_modified,
            checked_at=excluded.checked_at
        """,
        (source, endpoint, etag, modified, now),
    )


def upsert_incident(database: sqlite3.Connection, row: Incident) -> bool:
    """Insert or refresh one policy-validated incident by provider ID."""
    existing = database.execute(
        "SELECT 1 FROM incidents WHERE id=?", (row[0],)
    ).fetchone()
    database.execute(
        """
        INSERT INTO incidents(
            id, source, name, status, impact, started_at, resolved_at,
            fetched_at, source_url, details
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            source=excluded.source,
            name=excluded.name,
            status=excluded.status,
            impact=excluded.impact,
            started_at=excluded.started_at,
            resolved_at=excluded.resolved_at,
            fetched_at=excluded.fetched_at,
            source_url=excluded.source_url,
            details=excluded.details
        """,
        row,
    )
    return existing is None


@dataclass(frozen=True)
class CollectionRun:
    policy: SourcePolicy
    database: sqlite3.Connection
    now: str
    injected_fetcher: InjectedFetcher | None
    http_fetcher: HTTPJSONFetcher | None

    def _fetch(self, source: str, endpoint: str) -> FetchResult:
        if self.injected_fetcher is not None:
            return FetchResult(data=self.injected_fetcher(endpoint))
        if self.http_fetcher is None:
            raise CollectionError("collector has no configured fetcher")
        return self.http_fetcher.fetch(
            endpoint,
            conditional_headers=_conditional_headers(self.database, source),
        )

    def collect_source(
        self, source: str, endpoint: str, normalizer: Normalizer
    ) -> tuple[int, int]:
        result = self._fetch(source, endpoint)
        if result.not_modified:
            if not _has_source_rows(self.database, source):
                raise CollectionError(
                    "source returned not-modified without a local baseline"
                )
            rows: list[Incident] = []
        else:
            rows = list(normalizer(self.policy, result.data, self.now))
            if not rows:
                raise CollectionError("source returned no incident records")
        inserted = 0
        for row in rows:
            validate_incident_record(
                dict(zip(INCIDENT_COLUMNS, row, strict=True)),
                policy=self.policy,
            )
            inserted += int(upsert_incident(self.database, row))
        _update_fetch_state(self.database, source, endpoint, result, self.now)
        return len(rows), inserted


@dataclass(frozen=True)
class CollectionSummary:
    total: int
    inserted: int
    successes: int
    failures: tuple[str, ...]


def _collect_sources(
    run: CollectionRun, sources: Iterable[tuple[str, str, Normalizer]]
) -> CollectionSummary:
    total = inserted = successes = 0
    failures: list[str] = []
    for source, endpoint, normalizer in sources:
        try:
            source_total, source_inserted = run.collect_source(
                source, endpoint, normalizer
            )
            total += source_total
            inserted += source_inserted
            successes += 1
        except (
            CollectionError,
            OSError,
            TypeError,
            ValueError,
            KeyError,
            json.JSONDecodeError,
        ) as error:
            failures.append(f"{source}: {error}")
            print(f"{source}: ERROR {error}", file=sys.stderr)
    return CollectionSummary(total, inserted, successes, tuple(failures))


def collect(
    fetcher: InjectedFetcher | None = None,
    db_path: Path = DB,
    minimum_successes: int | None = None,
    *,
    policy_path: Path | None = None,
) -> tuple[int, int, int]:
    """Refresh only enabled sources, committing after the approved-source quorum."""
    policy = load_policy(policy_path or BASE / "source-policy.json")
    with closing(init_db(db_path)):
        pass
    sanitize_database(db_path, policy=policy)
    database = init_db(db_path)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    sources = _source_specs(policy)
    required = len(sources)
    try:
        if minimum_successes not in (None, required):
            raise ValueError(
                "all enabled sources are required by the fail-closed policy"
            )
        run = CollectionRun(
            policy=policy,
            database=database,
            now=now,
            injected_fetcher=fetcher,
            http_fetcher=HTTPJSONFetcher() if fetcher is None else None,
        )
        summary = _collect_sources(run, sources)
        if summary.successes < required:
            database.rollback()
            raise RuntimeError(
                f"source quorum failed: {summary.successes}/{len(sources)} succeeded; "
                f"required {required}"
            )
        database.commit()
    finally:
        database.close()
    print(
        f"collected {summary.total} incidents, inserted {summary.inserted}; "
        f"sources {summary.successes}/{len(sources)}"
    )
    if summary.failures:
        print(
            f"completed with {len(summary.failures)} source error(s)",
            file=sys.stderr,
        )
    return summary.total, summary.inserted, summary.successes


def main() -> int:
    try:
        collect()
    except (RuntimeError, SourcePolicyError, ValueError) as error:
        print(f"collection failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

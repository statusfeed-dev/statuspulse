#!/usr/bin/env python3
"""Enforce the reviewed StatusPulse commercial source-rights policy."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

BASE = Path(__file__).resolve().parent
DEFAULT_POLICY_PATH = BASE / "source-policy.json"

REVIEWED_STATES = {
    "atlassian": "enabled",
    "google-cloud": "enabled",
    "github": "pending",
    "cloudflare": "pending",
    "twilio": "pending",
    "supabase": "pending",
    "datadog": "disabled",
    "vercel": "disabled",
    "digitalocean": "disabled",
    "openai": "disabled",
    "aws": "disabled",
}
REVIEWED_ENABLED_SOURCES = frozenset(
    source for source, state in REVIEWED_STATES.items() if state == "enabled"
)
REVIEWED_INTERFACES = {
    "atlassian": (
        "https://status.atlassian.com/api/v2/incidents.json",
        "https://status.atlassian.com/incidents/{incident_id}",
    ),
    "google-cloud": (
        "https://status.cloud.google.com/incidents.json",
        "https://status.cloud.google.com/incidents/{incident_id}",
    ),
}
REVIEWED_PROVIDERS = {
    "atlassian": "Atlassian",
    "google-cloud": "Google Cloud",
    "github": "GitHub",
    "cloudflare": "Cloudflare",
    "twilio": "Twilio",
    "supabase": "Supabase",
    "datadog": "Datadog",
    "vercel": "Vercel",
    "digitalocean": "DigitalOcean",
    "openai": "OpenAI",
    "aws": "Amazon Web Services",
}
REVIEWED_INTERFACE_DOCUMENTATION = {
    "atlassian": (
        "https://support.atlassian.com/statuspage/docs/"
        "what-are-the-different-apis-under-statuspage/"
    ),
    "google-cloud": (
        "https://docs.cloud.google.com/service-health/docs/service-health-fallback"
    ),
}
REVIEWED_ALLOWED_FIELDS = {
    "atlassian": (
        "incident ID",
        "synthetic non-narrative label",
        "normalized status",
        "start timestamp",
        "end timestamp",
        "collector timestamp",
        "exact official incident permalink",
        "independently calculated metrics",
    ),
    "google-cloud": (
        "incident ID",
        "provider incident number",
        "synthetic non-narrative label",
        "normalized status",
        "start timestamp",
        "end timestamp",
        "collector timestamp",
        "stable affected product IDs",
        "stable affected location IDs",
        "exact official incident permalink",
        "independently calculated metrics",
    ),
}
REVIEWED_DETAILS_FIELDS = {
    "atlassian": (),
    "google-cloud": (
        "affected_location_ids",
        "affected_product_ids",
        "provider_number",
    ),
}
REVIEWED_DISCLOSURES = {
    "analysis": (
        "StatusPulse calculations and normalized records are independently "
        "produced from the cited official sources."
    ),
    "source_links": ("Each record must retain an exact official incident permalink."),
    "non_affiliation": (
        "StatusPulse is independent and is not affiliated with, endorsed by, "
        "or sponsored by Atlassian or Google."
    ),
    "historical_not_realtime": (
        "The dataset is historical research material, not a real-time "
        "monitoring or outage-alerting service."
    ),
    "google_attribution": (
        "Source incident identifiers and factual service-health fields are "
        "attributed to Google Cloud through each official incident link."
    ),
}
ALLOWED_STATUSES = frozenset({"investigating", "identified", "monitoring", "resolved"})
INCIDENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,240}$")
DETAIL_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9._:/-]{1,240}$")


class SourcePolicyError(ValueError):
    """Raised when policy or source data does not meet the reviewed contract."""


@dataclass(frozen=True)
class SourceRule:
    """One validated source rule from the reviewed manifest."""

    source: str
    provider: str
    state: str
    endpoint: str | None
    permalink_template: str | None
    details_fields: frozenset[str]
    evidence_urls: tuple[str, ...]
    attribution: str | None

    def permalink(self, incident_id: str) -> str:
        """Return the exact official link for a validated provider incident ID."""
        if self.state != "enabled" or self.permalink_template is None:
            raise SourcePolicyError(f"source {self.source!r} has no approved permalink")
        return self.permalink_template.format(incident_id=quote(incident_id, safe=""))


@dataclass(frozen=True)
class SourcePolicy:
    """Validated, dated source-rights manifest."""

    path: Path
    reviewed_on: date
    next_review_on: date
    sources: Mapping[str, SourceRule]
    disclosures: Mapping[str, str]

    @property
    def enabled_sources(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                source
                for source, rule in self.sources.items()
                if rule.state == "enabled"
            )
        )

    def rule(self, source: str) -> SourceRule:
        """Return an enabled rule, failing closed for unknown or gated sources."""
        rule = self.sources.get(source)
        if rule is None:
            raise SourcePolicyError(f"source {source!r} is not in the policy")
        if rule.state != "enabled":
            raise SourcePolicyError(
                f"source {source!r} is gated with state {rule.state!r}"
            )
        return rule


@dataclass(frozen=True)
class SanitizeResult:
    """Counts from an in-place database policy sanitization."""

    before: int
    retained: int
    deleted_gated: int
    deleted_invalid: int
    rewritten: int


def _parse_policy_date(value: object, field: str) -> date:
    if not isinstance(value, str):
        raise SourcePolicyError(f"policy {field} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise SourcePolicyError(f"policy {field} is not a valid ISO date") from error


def _require_https_urls(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise SourcePolicyError(f"policy {field} must be a non-empty URL list")
    urls: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise SourcePolicyError(f"policy {field} contains a non-string URL")
        parsed = urlparse(item)
        if parsed.scheme != "https" or not parsed.hostname:
            raise SourcePolicyError(f"policy {field} contains a non-HTTPS URL")
        urls.append(item)
    return tuple(urls)


def _read_policy_document(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SourcePolicyError(f"cannot read source policy {path}: {error}") from error
    if not isinstance(raw, dict):
        raise SourcePolicyError("source policy must be a JSON object")
    if raw.get("schema_version") != 1 or raw.get("default_state") != "disabled":
        raise SourcePolicyError(
            "source policy must use schema version 1 and default_state=disabled"
        )
    return raw


def _validated_review_dates(
    raw: Mapping[str, Any], as_of: date | None
) -> tuple[date, date]:
    reviewed_on = _parse_policy_date(raw.get("reviewed_on"), "reviewed_on")
    next_review_on = _parse_policy_date(raw.get("next_review_on"), "next_review_on")
    today = as_of or datetime.now(timezone.utc).date()
    if reviewed_on > today:
        raise SourcePolicyError("source policy review date is in the future")
    if today >= next_review_on:
        raise SourcePolicyError(
            f"source policy review expired on {next_review_on.isoformat()}"
        )
    if next_review_on <= reviewed_on:
        raise SourcePolicyError("source policy next review must follow its review date")
    if next_review_on - reviewed_on > timedelta(days=93):
        raise SourcePolicyError("source policy review interval exceeds 93 days")
    return reviewed_on, next_review_on


def _validate_enabled_rule(
    source: str, value: Mapping[str, Any], evidence_urls: tuple[str, ...]
) -> None:
    expected_endpoint, expected_permalink = REVIEWED_INTERFACES[source]
    if (
        value.get("endpoint") != expected_endpoint
        or value.get("permalink_template") != expected_permalink
    ):
        raise SourcePolicyError(
            f"source {source!r} interface differs from the reviewed interface"
        )
    if tuple(value.get("allowed_fields", ())) != REVIEWED_ALLOWED_FIELDS[source]:
        raise SourcePolicyError(
            f"source {source!r} allowed fields differ from the reviewed set"
        )
    if tuple(value.get("details_fields", ())) != REVIEWED_DETAILS_FIELDS[source]:
        raise SourcePolicyError(
            f"source {source!r} detail fields differ from the reviewed set"
        )
    attribution = value.get("attribution")
    if not isinstance(attribution, str) or not attribution.strip():
        raise SourcePolicyError(f"source {source!r} has no attribution rule")
    documentation = value.get("interface_documentation")
    if documentation != REVIEWED_INTERFACE_DOCUMENTATION[source]:
        raise SourcePolicyError(
            f"source {source!r} documentation differs from the reviewed URL"
        )
    if documentation not in evidence_urls:
        raise SourcePolicyError(
            f"source {source!r} interface documentation lacks evidence"
        )


def _parse_source_rule(source: str, reviewed_state: str, value: object) -> SourceRule:
    if not isinstance(value, dict):
        raise SourcePolicyError(f"source rule {source!r} must be an object")
    state = value.get("state")
    if state != reviewed_state:
        raise SourcePolicyError(
            f"source {source!r} state {state!r} differs from reviewed "
            f"state {reviewed_state!r}"
        )
    provider = value.get("provider")
    if provider != REVIEWED_PROVIDERS[source]:
        raise SourcePolicyError(
            f"source {source!r} provider differs from the reviewed provider"
        )
    decision = value.get("decision")
    if not isinstance(decision, str) or not decision.strip():
        raise SourcePolicyError(f"source {source!r} has no decision rationale")
    evidence_urls = _require_https_urls(
        value.get("evidence_urls"), f"sources.{source}.evidence_urls"
    )
    details_fields = value.get("details_fields", [])
    if not isinstance(details_fields, list) or any(
        not isinstance(item, str) for item in details_fields
    ):
        raise SourcePolicyError(
            f"source {source!r} details_fields must be a string list"
        )
    if state == "enabled":
        _validate_enabled_rule(source, value, evidence_urls)
    elif (
        value.get("endpoint") is not None or value.get("permalink_template") is not None
    ):
        raise SourcePolicyError(
            f"gated source {source!r} must not define a callable interface"
        )
    attribution = value.get("attribution")
    return SourceRule(
        source=source,
        provider=provider,
        state=state,
        endpoint=value.get("endpoint"),
        permalink_template=value.get("permalink_template"),
        details_fields=frozenset(details_fields),
        evidence_urls=evidence_urls,
        attribution=attribution if isinstance(attribution, str) else None,
    )


def _parse_source_rules(raw_sources: object) -> dict[str, SourceRule]:
    if not isinstance(raw_sources, dict) or set(raw_sources) != set(REVIEWED_STATES):
        raise SourcePolicyError(
            "source policy source set differs from the reviewed set"
        )
    sources = {
        source: _parse_source_rule(source, state, raw_sources.get(source))
        for source, state in REVIEWED_STATES.items()
    }
    enabled = frozenset(
        source for source, rule in sources.items() if rule.state == "enabled"
    )
    if enabled != REVIEWED_ENABLED_SOURCES:
        raise SourcePolicyError("enabled source set differs from the reviewed set")
    return sources


def load_policy(
    path: Path = DEFAULT_POLICY_PATH,
    *,
    as_of: date | None = None,
) -> SourcePolicy:
    """Load and validate the reviewed manifest, including its review deadline."""
    raw = _read_policy_document(path)
    reviewed_on, next_review_on = _validated_review_dates(raw, as_of)
    raw_disclosures = raw.get("required_public_disclosures")
    if raw_disclosures != REVIEWED_DISCLOSURES:
        raise SourcePolicyError(
            "source policy disclosures differ from the reviewed text"
        )
    sources = _parse_source_rules(raw.get("sources"))
    return SourcePolicy(
        path=path,
        reviewed_on=reviewed_on,
        next_review_on=next_review_on,
        sources=sources,
        disclosures=dict(raw_disclosures),
    )


def _parse_iso_timestamp(value: object, *, nullable: bool) -> datetime | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value:
        raise SourcePolicyError("incident timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise SourcePolicyError("incident timestamp is not ISO 8601") from error
    if parsed.tzinfo is None:
        raise SourcePolicyError("incident timestamp must include a timezone")
    return parsed


def _validate_timestamps(record: Mapping[str, Any]) -> None:
    """Validate incident timestamps and reject impossible negative durations."""
    started_at = _parse_iso_timestamp(record.get("started_at"), nullable=False)
    resolved_at = _parse_iso_timestamp(record.get("resolved_at"), nullable=True)
    _parse_iso_timestamp(record.get("fetched_at"), nullable=False)
    if started_at is not None and resolved_at is not None and resolved_at < started_at:
        raise SourcePolicyError("incident resolution precedes its start time")


def _provider_incident_id(record_id: object, source: str) -> str:
    prefix = f"{source}:"
    if not isinstance(record_id, str) or not record_id.startswith(prefix):
        raise SourcePolicyError(f"incident ID is not namespaced for {source!r}")
    provider_id = record_id[len(prefix) :]
    if not INCIDENT_ID_PATTERN.fullmatch(provider_id):
        raise SourcePolicyError("incident provider ID has an unsafe format")
    return provider_id


def _identifier_list(value: object, field: str) -> list[str]:
    if not isinstance(value, list):
        raise SourcePolicyError(f"incident details field {field!r} must be a list")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not DETAIL_IDENTIFIER_PATTERN.fullmatch(item):
            raise SourcePolicyError(
                f"incident details field {field!r} contains an invalid identifier"
            )
        result.append(item)
    if result != sorted(set(result)):
        raise SourcePolicyError(
            f"incident details field {field!r} must be sorted and unique"
        )
    return result


def _validate_details(source: str, value: object, rule: SourceRule) -> None:
    if source == "atlassian":
        if value not in (None, ""):
            raise SourcePolicyError("Atlassian records must not retain details")
        return
    if not isinstance(value, str) or not value:
        raise SourcePolicyError("Google Cloud records require structured identifiers")
    try:
        details = json.loads(value)
    except ValueError as error:
        raise SourcePolicyError("incident details is not valid JSON") from error
    if not isinstance(details, dict) or set(details) - rule.details_fields:
        raise SourcePolicyError("incident details contains unapproved fields")
    if not details:
        raise SourcePolicyError("incident details must not be empty")
    for field in ("affected_location_ids", "affected_product_ids"):
        if field in details:
            _identifier_list(details[field], field)
    if "provider_number" in details:
        number = details["provider_number"]
        if not isinstance(number, str) or not number.isdigit() or len(number) > 40:
            raise SourcePolicyError("Google Cloud provider number is invalid")


def validate_incident_record(
    record: Mapping[str, Any],
    *,
    policy: SourcePolicy | None = None,
) -> None:
    """Reject gated sources and any retained expressive or raw provider data."""
    active_policy = policy or load_policy()
    source = record.get("source")
    if not isinstance(source, str):
        raise SourcePolicyError("incident source is missing")
    rule = active_policy.rule(source)
    provider_id = _provider_incident_id(record.get("id"), source)
    expected_label = f"{rule.provider} incident {provider_id}"
    if record.get("name") != expected_label:
        raise SourcePolicyError("incident name is not the approved synthetic label")
    status = record.get("status")
    if status not in ALLOWED_STATUSES:
        raise SourcePolicyError("incident status is not an approved normalization")
    if record.get("impact") is not None:
        raise SourcePolicyError("provider impact labels are not approved for retention")
    _validate_timestamps(record)
    expected_url = rule.permalink(provider_id)
    if record.get("source_url") != expected_url:
        raise SourcePolicyError("incident citation is not the exact official permalink")
    _validate_details(source, record.get("details"), rule)


def validate_public_incident_record(
    record: Mapping[str, Any],
    *,
    policy: SourcePolicy | None = None,
) -> None:
    """Validate the non-sensitive fields allowed in a public sample record."""
    active_policy = policy or load_policy()
    source = record.get("source")
    if not isinstance(source, str):
        raise SourcePolicyError("incident source is missing")
    rule = active_policy.rule(source)
    provider_id = _provider_incident_id(record.get("id"), source)
    if record.get("name") != f"{rule.provider} incident {provider_id}":
        raise SourcePolicyError("incident name is not the approved synthetic label")
    if record.get("status") not in ALLOWED_STATUSES:
        raise SourcePolicyError("incident status is not an approved normalization")
    if record.get("impact") not in (None, ""):
        raise SourcePolicyError(
            "provider impact labels are not approved for publication"
        )
    normalized_record = dict(record)
    normalized_record["resolved_at"] = record.get("resolved_at") or None
    _validate_timestamps(normalized_record)
    if record.get("source_url") != rule.permalink(provider_id):
        raise SourcePolicyError("incident citation is not the exact official permalink")


def _stable_ids(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    values: set[str] = set()
    for item in value:
        identifier = item.get("id") if isinstance(item, dict) else None
        if isinstance(identifier, str) and DETAIL_IDENTIFIER_PATTERN.fullmatch(
            identifier
        ):
            values.add(identifier)
    return sorted(values)


def _sanitize_google_details(value: object) -> str:
    raw: dict[str, Any] = {}
    if isinstance(value, str) and value:
        try:
            decoded = json.loads(value)
        except ValueError:
            decoded = {}
        if isinstance(decoded, dict):
            raw = decoded

    details: dict[str, Any] = {}
    product_ids = raw.get("affected_product_ids")
    if isinstance(product_ids, list) and all(
        isinstance(item, str) for item in product_ids
    ):
        approved_products = sorted(
            {item for item in product_ids if DETAIL_IDENTIFIER_PATTERN.fullmatch(item)}
        )
    else:
        approved_products = _stable_ids(raw.get("products"))
    location_ids = raw.get("affected_location_ids")
    if isinstance(location_ids, list) and all(
        isinstance(item, str) for item in location_ids
    ):
        approved_locations = sorted(
            {item for item in location_ids if DETAIL_IDENTIFIER_PATTERN.fullmatch(item)}
        )
    else:
        approved_locations = _stable_ids(raw.get("locations"))
    if approved_products:
        details["affected_product_ids"] = approved_products
    if approved_locations:
        details["affected_location_ids"] = approved_locations
    number = raw.get("provider_number", raw.get("number"))
    if (
        isinstance(number, (str, int))
        and str(number).isdigit()
        and len(str(number)) <= 40
    ):
        details["provider_number"] = str(number)
    if not details:
        # Empty lists are explicit, factual, and keep the structured field valid.
        details = {"affected_location_ids": [], "affected_product_ids": []}
    return json.dumps(details, separators=(",", ":"), sort_keys=True)


def _sanitize_enabled_row(row: sqlite3.Row, policy: SourcePolicy) -> tuple[object, ...]:
    source = str(row["source"])
    rule = policy.rule(source)
    stored_id = row["id"]
    try:
        provider_id = _provider_incident_id(stored_id, source)
    except SourcePolicyError:
        # Migrate the collector's former GCP namespace to the reviewed source
        # name. This is the only accepted legacy identifier form.
        if (
            source != "google-cloud"
            or not isinstance(stored_id, str)
            or not stored_id.startswith("gcp:")
        ):
            raise
        provider_id = stored_id.removeprefix("gcp:")
        if not INCIDENT_ID_PATTERN.fullmatch(provider_id):
            raise SourcePolicyError("legacy Google Cloud incident ID is unsafe")
    normalized_id = f"{source}:{provider_id}"
    status = str(row["status"] or "").lower()
    if status == "postmortem":
        status = "resolved"
    if status not in ALLOWED_STATUSES:
        raise SourcePolicyError("stored incident has an unknown status")
    _parse_iso_timestamp(row["started_at"], nullable=False)
    _parse_iso_timestamp(row["resolved_at"], nullable=True)
    _parse_iso_timestamp(row["fetched_at"], nullable=False)
    details = (
        None if source == "atlassian" else _sanitize_google_details(row["details"])
    )
    return (
        normalized_id,
        f"{rule.provider} incident {provider_id}",
        status,
        None,
        rule.permalink(provider_id),
        details,
        stored_id,
    )


def _require_incident_schema(database: sqlite3.Connection) -> None:
    columns = {row[1] for row in database.execute("PRAGMA table_info(incidents)")}
    required = {
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
    }
    if not required <= columns:
        raise SourcePolicyError("database incidents schema is incomplete")


def _delete_gated_rows(database: sqlite3.Connection, policy: SourcePolicy) -> int:
    placeholders = ",".join("?" for _ in policy.enabled_sources)
    cursor = database.execute(
        f"DELETE FROM incidents WHERE source NOT IN ({placeholders})",
        policy.enabled_sources,
    )
    return max(cursor.rowcount, 0)


def _rewrite_enabled_rows(
    database: sqlite3.Connection, policy: SourcePolicy
) -> tuple[int, int]:
    deleted_invalid = rewritten = 0
    rows = database.execute("SELECT * FROM incidents ORDER BY id").fetchall()
    for row in rows:
        try:
            values = _sanitize_enabled_row(row, policy)
        except SourcePolicyError:
            database.execute("DELETE FROM incidents WHERE id=?", (row["id"],))
            deleted_invalid += 1
            continue
        database.execute(
            """
            UPDATE incidents
            SET id=?, name=?, status=?, impact=?, source_url=?, details=?
            WHERE id=?
            """,
            values,
        )
        rewritten += 1
    return deleted_invalid, rewritten


def _validate_retained_rows(database: sqlite3.Connection, policy: SourcePolicy) -> int:
    rows = database.execute("SELECT * FROM incidents ORDER BY id").fetchall()
    for row in rows:
        validate_incident_record(dict(row), policy=policy)
    return len(rows)


def _sanitize_connection(
    database: sqlite3.Connection, policy: SourcePolicy
) -> SanitizeResult:
    database.row_factory = sqlite3.Row
    _require_incident_schema(database)
    database.execute("BEGIN IMMEDIATE")
    before = database.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
    deleted_gated = _delete_gated_rows(database, policy)
    deleted_invalid, rewritten = _rewrite_enabled_rows(database, policy)
    retained = _validate_retained_rows(database, policy)
    if before != retained + deleted_gated + deleted_invalid:
        raise SourcePolicyError("database sanitization counts are inconsistent")
    database.commit()
    return SanitizeResult(
        before=before,
        retained=retained,
        deleted_gated=deleted_gated,
        deleted_invalid=deleted_invalid,
        rewritten=rewritten,
    )


def sanitize_database(
    path: Path,
    *,
    policy: SourcePolicy | None = None,
) -> SanitizeResult:
    """Delete gated/invalid rows and rewrite enabled rows to minimal fields."""
    active_policy = policy or load_policy()
    if not path.is_file():
        raise SourcePolicyError(f"database does not exist: {path}")
    try:
        with closing(sqlite3.connect(path)) as database:
            return _sanitize_connection(database, active_policy)
    except sqlite3.Error as error:
        raise SourcePolicyError(f"cannot sanitize database: {error}") from error


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--policy", type=Path, default=DEFAULT_POLICY_PATH, help="policy manifest"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate", help="validate the dated policy manifest")
    sanitize = commands.add_parser(
        "sanitize", help="sanitize a working SQLite database in place"
    )
    sanitize.add_argument("database", type=Path)
    arguments = parser.parse_args(argv)
    try:
        policy = load_policy(arguments.policy)
        if arguments.command == "validate":
            print(
                f"enabled={','.join(policy.enabled_sources)} "
                f"next_review={policy.next_review_on.isoformat()}"
            )
        else:
            result = sanitize_database(arguments.database, policy=policy)
            print(
                f"sanitized before={result.before} retained={result.retained} "
                f"deleted_gated={result.deleted_gated} "
                f"deleted_invalid={result.deleted_invalid} "
                f"rewritten={result.rewritten}"
            )
    except SourcePolicyError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

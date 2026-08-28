#!/usr/bin/env python3
"""Generate the public StatusPulse pilot page and privacy-safe sample."""

import csv
import html
import json
import os
import re
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from release_contract import ACTIVE_STATUSES, SAMPLE_COLUMNS, safe_csv_cell
from source_policy import load_policy

ROOT = Path(__file__).resolve().parent
DB = ROOT / "statusfeed.db"
OUT = ROOT / "index.html"
SAMPLE = ROOT / "statuspulse-sample.csv"
STATS = ROOT / "stats.json"
MTTR_STATS = ROOT / "mttr_stats.json"

PILOT_LINK_FALLBACK = "https://statusfeed-dev.github.io/statuspulse/#pilot"


def esc(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def validated_pilot_link(candidate: str) -> str:
    """Return a tightly scoped Stripe URL, or the safe local pilot anchor."""
    candidate = candidate.strip()
    parsed = urlparse(candidate)
    if (
        parsed.scheme == "https"
        and parsed.netloc == "buy.stripe.com"
        and re.fullmatch(r"/[A-Za-z0-9_-]+", parsed.path)
        and not parsed.params
        and not parsed.query
        and not parsed.fragment
    ):
        return candidate
    return PILOT_LINK_FALLBACK


def configured_pilot_link(environment: Mapping[str, str] = os.environ) -> str:
    """Return only an expected Stripe-hosted pilot URL, or a safe local anchor."""
    return validated_pilot_link(environment.get("STATUSPULSE_PILOT_URL", ""))


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def duration_hours(start: str | None, end: str | None) -> float | None:
    started, ended = parse_dt(start), parse_dt(end)
    if not started or not ended:
        return None
    return max(0, (ended - started).total_seconds() / 3600)


def median(values: list[float]) -> float:
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / 2


def load_rows(database: Path = DB) -> list[sqlite3.Row]:
    with closing(sqlite3.connect(database)) as db:
        db.row_factory = sqlite3.Row
        return db.execute(
            "SELECT * FROM incidents ORDER BY started_at DESC, id ASC"
        ).fetchall()


def compute_metrics(rows: list[sqlite3.Row], now: datetime) -> dict[str, Any]:
    cutoff = now - timedelta(days=30)
    recent = [
        row
        for row in rows
        if (parse_dt(row["started_at"]) or datetime.min.replace(tzinfo=timezone.utc))
        >= cutoff
    ]
    active = [row for row in recent if (row["status"] or "").lower() in ACTIVE_STATUSES]
    providers = sorted({row["source"] for row in rows})
    provider_counts: dict[str, int] = {}
    provider_recent: dict[str, int] = {}
    provider_major: dict[str, int] = {}
    durations: dict[str, list[float]] = {}
    impacts: dict[str, int] = {}

    for row in rows:
        source = row["source"]
        provider_counts[source] = provider_counts.get(source, 0) + 1
        duration = duration_hours(row["started_at"], row["resolved_at"])
        if duration is not None:
            durations.setdefault(source, []).append(duration)
    for row in recent:
        source = row["source"]
        provider_recent[source] = provider_recent.get(source, 0) + 1
        impact = row["impact"] or "unknown"
        impacts[impact] = impacts.get(impact, 0) + 1
        if impact in {"critical", "major"}:
            provider_major[source] = provider_major.get(source, 0) + 1

    provider_mttr = {
        source: median(values) for source, values in durations.items() if values
    }
    return {
        "recent": recent,
        "active": active,
        "providers": providers,
        "provider_counts": provider_counts,
        "provider_recent": provider_recent,
        "provider_major": provider_major,
        "provider_mttr": provider_mttr,
        "provider_duration_counts": {
            source: len(values) for source, values in durations.items()
        },
        "impacts": impacts,
    }


def write_sample(rows: list[sqlite3.Row], destination: Path = SAMPLE) -> None:
    with destination.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(
            output,
            fieldnames=SAMPLE_COLUMNS,
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(
            {field: safe_csv_cell(row[field]) for field in SAMPLE_COLUMNS}
            for row in rows[:100]
        )


PAGE_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="favicon.svg" type="image/svg+xml">
<meta name="description" content="Fixed-scope vendor dependency evidence briefs using cited primary sources and minimal normalized incident facts.">
<link rel="canonical" href="https://statusfeed-dev.github.io/statuspulse/">
<meta property="og:title" content="StatusPulse — vendor dependency evidence brief">
<meta property="og:description" content="A decision-ready evidence map for up to 20 SaaS and cloud dependencies.">
<meta property="og:url" content="https://statusfeed-dev.github.io/statuspulse/">
<meta property="og:type" content="website">
<script type="application/ld+json">{structured_offer}</script>
<title>StatusPulse — vendor dependency evidence brief</title>
<style>
body{{font-family:system-ui,-apple-system,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;color:#17202a;line-height:1.55}}
h1 span{{color:#087f5b}}h2{{margin-top:2rem}}a{{color:#0866c6}}a:focus,.button:focus{{outline:3px solid #74c0fc;outline-offset:2px}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:.8rem}}
.card{{border:1px solid #d9e2e8;border-radius:10px;padding:1rem;background:#fbfdfd}}.big{{font-size:1.7rem;font-weight:700}}
table{{border-collapse:collapse;width:100%;font-size:.9rem}}td,th{{padding:.65rem .6rem;border-bottom:1px solid #e5e7eb;text-align:left;vertical-align:top}}th{{background:#f8fafc}}
.status{{border-radius:999px;padding:.15rem .5rem;background:#eef2f4;font-size:.8rem}}.resolved{{color:#087f5b;background:#d3f9d8}}.investigating,.identified,.monitoring{{color:#9a3412;background:#ffedd5}}
.cta{{background:#e6fcf5;border:1px solid #96f2d7;border-radius:10px;padding:1.25rem;margin-top:2rem}}.button{{display:inline-block;background:#087f5b;color:#fff;text-decoration:none;font-weight:700;border-radius:7px;padding:.7rem 1rem}}small{{color:#59636e}}.fine-print{{font-size:.88rem;color:#59636e}}
@media(max-width:640px){{body{{margin:1rem auto}}table{{display:block;overflow-x:auto;white-space:nowrap}}}}
</style></head><body>
<h1>Status<span>Pulse</span></h1>
<p><strong>A decision-ready evidence brief for your vendor stack.</strong> Give us up to 20 SaaS or cloud dependencies. We map official status, service-health, support, and policy sources; identify evidence gaps; and include calculated incident metrics only for sources enabled by our dated, fail-closed source policy.</p>
<div class="grid"><div class="card"><div class="big">{row_count:,}</div>Policy-gated normalized incidents</div><div class="card"><div class="big">{recent_count:,}</div>Started in the last 30 days</div><div class="card"><div class="big">{active_count:,}</div>Active vendor reports in 30 days</div><div class="card"><div class="big">{provider_count}</div>Automated sources currently enabled</div></div>
<p><small>Dataset generated {generated}. Enabled sources: {source_list}. Recent impact mix: {impact_items}. Sources not enabled by the source policy are link-mapped but are not bulk-collected.</small></p>
<div class="cta" id="pilot"><h2>$79 fixed-scope pilot</h2>
<p><strong>One payment. No subscription.</strong> The first three pilot customers receive:</p>
<ul><li>An official evidence map for up to 20 named dependencies, including status, support, service-health, and policy links where available.</li><li>A branded, print-ready report plus a supporting CSV of source-policy-gated factual records and direct source links.</li><li>Coverage gaps, renewal and due-diligence questions, and incident metrics for enabled documented-integration sources.</li><li>Delivery to the checkout email within two business days and one factual-correction pass.</li></ul>
{checkout_action}
<p class="fine-print">Initial pilot sales are limited to US customers. Submit only non-confidential vendor names. If source-policy-approved material cannot support a useful brief, we will offer a replacement scope or a full refund before work starts. This is historical vendor-reported evidence, not real-time status, synthetic monitoring, an SLA audit, legal advice, or an uptime guarantee.</p></div>
<h2>Preview the evidence</h2><p><a href="statuspulse-sample.csv" download>Download the current minimal-fact sample with source URLs</a>. It contains normalized factual fields and excludes provider narratives and raw feed objects.</p>
<h2>Recent vendor-reported incidents</h2><table><tr><th>Provider</th><th>Incident</th><th>Status</th><th>Impact</th><th>Started</th></tr>{incident_rows}</table>
<h2>Enabled-source snapshot</h2><p>Only sources enabled by the dated source policy are automated. Counts reflect each enabled provider's available history window. Duration is the median among records with both start and resolution timestamps.</p><table><tr><th>Provider</th><th>Incidents (30d)</th><th>Critical/major (30d)</th><th>Incidents captured</th><th>Median resolved duration</th></tr>{score_rows}</table>
<p><small>Methodology: <a href="vendor-incident-history.html">how vendor incident history is collected and interpreted</a>.</small></p>
<h2>Source rights and attribution</h2><ul>{disclosure_items}</ul>
<p><small><a href="terms.html">Terms</a> · <a href="privacy.html">Privacy</a> · <a href="refunds.html">Refund policy</a> · <a href="support.html">Support</a></small></p>
</body></html>"""


def _render_incident_rows(recent: list[sqlite3.Row]) -> str:
    """Render the bounded recent-incident table body."""
    return "\n".join(
        "<tr>"
        f"<td>{esc(row['source'])}</td>"
        f"<td>{esc(row['name'])}</td>"
        f'<td><span class="status {esc(row["status"])}">'
        f"{esc(row['status'])}</span></td>"
        f"<td>{esc(row['impact'])}</td>"
        f"<td>{esc((row['started_at'] or '')[:19].replace('T', ' '))}</td>"
        "</tr>"
        for row in recent[:25]
    )


def _render_score_rows(metrics: dict[str, Any]) -> str:
    """Render the enabled-provider score table body."""
    provider_counts = metrics["provider_counts"]
    provider_recent = metrics["provider_recent"]
    provider_major = metrics["provider_major"]
    provider_mttr = metrics["provider_mttr"]
    sources = sorted(
        provider_counts,
        key=lambda item: (-provider_recent.get(item, 0), item),
    )
    return "\n".join(
        "<tr>"
        f"<td>{esc(source)}</td>"
        f"<td>{provider_recent.get(source, 0)}</td>"
        f"<td>{provider_major.get(source, 0)}</td>"
        f"<td>{provider_counts[source]}</td>"
        f"<td>{f'{provider_mttr[source]:.2f} h' if source in provider_mttr else 'n/a'}</td>"
        "</tr>"
        for source in sources
    )


def _render_disclosures(disclosures: Mapping[str, str]) -> str:
    """Render policy-defined public disclosures in their fixed order."""
    keys = (
        "analysis",
        "source_links",
        "non_affiliation",
        "historical_not_realtime",
        "google_attribution",
    )
    return "".join(f"<li>{esc(disclosures[key])}</li>" for key in keys)


def _structured_service(pilot_link: str) -> str:
    """Render script-safe service metadata, with an offer only when buyable."""
    service: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "Service",
        "name": "Vendor Dependency Evidence Brief — Pilot",
        "description": (
            "One fixed-scope evidence brief for up to 20 named SaaS or cloud "
            "dependencies, using source links, original analysis, and only "
            "source-policy-gated automated incident data."
        ),
        "provider": {"@type": "Organization", "name": "StatusPulse"},
    }
    if pilot_link != PILOT_LINK_FALLBACK:
        service["offers"] = {
            "@type": "Offer",
            "price": "79.00",
            "priceCurrency": "USD",
            "url": pilot_link,
            "availability": "https://schema.org/LimitedAvailability",
        }
    return (
        json.dumps(service, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def _checkout_action(pilot_link: str) -> str:
    """Render a purchase action only for a validated Stripe checkout URL."""
    if pilot_link == PILOT_LINK_FALLBACK:
        return (
            '<p class="fine-print"><strong>Checkout temporarily unavailable.'
            "</strong> Please check back soon.</p>"
        )
    return (
        f'<p><a class="button" href="{esc(pilot_link)}">Order the pilot — $79</a></p>'
    )


def render_page(
    rows: list[sqlite3.Row],
    metrics: dict[str, Any],
    now: datetime,
    pilot_link: str,
    disclosures: Mapping[str, str] | None = None,
) -> str:
    """Render the public landing page from one validated metrics snapshot."""
    pilot_link = validated_pilot_link(pilot_link)
    providers = metrics["providers"]
    impact_items = " · ".join(
        f"{esc(key)}: {value}" for key, value in sorted(metrics["impacts"].items())
    )
    policy_disclosures = disclosures or load_policy().disclosures
    return PAGE_TEMPLATE.format(
        structured_offer=_structured_service(pilot_link),
        row_count=len(rows),
        recent_count=len(metrics["recent"]),
        active_count=len(metrics["active"]),
        provider_count=len(providers),
        generated=now.strftime("%Y-%m-%d %H:%M UTC"),
        source_list=esc(", ".join(providers)),
        impact_items=impact_items or "none",
        checkout_action=_checkout_action(pilot_link),
        incident_rows=_render_incident_rows(metrics["recent"]),
        score_rows=_render_score_rows(metrics),
        disclosure_items=_render_disclosures(policy_disclosures),
    )


def write_metadata(
    rows: list[sqlite3.Row], metrics: dict[str, Any], now: datetime
) -> None:
    stats = {
        "total": len(rows),
        "last30d": len(metrics["recent"]),
        "coverage_start": min(
            (row["started_at"] for row in rows if row["started_at"]),
            default=None,
        ),
        "coverage_end": max(
            (row["started_at"] for row in rows if row["started_at"]),
            default=None,
        ),
        "computed_at": now.isoformat(),
        "by_source": metrics["provider_counts"],
        "active_reports_30d": len(metrics["active"]),
        "unresolved": len(metrics["active"]),
    }
    STATS.write_text(json.dumps(stats, indent=2) + "\n", encoding="utf-8")
    mttr = {
        source: {
            "median_mttr_hrs": round(value, 2),
            "n": metrics["provider_duration_counts"][source],
        }
        for source, value in sorted(metrics["provider_mttr"].items())
    }
    MTTR_STATS.write_text(json.dumps(mttr, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    rows = load_rows()
    if not rows:
        raise RuntimeError("statusfeed.db contains no incidents")
    now = datetime.now(timezone.utc)
    metrics = compute_metrics(rows, now)
    write_sample(rows)
    OUT.write_text(
        render_page(
            rows,
            metrics,
            now,
            configured_pilot_link(),
            disclosures=load_policy().disclosures,
        ),
        encoding="utf-8",
    )
    write_metadata(rows, metrics, now)
    print(
        f"generated {OUT.name}: {len(rows)} total, "
        f"{len(metrics['recent'])} recent, "
        f"{len(metrics['active'])} active vendor reports, "
        f"{len(metrics['providers'])} sources"
    )


if __name__ == "__main__":
    main()

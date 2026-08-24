#!/usr/bin/env python3
"""Generate the public StatusPulse page and sample from the local collector DB."""
import csv
import html
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB = ROOT / "statusfeed.db"
OUT = ROOT / "index.html"
SAMPLE = ROOT / "statuspulse-sample.csv"
LINK_STANDARD = "https://buy.stripe.com/9B6eVe0wp7eGaVc1bT8og00"
LINK_FOUNDING = "https://buy.stripe.com/bJe5kE4MF2Yq2oG2fX8og01"
LINK_PRO = "https://buy.stripe.com/6oUdRacf756ybZgf2J8og02"
LINK_ANNUAL = "https://buy.stripe.com/bJe8wQ92VbuW4wO4o58og03"


def esc(value):
    return html.escape(str(value or ""), quote=True)


def parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def duration_hours(start, end):
    a, b = parse_dt(start), parse_dt(end)
    if not a or not b:
        return None
    return max(0, (b - a).total_seconds() / 3600)


def main():
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    rows = db.execute("SELECT * FROM incidents ORDER BY started_at DESC").fetchall()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=30)
    recent = [r for r in rows if (parse_dt(r["started_at"]) or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff]
    unresolved = [r for r in rows if r["status"] != "resolved"]
    providers = sorted({r["source"] for r in rows})
    impacts = {}
    for r in recent:
        impacts[r["impact"] or "unknown"] = impacts.get(r["impact"] or "unknown", 0) + 1
    provider_counts = {}
    provider_recent = {}
    provider_major = {}
    provider_mttr = {}
    for r in rows:
        source = r["source"]
        provider_counts[source] = provider_counts.get(source, 0) + 1
        started = parse_dt(r["started_at"]) or datetime.min.replace(tzinfo=timezone.utc)
        if started >= cutoff:
            provider_recent[source] = provider_recent.get(source, 0) + 1
            if r["impact"] in ("critical", "major"):
                provider_major[source] = provider_major.get(source, 0) + 1
        h = duration_hours(r["started_at"], r["resolved_at"])
        if h is not None:
            provider_mttr.setdefault(source, []).append(h)
    for source in provider_mttr:
        vals = sorted(provider_mttr[source])
        n = len(vals)
        provider_mttr[source] = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2

    sample_fields = ["id", "source", "name", "status", "impact", "started_at", "resolved_at", "fetched_at"]
    with SAMPLE.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=sample_fields)
        writer.writeheader()
        writer.writerows({field: r[field] for field in sample_fields} for r in rows[:100])

    incident_rows = "\n".join(
        f'<tr><td>{esc(r["source"])}</td><td>{esc(r["name"])}</td><td><span class="status {esc(r["status"])}">{esc(r["status"])}</span></td><td>{esc(r["impact"])}</td><td>{esc((r["started_at"] or "")[:19].replace("T", " "))}</td></tr>'
        for r in recent[:25]
    )
    score_rows = "\n".join(
        f'<tr><td>{esc(source)}</td><td>{provider_recent.get(source, 0)}</td><td>{provider_major.get(source, 0)}</td><td>{provider_counts[source]}</td><td>{("%.2f h" % provider_mttr[source]) if source in provider_mttr else "n/a"}</td></tr>'
        for source in sorted(provider_counts, key=lambda s: (-provider_recent.get(s, 0), s))
    )
    impact_items = " · ".join(f"{esc(k)}: {v}" for k, v in sorted(impacts.items()))
    generated = now.strftime("%Y-%m-%d %H:%M UTC")
    source_list = ", ".join(providers)
    page = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="favicon.svg" type="image/svg+xml">
<meta name="description" content="Continuously refreshed, queryable incident history from official cloud-provider status pages.">
<link rel="canonical" href="https://statusfeed-dev.github.io/statuspulse/">
<meta property="og:title" content="StatusPulse — incident history and vendor reliability data">
<meta property="og:description" content="Citation-backed incident timelines from official provider status feeds, available as CSV and SQLite.">
<meta property="og:url" content="https://statusfeed-dev.github.io/statuspulse/">
<meta property="og:type" content="website">
<script type="application/ld+json">{{"@context":"https://schema.org","@type":"Dataset","name":"StatusPulse incident history","description":"Citation-backed incident timelines from official cloud and SaaS status feeds.","url":"https://statusfeed-dev.github.io/statuspulse/","license":"https://github.com/statusfeed-dev/statuspulse/blob/main/stripe-listing.md","isAccessibleForFree":true,"keywords":"incident history, vendor reliability, SRE data, postmortem evidence"}}</script>
<title>StatusPulse — incident history and vendor reliability data</title>
<style>
body{{font-family:system-ui,-apple-system,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;color:#17202a;line-height:1.5}}
h1 span{{color:#087f5b}} h2{{margin-top:2rem}} a{{color:#0866c6}} a:focus,button:focus{{outline:3px solid #74c0fc;outline-offset:2px}} .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:.8rem}}
.card{{border:1px solid #d9e2e8;border-radius:10px;padding:1rem;background:#fbfdfd}} .big{{font-size:1.7rem;font-weight:700}}
table{{border-collapse:collapse;width:100%;font-size:.9rem}}td,th{{padding:.65rem .6rem;border-bottom:1px solid #e5e7eb;text-align:left;vertical-align:top}}th{{background:#f8fafc}}
.status{{border-radius:999px;padding:.15rem .5rem;background:#eef2f4;font-size:.8rem}}.resolved{{color:#087f5b;background:#d3f9d8}}.investigating,.identified{{color:#9a3412;background:#ffedd5}}
.cta{{background:#e6fcf5;border:1px solid #96f2d7;border-radius:10px;padding:1rem}}.plans{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:.75rem;margin-top:1rem}}.plan{{background:#fff;border:1px solid #b2f2bb;border-radius:8px;padding:.8rem}}.plan a{{display:inline-block;font-weight:700;margin-top:.4rem}}small{{color:#59636e}}
@media(max-width:640px){{body{{margin:1rem auto}}table{{display:block;overflow-x:auto;white-space:nowrap}}}}
</style></head><body>
<h1>Status<span>Pulse</span></h1>
<p><strong>Evidence for vendor reliability reviews.</strong> StatusPulse preserves official incident timelines across {len(providers)} infrastructure providers in queryable CSV/SQLite form. It is a historical dataset, not synthetic monitoring or an uptime guarantee.</p>
<div class="grid"><div class="card"><div class="big">{len(rows):,}</div>Total incidents</div><div class="card"><div class="big">{len(recent):,}</div>Started in 30 days</div><div class="card"><div class="big">{len(unresolved):,}</div>Currently unresolved</div><div class="card"><div class="big">{len(providers)}</div>Official sources</div></div>
<p><small>Last generated {generated}. Sources: {esc(source_list)}. Recent impact mix: {impact_items or "none"}.</small></p>
<h2>Recent incident timeline</h2><p>Use this view to investigate recurring vendor failures, compare incident impact, and retain evidence beyond a provider's dashboard history.</p>
<table><tr><th>Provider</th><th>Incident</th><th>Status</th><th>Impact</th><th>Started</th></tr>{incident_rows}</table>
<h2>Provider reliability scorecard</h2><p>Incidents captured from each provider's official status feed, ranked by last-30-day volume. Severity counts include critical and major impacts only. Durations are medians of resolved incidents; coverage windows differ by source.</p><table><tr><th>Provider</th><th>Incidents (30d)</th><th>Critical/major (30d)</th><th>Incidents (all time)</th><th>Median resolved duration</th></tr>{score_rows}</table>
<div class="cta"><h2>Get the data</h2><p><a href="statuspulse-sample.csv" download>Download the current 100-row CSV sample</a> · <a href="https://github.com/statusfeed-dev/statuspulse-export">Use the free CLI exporter</a></p><p>The paid release includes the current full CSV and SQLite dataset, refreshed from official provider incident feeds. Subscribers get authenticated access immediately after checkout.</p><div class="plans"><div class="plan"><strong>Founding</strong><br>$3/month<br><a href="{LINK_FOUNDING}">Choose Founding</a></div><div class="plan"><strong>Standard</strong><br>$5/month<br><a href="{LINK_STANDARD}">Choose Standard</a></div><div class="plan"><strong>Pro</strong><br>$9/month<br><a href="{LINK_PRO}">Choose Pro</a></div><div class="plan"><strong>Annual</strong><br>$79/year<br><a href="{LINK_ANNUAL}">Choose Annual</a></div></div><small>Secure checkout by Stripe. Cancel recurring plans through Stripe. This is historical data, not synthetic monitoring or an uptime guarantee.</small></div>
<p><small>Collected from official public status APIs with source URLs, timestamps, and normalized incident details. Sources: {esc(source_list)}. Built by statusfeed-dev.</small></p></body></html>'''
    OUT.write_text(page, encoding="utf-8")
    stats = {
        "total": len(rows),
        "last30d": len(recent),
        "coverage_start": min((r["started_at"] for r in rows if r["started_at"]), default=None),
        "coverage_end": max((r["started_at"] for r in rows if r["started_at"]), default=None),
        "computed_at": now.isoformat(),
        "by_source": provider_counts,
        "unresolved": len(unresolved),
    }
    (ROOT / "stats.json").write_text(__import__("json").dumps(stats, indent=2) + "\n", encoding="utf-8")
    print(f"generated {OUT.name}: {len(rows)} total, {len(recent)} recent, {len(unresolved)} unresolved, {len(providers)} sources")


if __name__ == "__main__":
    main()

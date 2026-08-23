#!/usr/bin/env python3
"""Generate the public StatusPulse page and sample from the local collector DB."""
import csv
import html
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB = ROOT.parent / "statusfeed.db"
OUT = ROOT / "index.html"
SAMPLE = ROOT / "statuspulse-sample.csv"
LINK_STANDARD = "https://buy.stripe.com/9B6eVe0wp7eGaVc1bT8og00"
LINK_FOUNDING = "https://buy.stripe.com/bJe5kE4MF2Yq2oG2fX8og01"


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
    provider_mttr = {}
    for r in rows:
        provider_counts[r["source"]] = provider_counts.get(r["source"], 0) + 1
        h = duration_hours(r["started_at"], r["resolved_at"])
        if h is not None:
            provider_mttr.setdefault(r["source"], []).append(h)
    for source in provider_mttr:
        vals = sorted(provider_mttr[source])
        n = len(vals)
        provider_mttr[source] = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2

    sample_fields = ["id", "source", "name", "status", "impact", "started_at", "resolved_at", "fetched_at"]
    with SAMPLE.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=sample_fields)
        writer.writeheader()
        writer.writerows(dict(r) for r in rows[:100])

    incident_rows = "\n".join(
        f'<tr><td>{esc(r["source"])}</td><td>{esc(r["name"])}</td><td><span class="status {esc(r["status"])}">{esc(r["status"])}</span></td><td>{esc(r["impact"])}</td><td>{esc((r["started_at"] or "")[:19].replace("T", " "))}</td></tr>'
        for r in recent[:25]
    )
    score_rows = "\n".join(
        f'<tr><td>{esc(source)}</td><td>{provider_counts[source]}</td><td>{provider_mttr.get(source, 0):.2f} h</td></tr>'
        for source in sorted(provider_counts, key=lambda s: (-provider_counts[s], s))
    )
    impact_items = " · ".join(f"{esc(k)}: {v}" for k, v in sorted(impacts.items()))
    generated = now.strftime("%Y-%m-%d %H:%M UTC")
    source_list = ", ".join(providers)
    page = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="Continuously refreshed, queryable incident history from official cloud-provider status pages.">
<title>StatusPulse — incident history and vendor reliability data</title>
<style>
body{{font-family:system-ui,-apple-system,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;color:#17202a;line-height:1.45}}
h1 span{{color:#087f5b}} h2{{margin-top:2rem}} a{{color:#0866c6}} .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:.8rem}}
.card{{border:1px solid #d9e2e8;border-radius:10px;padding:1rem;background:#fbfdfd}} .big{{font-size:1.7rem;font-weight:700}}
table{{border-collapse:collapse;width:100%;font-size:.9rem}}td,th{{padding:.55rem .6rem;border-bottom:1px solid #e5e7eb;text-align:left;vertical-align:top}}
.status{{border-radius:999px;padding:.15rem .5rem;background:#eef2f4;font-size:.8rem}}.resolved{{color:#087f5b;background:#d3f9d8}}.investigating,.identified{{color:#9a3412;background:#ffedd5}}
.cta{{background:#e6fcf5;border:1px solid #96f2d7;border-radius:10px;padding:1rem}}small{{color:#59636e}}
</style></head><body>
<h1>Status<span>Pulse</span></h1>
<p><strong>Evidence for vendor reliability reviews.</strong> StatusPulse preserves official incident timelines across {len(providers)} infrastructure providers in queryable CSV/SQLite form. It is a historical dataset, not synthetic monitoring or an uptime guarantee.</p>
<div class="grid"><div class="card"><div class="big">{len(rows):,}</div>Total incidents</div><div class="card"><div class="big">{len(recent):,}</div>Started in 30 days</div><div class="card"><div class="big">{len(unresolved):,}</div>Currently unresolved</div><div class="card"><div class="big">{len(providers)}</div>Official sources</div></div>
<p><small>Last generated {generated}. Sources: {esc(source_list)}. Recent impact mix: {impact_items or "none"}.</small></p>
<h2>Recent incident timeline</h2><p>Use this view to investigate recurring vendor failures, compare incident impact, and retain evidence beyond a provider's dashboard history.</p>
<table><tr><th>Provider</th><th>Incident</th><th>Status</th><th>Impact</th><th>Started</th></tr>{incident_rows}</table>
<h2>Provider coverage and median resolution time</h2><table><tr><th>Provider</th><th>Incidents captured</th><th>Median resolved duration</th></tr>{score_rows}</table>
<div class="cta"><h2>Download or subscribe</h2><p><a href="statuspulse-sample.csv" download>Download the current 100-row CSV sample</a> · <a href="https://github.com/statusfeed-dev/statuspulse-export">Use the free CLI exporter</a></p><p>The paid release includes the current full CSV/SQLite dataset for SRE, platform, vendor-management, and reliability-research workflows.</p><p><a href="{LINK_STANDARD}">Subscribe — $5/month</a> · <a href="{LINK_FOUNDING}">Founding member — $3/month</a></p><small>Initial subscribers receive the current release. Automated subscriber delivery is being connected; do not rely on this page as an API endpoint.</small></div>
<p><small>Collected from official public Statuspage API endpoints with source provenance. Built by statusfeed-dev.</small></p></body></html>'''
    OUT.write_text(page, encoding="utf-8")
    stats = {
        "total": len(rows),
        "last30d": len(recent),
        "coverage_start": min((r["started_at"] for r in rows), default=None),
        "coverage_end": max((r["started_at"] for r in rows), default=None),
        "computed_at": now.isoformat(),
        "by_source": provider_counts,
        "unresolved": len(unresolved),
    }
    (ROOT / "stats.json").write_text(__import__("json").dumps(stats, indent=2) + "\n", encoding="utf-8")
    print(f"generated {OUT.name}: {len(rows)} total, {len(recent)} recent, {len(unresolved)} unresolved, {len(providers)} sources")


if __name__ == "__main__":
    main()

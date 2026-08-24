#!/usr/bin/env python3
"""Collect official public incident histories into statusfeed.db."""
import datetime
import json
import sqlite3
import sys
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB = BASE / "statusfeed.db"
UA = "StatusPulse-collector/0.2 (+https://statusfeed-dev.github.io/statuspulse/)"

STATUSPAGE_SOURCES = {
    "github": "https://www.githubstatus.com/api/v2/incidents.json",
    "cloudflare": "https://www.cloudflarestatus.com/api/v2/incidents.json",
    "atlassian": "https://status.atlassian.com/api/v2/incidents.json",
    "datadog": "https://status.datadoghq.com/api/v2/incidents.json",
    "twilio": "https://status.twilio.com/api/v2/incidents.json",
    "vercel": "https://www.vercel-status.com/api/v2/incidents.json",
    "supabase": "https://status.supabase.com/api/v2/incidents.json",
    "digitalocean": "https://status.digitalocean.com/api/v2/incidents.json",
    "openai": "https://status.openai.com/api/v2/incidents.json",
}
GCP_URL = "https://status.cloud.google.com/incidents.json"
AWS_URL = "https://status.aws.amazon.com/data.json"


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as response:
        raw = response.read()
        # AWS currently serves UTF-16; JSON endpoints generally use UTF-8.
        return json.loads(raw.decode("utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8"))


def init_db():
    db = sqlite3.connect(DB)
    db.execute("""CREATE TABLE IF NOT EXISTS incidents(
        id TEXT PRIMARY KEY, source TEXT, name TEXT, status TEXT,
        impact TEXT, started_at TEXT, resolved_at TEXT, fetched_at TEXT,
        source_url TEXT, details TEXT)""")
    # Migrate the original 8-column database without losing existing data.
    cols = {r[1] for r in db.execute("PRAGMA table_info(incidents)")}
    for name, kind in (("source_url", "TEXT"), ("details", "TEXT")):
        if name not in cols:
            db.execute(f"ALTER TABLE incidents ADD COLUMN {name} {kind}")
    db.commit()
    return db


def normalize_statuspage(source, url, data, now):
    for inc in data.get("incidents", []):
        updates = inc.get("incident_updates") or []
        resolved_at = None
        if inc.get("status") == "resolved":
            resolved_at = inc.get("resolved_at") or (updates[0].get("updated_at") if updates else None)
        details = {
            "shortlink": inc.get("shortlink"),
            "page_url": f"https://{url.split('/')[2]}/incidents/{inc.get('id')}",
            "components": [c.get("name") for c in (inc.get("components") or [])],
            "update_count": len(updates),
        }
        yield (f"{source}:{inc.get('id')}", source, inc.get("name"), inc.get("status"),
               inc.get("impact"), inc.get("started_at"), resolved_at, now, url,
               json.dumps(details, sort_keys=True))


def normalize_gcp(data, now):
    for inc in data if isinstance(data, list) else []:
        begin = inc.get("begin")
        end = inc.get("end")
        products = inc.get("affected_products") or []
        locations = inc.get("affected_locations") or []
        details = {"number": inc.get("number"), "products": products, "locations": locations,
                   "updated": inc.get("updated"), "external_desc": inc.get("external_desc")}
        yield (f"gcp:{inc.get('id')}", "google-cloud", inc.get("external_desc") or "Google Cloud incident",
               "resolved" if end else "investigating", "major" if inc.get("severity") == "high" else "minor",
               begin, end, now, GCP_URL, json.dumps(details, sort_keys=True))


def normalize_aws(data, now):
    items = data.get("archive", data) if isinstance(data, dict) else data
    if not isinstance(items, list):
        return
    for inc in items:
        # AWS Health public archive fields vary; retain the raw normalized detail.
        started = inc.get("date") or inc.get("startTime")
        ended = inc.get("endDate") or inc.get("endTime")
        arn = inc.get("arn") or inc.get("id") or inc.get("eventArn")
        if not arn or not started:
            continue
        yield (f"aws:{arn}", "aws", inc.get("service") or inc.get("eventTypeCode") or "AWS service incident",
               "resolved" if ended else "investigating", "major", started, ended, now, AWS_URL,
               json.dumps(inc, sort_keys=True, default=str))


def insert_unique(db, row):
    """Insert an incident unless the same natural key (source, name, started_at)
    is already present under a different id — some status pages return the same
    incident more than once with inconsistent ids."""
    existing = db.execute(
        "SELECT id FROM incidents WHERE source=? AND name=? AND started_at=?",
        (row[1], row[2], row[5]),
    ).fetchone()
    if existing and existing[0] != row[0]:
        return False
    db.execute("INSERT OR REPLACE INTO incidents VALUES (?,?,?,?,?,?,?,?,?,?)", row)
    return True


def collect():
    db = init_db()
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    total = inserted = 0
    for source, url in STATUSPAGE_SOURCES.items():
        try:
            for row in normalize_statuspage(source, url, fetch(url), now):
                if insert_unique(db, row):
                    inserted += 1
                total += 1
        except Exception as exc:
            print(f"{source}: ERROR {exc}", file=sys.stderr)
    for label, url, normalizer in (("google-cloud", GCP_URL, normalize_gcp), ("aws", AWS_URL, normalize_aws)):
        try:
            for row in normalizer(fetch(url), now):
                if insert_unique(db, row):
                    inserted += 1
                total += 1
        except Exception as exc:
            print(f"{label}: ERROR {exc}", file=sys.stderr)
    db.commit()
    print(f"collected {total} incidents, inserted {inserted}")
    return total


if __name__ == "__main__":
    collect()

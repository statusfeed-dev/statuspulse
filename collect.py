#!/usr/bin/env python3
"""Collect public status-page incidents into statusfeed.db (official public APIs only)."""
import json, sqlite3, urllib.request, datetime, sys

DB = __import__('os').path.join(__import__('os').path.dirname(__file__), 'statusfeed.db')

SOURCES = {
    "github": "https://www.githubstatus.com/api/v2/incidents.json",
    "cloudflare": "https://www.cloudflarestatus.com/api/v2/incidents.json",
    "atlassian": "https://status.atlassian.com/api/v2/incidents.json",
    "datadog": "https://status.datadoghq.com/api/v2/incidents.json",
    "twilio": "https://status.twilio.com/api/v2/incidents.json",
    "vercel": "https://www.vercel-status.com/api/v2/incidents.json",
    "supabase": "https://status.supabase.com/api/v2/incidents.json",
    "digitalocean": "https://status.digitalocean.com/api/v2/incidents.json",
}

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "statusfeed-collector/0.1"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())

def init_db():
    db = sqlite3.connect(DB)
    db.execute("""CREATE TABLE IF NOT EXISTS incidents(
        id TEXT PRIMARY KEY, source TEXT, name TEXT, status TEXT,
        impact TEXT, started_at TEXT, resolved_at TEXT, fetched_at TEXT)""")
    db.commit()
    return db

def collect():
    db = init_db()
    now = datetime.datetime.utcnow().isoformat()
    n = 0
    for src, url in SOURCES.items():
        try:
            data = fetch(url)
        except Exception as e:
            print(f"{src}: ERROR {e}", file=sys.stderr)
            continue
        for inc in data.get("incidents", []):
            updates = inc.get("incident_updates") or []
            resolved = updates[0].get("updated_at") if inc.get("status") == "resolved" and updates else None
            db.execute("INSERT OR REPLACE INTO incidents VALUES (?,?,?,?,?,?,?,?)",
                (str(inc.get("id")), src, inc.get("name"), inc.get("status"),
                 inc.get("impact"), inc.get("started_at"), resolved, now))
            n += 1
    db.commit()
    print(f"collected {n} incidents")
    return n

if __name__ == "__main__":
    collect()

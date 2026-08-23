#!/usr/bin/env python3
"""Export recent incidents from an official Statuspage API as CSV.

Usage: python3 statuspulse-export.py https://www.githubstatus.com 30 incidents.csv
"""
import csv
import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone


def main():
    if len(sys.argv) not in (2, 3, 4):
        raise SystemExit("usage: statuspulse-export.py STATUSPAGE_URL [DAYS] [OUTPUT.csv]")
    base = sys.argv[1].rstrip("/")
    days = int(sys.argv[2]) if len(sys.argv) >= 3 else 30
    output = sys.argv[3] if len(sys.argv) == 4 else "incidents.csv"
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    req = urllib.request.Request(
        base + "/api/v2/incidents.json",
        headers={"User-Agent": "statuspulse-export/0.1 (+https://statusfeed-dev.github.io/statuspulse/)"},
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        payload = json.load(response)
    rows = []
    for incident in payload.get("incidents", []):
        started = incident.get("started_at") or ""
        try:
            if started and datetime.fromisoformat(started.replace("Z", "+00:00")) < cutoff:
                continue
        except ValueError:
            pass
        rows.append({
            "id": incident.get("id", ""),
            "name": incident.get("name", ""),
            "status": incident.get("status", ""),
            "impact": incident.get("impact", ""),
            "started_at": started,
            "resolved_at": incident.get("resolved_at", ""),
            "shortlink": incident.get("shortlink", ""),
        })
    fields = ["id", "name", "status", "impact", "started_at", "resolved_at", "shortlink"]
    with open(output, "w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {len(rows)} incidents to {output}")


if __name__ == "__main__":
    main()

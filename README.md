# StatusPulse

Cross-provider status-page incident history. Live demo: `index.html` (serve via GitHub Pages from repo root).

## Files
- `index.html` — public teaser page (recent incidents + waitlist form)
- `statuspulse-sample.csv` — free 100-row sample
- `collect.py` — hourly collector (official public status APIs only, respects rate limits)
- `gumroad-listing.md` — product listing draft

## Data schema
`id, source, name, status, impact, started_at, resolved_at, fetched_at`

## Sources (all official /api/v2 endpoints)
GitHub · Cloudflare · Datadog · Vercel · Supabase · Twilio · Atlassian · DigitalOcean

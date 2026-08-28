# StatusPulse

StatusPulse turns source-policy-gated incident facts and cited primary-source links into fixed-scope dependency evidence briefs for agencies, MSPs, and small SaaS teams.

**Live site:** https://statusfeed-dev.github.io/statuspulse/

## Pilot offer

The validation offer is a **$79 one-time Vendor Dependency Evidence Brief** for up to 20 named SaaS or cloud dependencies. It includes:

- A print-ready, cited report.
- A supporting CSV with direct source URLs.
- Official status, support, service-health, and policy links.
- Source-policy-gated incident metrics, evidence gaps, and due-diligence questions.
- Delivery within two business days.
- One factual-correction pass.

It is not a subscription, real-time status service, synthetic monitoring, independent audit, SLA determination, legal advice, or uptime guarantee. The first validation link is US-only and capped at three completed purchases.

## Public preview and private release boundary

The public site intentionally contains only an allowlisted preview:

- Generated landing page and methodology.
- A current sample of at most 100 records with source URLs.
- Aggregate statistics.
- Terms, privacy, refund, and support pages.

The full SQLite database and full CSV are private R2 objects. `statusfeed.db`, `statuspulse.csv`, local operations databases, and release working directories are ignored by Git and rejected by the Pages build. Historical repository commits may still contain older snapshots, so the product does not claim that previously published facts are exclusive.

## Data refresh

`.github/workflows/refresh-data.yml` is the only scheduled collector. It:

1. Downloads the canonical private SQLite database from R2.
2. Validates database integrity and schema.
3. Runs the deterministic Python collector.
4. Regenerates the public preview and full private CSV from the same database.
5. Rejects row loss or inconsistent artifacts.
6. Writes versioned recovery objects before replacing the canonical R2 objects.
7. Commits only explicit public artifacts.

The collector fails closed against `source_policy.py`: it requests and retains only sources with documented supported public integration paths or separate written permission. Hermes must not run a duplicate collector.

## Local verification

The Python implementation uses only the standard library:

```bash
python3 -m unittest -v
python3 generate_site.py
mkdir -p .release
python3 export_release.py statusfeed.db .release/statuspulse.csv
python3 validate_release.py release \
  --database statusfeed.db \
  --stats stats.json \
  --sample statuspulse-sample.csv \
  --mttr-stats mttr_stats.json \
  --full-csv .release/statuspulse.csv \
  --minimum-rows 1
```

The production Cloudflare Worker should be tested with the repository's Worker test suite before deployment. Secrets belong in platform secret stores, never source control or command output.

## Order notification and delivery

`order_notifier.py` independently polls Stripe for paid pilot orders. It records
each order in the private `ops.db` ledger and sends the owner a minimal Telegram
alert without copying customer intake into Telegram. After delivery, start the
90-day intake-retention window:

```bash
python3 order_notifier.py mark-delivered cs_live_REPLACE_ME
```

Hardened user-service templates are in `deploy/`. Their private environment
file must contain only `STRIPE_API_KEY`, `STATUSPULSE_PAYMENT_LINK_ID`,
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_HOME_CHANNEL`, and `STATUSPULSE_OPS_DB`, and
must use mode `0600`.

## Data fields

The private normalized database uses:

`id, source, name, status, impact, started_at, resolved_at, fetched_at, source_url, details`

The public sample omits raw `details` and provider narratives but includes `source_url`, allowing a buyer to inspect provenance.

## Key files

- `collect.py` — deterministic official-feed collector.
- `generate_site.py` — public page, sample, and aggregate-stat generation.
- `release_contract.py` — shared schema, validation, and public allowlist rules.
- `validate_release.py` — release and Pages-boundary validation commands.
- `export_release.py` — atomic full-release CSV export.
- `order_notifier.py` — private order ledger, owner notification, and intake retention.
- `worker.js` — Stripe webhook and private order-state boundary.
- `schema.sql` — production D1 schema.
- `stripe-listing.md` — exact product and checkout configuration.

StatusPulse is not affiliated with, endorsed by, or sponsored by any listed provider. Provider names identify the services analyzed; calculations and commentary are StatusPulse's.

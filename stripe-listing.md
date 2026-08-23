# StatusPulse — Stripe listing copy

## Product name
StatusPulse Incident History — recurring data release

## Short description
Continuously refreshed cross-provider incident history for SRE, platform, vendor-management, and reliability-research workflows.

## Full description
StatusPulse preserves incident timelines from official public Statuspage API endpoints across GitHub, Cloudflare, Datadog, Vercel, Supabase, Twilio, Atlassian, and DigitalOcean.

The paid release is a current downloadable CSV and SQLite snapshot with source, incident name, status, impact, start/resolution timestamps, and fetch provenance. Use it to support vendor reviews, postmortems, reliability research, SLO reporting, and historical comparisons without manually collecting eight status pages.

This is historical incident data, not synthetic monitoring, an uptime guarantee, or an API endpoint. The initial subscriber delivery is the current release; automated subscriber delivery is being connected.

## Plans

- **Standard — $5/month:** current full CSV + SQLite release.
- **Founding member — $3/month:** same current release at the founding rate while available.

## Included fields

`id, source, name, status, impact, started_at, resolved_at, fetched_at`

## Free preview

- Live preview: https://statusfeed-dev.github.io/statuspulse/
- 100-row sample: https://statusfeed-dev.github.io/statuspulse/statuspulse-sample.csv
- Free CLI exporter: https://github.com/statusfeed-dev/statuspulse-export

## Checkout links

- Standard: https://buy.stripe.com/9B6eVe0wp7eGaVc1bT8og00
- Founding member: https://buy.stripe.com/bJe5kE4MF2Yq2oG2fX8og01

## Fulfillment note

Until webhook-backed entitlement and delivery are deployed, fulfill a successful subscription manually with the current CSV/SQLite release. Do not promise automatic hourly delivery. Never place Stripe secret or webhook keys in this repository.

## Refund/support policy draft

If the first release is not usable for the stated fields and coverage, contact the publisher within 7 days for support or a refund review. Coverage depends on the availability and content of each provider's official public status endpoint.

## Keywords

incident history, vendor reliability, SRE data, status page data, postmortem analysis, vendor due diligence, MTTR, SQLite, CSV, cloud infrastructure

*Not affiliated with Atlassian or any listed provider.*

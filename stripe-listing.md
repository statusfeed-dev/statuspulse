# StatusPulse — Stripe listing copy

## Product name
StatusPulse Incident History — recurring data release

## Short description
Continuously refreshed cross-provider incident history for SRE, platform, vendor-management, and reliability-research workflows.

## Full description
StatusPulse preserves incident timelines from official public Statuspage API endpoints across GitHub, Cloudflare, Datadog, Vercel, Supabase, Twilio, Atlassian, and DigitalOcean.

The paid release is a current downloadable CSV and SQLite snapshot with source, incident name, status, impact, start/resolution timestamps, and fetch provenance. Use it to support vendor reviews, postmortems, reliability research, SLO reporting, and historical comparisons without manually collecting official status pages.

This is historical incident data, not synthetic monitoring, an uptime guarantee, or an API endpoint. Subscribers receive authenticated access to the current release immediately after successful checkout.

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

## Fulfillment implementation

`fulfillment.py` implements the deployable fulfillment boundary. It verifies Stripe webhook signatures with a five-minute tolerance, rejects live-mode fixture events during local/test use, processes events idempotently, grants access only for paid subscription checkouts, revokes canceled subscriptions, and protects both full-dataset formats behind an expiring authenticated session.

## Manual production steps

No Stripe resources or deployments are created by this repository. Before production, an operator must:

1. Deploy `fulfillment.py` behind HTTPS with persistent storage for `ops.db` and read access to the refreshed `statusfeed.db`.
2. Set `STRIPE_WEBHOOK_SECRET` in the platform secret manager (never `.env` or source control). Optionally set `STATUSPULSE_OPS_DB`, `STATUSPULSE_DATASET_DB`, and `PORT`.
3. In Stripe, set each Checkout/Payment Link success redirect to `https://YOUR_FULFILLMENT_HOST/checkout/success?session_id={CHECKOUT_SESSION_ID}`.
4. Create a Stripe webhook endpoint at `https://YOUR_FULFILLMENT_HOST/stripe/webhook` for `checkout.session.completed` and `customer.subscription.deleted`, then place its signing secret in the platform secret manager.
5. First repeat the complete flow in Stripe test mode. Confirm an unpaid or invalidly signed event grants nothing, a paid subscription enables both downloads, replaying the event changes nothing, and cancellation revokes access.
6. Only after the test flow passes, explicitly set `STATUSPULSE_ALLOW_LIVE=true` in the production secret/configuration manager. Rotate the production signing secret if it is ever exposed, and configure backups/retention for the minimal operations database.

The current implementation deliberately does not create Stripe customers, prices, charges, webhooks, or deployments and does not send email. Checkout's success redirect is the delivery path.

## Refund/support policy draft

If the first release is not usable for the stated fields and coverage, contact the publisher within 7 days for support or a refund review. Coverage depends on the availability and content of each provider's official public status endpoint.

## Keywords

incident history, vendor reliability, SRE data, status page data, postmortem analysis, vendor due diligence, MTTR, SQLite, CSV, cloud infrastructure

*Not affiliated with Atlassian or any listed provider.*

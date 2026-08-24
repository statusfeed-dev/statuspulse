# StatusPulse

Cross-provider status-page incident history for SRE, platform, vendor-management, and reliability-research workflows.

**Live preview and paid path:** https://statusfeed-dev.github.io/statuspulse/

## Free lead magnets

- Download the current 100-row sample: https://statusfeed-dev.github.io/statuspulse/statuspulse-sample.csv
- Use the dependency-free [statuspulse-export.py](statuspulse-export.py) CLI to export recent incidents from any official Statuspage.io-powered endpoint.
- Public lead-magnet repo: https://github.com/statusfeed-dev/statuswatch

Example:

```bash
python3 statuspulse-export.py https://www.githubstatus.com 30 incidents.csv
```

## Paid release

The paid release is a current downloadable CSV + SQLite snapshot covering official incident timelines from GitHub, Cloudflare, Datadog, Vercel, Supabase, Twilio, Atlassian, DigitalOcean, OpenAI, Google Cloud, and AWS.

- Founding member: **$3/month** — [subscribe](https://buy.stripe.com/bJe5kE4MF2Yq2oG2fX8og01)
- Standard: **$5/month** — [subscribe](https://buy.stripe.com/9B6eVe0wp7eGaVc1bT8og00)
- Pro: **$9/month** — [subscribe](https://buy.stripe.com/6oUdRacf756ybZgf2J8og02)
- Annual: **$79/year** — [subscribe](https://buy.stripe.com/bJe8wQ92VbuW4wO4o58og03)

The production fulfillment Worker is live at https://statuspulse-fulfillment.statuspulse.workers.dev. It grants authenticated, 24-hour access to the current release after a verified Stripe Checkout webhook. The static preview itself is not an API endpoint.

## Paid fulfillment service

The reference service uses only the Python standard library. The production Worker uses D1 and R2. A valid `checkout.session.completed` webhook for a paid subscription creates an entitlement. Stripe Checkout redirects to:

`https://YOUR_FULFILLMENT_HOST/checkout/success?session_id={CHECKOUT_SESSION_ID}`

That redirect exchanges the non-guessable Checkout Session ID for a random, expiring, `Secure`/`HttpOnly` cookie. The CSV is generated from the current `statusfeed.db`; the SQLite download returns that same current database. Both download endpoints re-check the active entitlement. A `customer.subscription.deleted` event revokes it. The local operations database stores only Stripe event IDs, Checkout Session IDs, subscription IDs, active state, timestamps, and hashes of access tokens—no customer, payment, or card data.

Local test setup (fixture values only):

```bash
python3 -m unittest -v
STRIPE_WEBHOOK_SECRET=fixture_signing_secret PORT=8000 python3 fulfillment.py
```

Do not put secrets in `.env`, source control, command output, or client-side code. Supply production secrets through the hosting platform's secret manager. See [stripe-listing.md](stripe-listing.md) for the production checklist.

Each record includes source URL and collection timestamp. Statuspage records also retain normalized component and update-count details; provider-specific records retain additional raw public metadata in `details`. AWS/Azure/GCP/OpenAI coverage may have different public-history windows and schemas, so compare only with the stated source and coverage metadata.

## Files

- `index.html` — generated public teaser page
- `statuspulse-sample.csv` — free 100-row sample
- `collect.py` — hourly collector using official public status APIs only
- `generate_site.py` — regenerates the page and sample from the local DB
- `statuspulse-export.py` — free CLI lead magnet
- `fulfillment.py` — local reference implementation and tests for signed webhook processing, entitlements, authentication, and downloads
- `worker.js` — production Cloudflare Worker fulfillment boundary
- `test_fulfillment.py` — fulfillment security and download tests
- `stripe-listing.md` — product copy and fulfillment boundary

## Data schema

`id, source, name, status, impact, started_at, resolved_at, fetched_at`

All collection uses official `/api/v2` endpoints and respects rate limits. Not affiliated with any listed provider.

## License

The CLI lead magnet is MIT licensed in its separate public repository. The collected dataset is offered under the checkout terms.

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

- Standard: **$5/month** — [subscribe](https://buy.stripe.com/9B6eVe0wp7eGaVc1bT8og00)
- Founding member: **$3/month** — [subscribe](https://buy.stripe.com/bJe5kE4MF2Yq2oG2fX8og01)

The initial subscriber delivery is the current release. Automated subscriber delivery is being connected; this static site is not an API endpoint.

Each record includes source URL and collection timestamp. Statuspage records also retain normalized component and update-count details; provider-specific records retain additional raw public metadata in `details`. AWS/Azure/GCP/OpenAI coverage may have different public-history windows and schemas, so compare only with the stated source and coverage metadata.

## Files

- `index.html` — generated public teaser page
- `statuspulse-sample.csv` — free 100-row sample
- `collect.py` — hourly collector using official public status APIs only
- `generate_site.py` — regenerates the page and sample from the local DB
- `statuspulse-export.py` — free CLI lead magnet
- `stripe-listing.md` — product copy and fulfillment boundary

## Data schema

`id, source, name, status, impact, started_at, resolved_at, fetched_at`

All collection uses official `/api/v2` endpoints and respects rate limits. Not affiliated with any listed provider.

## License

The CLI lead magnet is MIT licensed in its separate public repository. The collected dataset is offered under the checkout terms.

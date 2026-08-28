# StatusPulse — Stripe listing

## Product

**Vendor Dependency Evidence Brief — Pilot**

One fixed-scope evidence brief for up to 20 named SaaS or cloud dependencies. Includes an official-source map, source-policy-gated normalized incident metrics, evidence gaps, due-diligence questions, a print-ready report, supporting CSV, direct source links, and one factual-correction pass. Delivered to the checkout email within two business days.

This is not synthetic monitoring, an uptime or SLA guarantee, legal advice, or an independent audit.

## Price and capacity

- **US $79 one time.** No recurring charge.
- Maximum three completed pilot checkouts.
- Initial sales are limited to US customers.
- Applicable tax is calculated by Stripe Checkout under the account's configured tax settings.

## Checkout intake

Required:

- Business name.
- Checkout email.
- A comma-separated list of up to 20 non-confidential vendor names.
- Acceptance of the published Terms. The link must remain inactive until Stripe Public details contains the Terms URL and Checkout enforces acceptance.

Optional:

- Primary purpose: renewal review, vendor due diligence, architecture review, or other.

Do not request credentials, customer records, security findings, personal data, health data, payment data, or other sensitive information in a custom field.

## Public policies

- Terms: https://statusfeed-dev.github.io/statuspulse/terms.html
- Privacy: https://statusfeed-dev.github.io/statuspulse/privacy.html
- Refunds: https://statusfeed-dev.github.io/statuspulse/refunds.html
- Support: https://statusfeed-dev.github.io/statuspulse/support.html

The Stripe account's public business details must identify the seller and provide a private support contact before the link is published.

## Fulfillment contract

The production webhook must accept only the configured pilot Payment Link. Before queuing an order it verifies:

- Live/test mode is explicitly enabled for the environment.
- Checkout mode is `payment`.
- Payment status is `paid`.
- The Payment Link ID matches the environment allowlist.
- Offer metadata matches `vendor_reliability_pilot`, version `1`.
- Currency is USD and total is exactly 7900 cents plus any separately reported tax.

The handler records events idempotently, never treats the confirmation-page view as delivery, and tracks asynchronous payment and refund states. Refund initiation remains owner-controlled.

## Validation gate

Before publishing the live URL:

1. Deploy and pass Worker tests.
2. Complete the same flow with a separate Stripe sandbox key and webhook.
3. Confirm a wrong link, wrong amount, unpaid session, replayed event, and invalid signature queue nothing.
4. Confirm the four retired subscription links are inactive.
5. Confirm the new Price is one-time USD 7900 and the link is capped at three completions.
6. Confirm Stripe public business, Terms, Privacy, support, tax, and receipt settings in the Dashboard.

Refunds are approved and initiated by the owner through Stripe. The agent may detect and report refund events but may not initiate money movement.

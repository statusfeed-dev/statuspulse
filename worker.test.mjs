import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import test from "node:test";

import worker, {
  ValidationError,
  deriveOrderState,
  mergeCheckoutState,
  validatePilotSession,
  verifyStripeSignature,
} from "./worker.js";

const SECRET = "whsec_fixture_only";
const PAYMENT_LINK = "plink_fixture";
const SCHEMA = readFileSync(new URL("./schema.sql", import.meta.url), "utf8");
const MIGRATION = readFileSync(
  new URL("./migrations/0001_statuspulse_order_state_v2.sql", import.meta.url),
  "utf8",
);

function applyMigration(database) {
  database.exec("BEGIN IMMEDIATE");
  try {
    database.exec(MIGRATION);
    database.exec("COMMIT");
  } catch (error) {
    database.exec("ROLLBACK");
    throw error;
  }
}

class D1StatementFixture {
  constructor(database, sql, parameters = []) {
    this.database = database;
    this.sql = sql;
    this.parameters = parameters;
  }

  bind(...parameters) {
    return new D1StatementFixture(this.database, this.sql, parameters);
  }

  async first() {
    return this.database.prepare(this.sql).get(...this.parameters) ?? null;
  }

  async all() {
    return { results: this.database.prepare(this.sql).all(...this.parameters) };
  }

  async run() {
    this.runSync();
    return { success: true };
  }

  runSync() {
    return this.database.prepare(this.sql).run(...this.parameters);
  }
}

class D1DatabaseFixture {
  constructor() {
    this.database = new DatabaseSync(":memory:");
    this.database.exec(SCHEMA);
  }

  prepare(sql) {
    return new D1StatementFixture(this.database, sql);
  }

  async batch(statements) {
    this.database.exec("BEGIN IMMEDIATE");
    try {
      const results = statements.map((statement) => statement.runSync());
      this.database.exec("COMMIT");
      return results;
    } catch (error) {
      this.database.exec("ROLLBACK");
      throw error;
    }
  }

  one(sql, ...parameters) {
    const row = this.database.prepare(sql).get(...parameters);
    return row ? { ...row } : null;
  }

  all(sql, ...parameters) {
    return this.database.prepare(sql).all(...parameters);
  }

  close() {
    this.database.close();
  }
}

function checkout(overrides = {}) {
  return {
    id: "cs_test_fixture",
    object: "checkout.session",
    payment_link: PAYMENT_LINK,
    mode: "payment",
    metadata: {
      offer_id: "vendor_reliability_pilot",
      offer_version: "1",
    },
    currency: "usd",
    amount_subtotal: 7900,
    amount_total: 8374,
    total_details: { amount_tax: 474, amount_discount: 0 },
    automatic_tax: { enabled: true, status: "complete" },
    tax_id_collection: { enabled: true },
    billing_address_collection: "required",
    payment_status: "paid",
    payment_intent: "pi_test_fixture",
    consent: { terms_of_service: "accepted" },
    customer_details: {
      email: "buyer@example.com",
      address: { country: "US" },
    },
    collected_information: { business_name: "Fixture LLC" },
    custom_fields: [
      {
        key: "dependencies",
        type: "text",
        text: { value: "GitHub, Cloudflare, Vercel" },
      },
      {
        key: "reportfocus",
        type: "dropdown",
        dropdown: { value: "duediligence" },
      },
    ],
    status: "complete",
    ...overrides,
  };
}

function refund({
  id = "re_fixture",
  paymentIntent = "pi_test_fixture",
  amount = 8374,
  status = "succeeded",
} = {}) {
  return {
    id,
    object: "refund",
    payment_intent: paymentIntent,
    amount,
    currency: "usd",
    status,
  };
}

let nextStripeEventCreated = 1_800_000_000;

function stripeEvent(id, type, object, options = {}) {
  return {
    id,
    type,
    created: options.created ?? nextStripeEventCreated++,
    livemode: true,
    data: { object },
  };
}

async function stripeSignature(payload, timestamp) {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(SECRET),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const digest = await crypto.subtle.sign(
    "HMAC",
    key,
    new TextEncoder().encode(`${timestamp}.${payload}`),
  );
  const value = [...new Uint8Array(digest)]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
  return `t=${timestamp},v1=${value}`;
}

async function postEvent(database, event, environment = {}) {
  const payload = JSON.stringify(event);
  const timestamp = Math.floor(Date.now() / 1000);
  const request = new Request("https://example.test/stripe/webhook", {
    method: "POST",
    body: payload,
    headers: { "Stripe-Signature": await stripeSignature(payload, timestamp) },
  });
  return worker.fetch(request, {
    DB: database,
    STRIPE_WEBHOOK_SECRET: SECRET,
    STATUSPULSE_ALLOW_LIVE: "true",
    STATUSPULSE_PAYMENT_LINK_ID: PAYMENT_LINK,
    ...environment,
  });
}

async function expectProcessed(database, event) {
  const result = await postEvent(database, event);
  assert.equal(result.status, 200);
  assert.equal(await result.text(), "processed\n");
}

test("verifies a current Stripe webhook signature", async () => {
  const now = 1_800_000_000_000;
  const timestamp = Math.floor(now / 1000);
  const payload = JSON.stringify({ id: "evt_fixture" });
  const signature = await stripeSignature(payload, timestamp);

  const event = await verifyStripeSignature(payload, signature, SECRET, now);

  assert.equal(event.id, "evt_fixture");
});

test("rejects an expired Stripe webhook signature", async () => {
  const now = 1_800_000_000_000;
  const payload = JSON.stringify({ id: "evt_fixture" });
  const signature = await stripeSignature(payload, Math.floor(now / 1000) - 301);

  await assert.rejects(
    verifyStripeSignature(payload, signature, SECRET, now),
    ValidationError,
  );
});

test("rejects an oversized streaming webhook body before signature or D1 work", async () => {
  const request = new Request("https://example.test/stripe/webhook", {
    method: "POST",
    body: "x".repeat((1024 * 1024) + 1),
    headers: { "Stripe-Signature": "invalid" },
  });

  const result = await worker.fetch(request, {
    DB: null,
    STRIPE_WEBHOOK_SECRET: SECRET,
    STATUSPULSE_ALLOW_LIVE: "true",
    STATUSPULSE_PAYMENT_LINK_ID: PAYMENT_LINK,
  });

  assert.equal(result.status, 413);
  assert.equal(await result.text(), "payload too large\n");
});

test("accepts the exact pilot contract without returning intake PII", () => {
  const order = validatePilotSession(checkout(), PAYMENT_LINK);

  assert.equal(order.amountSubtotal, 7900);
  assert.equal(order.amountTax, 474);
  assert.equal(order.paymentIntentId, "pi_test_fixture");
  assert.equal(order.eligibilityState, "eligible");
  assert.equal("customerEmail" in order, false);
  assert.equal("businessName" in order, false);
  assert.equal("dependencies" in order, false);
});

test("rejects wrong links, amounts, business names, and oversized scope", () => {
  assert.throws(
    () => validatePilotSession(checkout({ payment_link: "plink_other" }), PAYMENT_LINK),
    /unexpected Payment Link/,
  );
  assert.throws(
    () => validatePilotSession(checkout({ amount_subtotal: 7800 }), PAYMENT_LINK),
    /unexpected subtotal/,
  );
  assert.throws(
    () => validatePilotSession(checkout({ collected_information: {} }), PAYMENT_LINK),
    /business name/,
  );
  assert.throws(
    () => validatePilotSession(checkout({ consent: null }), PAYMENT_LINK),
    /terms consent/,
  );
  assert.throws(
    () => validatePilotSession(
      checkout({ automatic_tax: { enabled: true, status: "failed" } }),
      PAYMENT_LINK,
    ),
    /automatic tax calculation/,
  );
  const outOfScope = validatePilotSession(checkout({
      customer_details: {
        email: "buyer@example.com",
        address: { country: "CA" },
      },
    }), PAYMENT_LINK);
  assert.equal(outOfScope.eligibilityState, "manual_refund_required");
  const dependencies = Array.from({ length: 21 }, (_, index) => `v${index}`).join(",");
  assert.throws(
    () => validatePilotSession(checkout({
      custom_fields: [{ key: "dependencies", type: "text", text: { value: dependencies } }],
    }), PAYMENT_LINK),
    /dependency count/,
  );
});

test("pure state transitions are monotonic and aggregate all refunds", () => {
  assert.equal(mergeCheckoutState("paid", "expired"), "paid");
  assert.equal(mergeCheckoutState("expired", "pending"), "expired");
  assert.deepEqual(
    deriveOrderState(
      "paid",
      8374,
      "queued",
      [
        { amount: 1000, status: "succeeded" },
        { amount: 500, status: "pending" },
      ],
    ),
    {
      paymentState: "refund_pending",
      fulfillmentState: "on_hold",
      refundedAmount: 1000,
    },
  );
});

test("health endpoint verifies the complete D1 schema contract", async (t) => {
  const database = new D1DatabaseFixture();
  t.after(() => database.close());
  const result = await worker.fetch(
    new Request("https://example.test/healthz"),
    { DB: database },
  );

  assert.equal(result.status, 200);
  assert.equal(await result.text(), "ok\n");
});

test("signed unrelated events and checkouts for other links are ignored", async () => {
  const unrelated = await postEvent(null, stripeEvent(
    "evt_unrelated",
    "customer.created",
    { id: "cus_fixture" },
  ));
  assert.equal(unrelated.status, 200);
  assert.equal(await unrelated.text(), "ignored\n");

  const wrongLink = await postEvent(null, stripeEvent(
    "evt_wrong_link",
    "checkout.session.completed",
    checkout({ payment_link: "plink_other" }),
  ));
  assert.equal(wrongLink.status, 200);
  assert.equal(await wrongLink.text(), "ignored\n");

  const ordinaryCheckout = await postEvent(null, stripeEvent(
    "evt_ordinary_checkout",
    "checkout.session.completed",
    checkout({ payment_link: null, metadata: {} }),
  ));
  assert.equal(ordinaryCheckout.status, 200);
  assert.equal(await ordinaryCheckout.text(), "ignored\n");
});

test("D1 order schema contains no customer intake PII columns", (t) => {
  const database = new D1DatabaseFixture();
  t.after(() => database.close());

  const columns = new Set(
    database.all("PRAGMA table_info(orders)").map((column) => column.name),
  );
  for (const forbidden of [
    "customer_email",
    "business_name",
    "dependencies",
    "report_focus",
  ]) {
    assert.equal(columns.has(forbidden), false);
  }
});

test("a refund received before checkout is retained and blocks fulfillment", async (t) => {
  const database = new D1DatabaseFixture();
  t.after(() => database.close());

  const earlyRefund = await postEvent(database, stripeEvent(
    "evt_refund_early",
    "refund.created",
    refund(),
  ));
  assert.equal(earlyRefund.status, 200);
  assert.equal(await earlyRefund.text(), "recorded_unmatched\n");
  assert.equal(database.one("SELECT status FROM refunds WHERE refund_id = ?", "re_fixture").status, "succeeded");

  await expectProcessed(database, stripeEvent(
    "evt_checkout_after_refund",
    "checkout.session.completed",
    checkout(),
  ));
  const order = database.one(
    "SELECT checkout_state, payment_state, fulfillment_state, refunded_amount FROM orders",
  );
  assert.deepEqual(order, {
    checkout_state: "paid",
    payment_state: "refunded",
    fulfillment_state: "canceled",
    refunded_amount: 8374,
  });
});

test("out-of-order checkout events cannot downgrade a paid order", async (t) => {
  const database = new D1DatabaseFixture();
  t.after(() => database.close());

  await expectProcessed(database, stripeEvent(
    "evt_checkout_paid",
    "checkout.session.completed",
    checkout(),
  ));
  await expectProcessed(database, stripeEvent(
    "evt_checkout_expired_late",
    "checkout.session.expired",
    checkout({
      payment_status: "unpaid",
      payment_intent: "pi_test_fixture",
      customer_details: null,
      collected_information: null,
      custom_fields: [],
      status: "expired",
    }),
  ));

  const order = database.one(
    "SELECT checkout_state, payment_state, fulfillment_state FROM orders",
  );
  assert.deepEqual(order, {
    checkout_state: "paid",
    payment_state: "paid",
    fulfillment_state: "queued",
  });
});

test("managed async payment events queue only after paid success", async (t) => {
  const database = new D1DatabaseFixture();
  t.after(() => database.close());
  const pendingSession = checkout({
    payment_status: "unpaid",
    customer_details: null,
    collected_information: null,
    custom_fields: [],
    consent: null,
    payment_method_types: ["us_bank_account"],
  });
  await expectProcessed(database, stripeEvent(
    "evt_async_pending",
    "checkout.session.completed",
    pendingSession,
  ));
  assert.deepEqual(
    database.one("SELECT checkout_state, payment_state, fulfillment_state FROM orders"),
    {
      checkout_state: "pending",
      payment_state: "pending",
      fulfillment_state: "pending",
    },
  );

  await expectProcessed(database, stripeEvent(
    "evt_async_succeeded",
    "checkout.session.async_payment_succeeded",
    checkout({ payment_method_types: ["us_bank_account"] }),
  ));
  assert.deepEqual(
    database.one("SELECT checkout_state, payment_state, fulfillment_state FROM orders"),
    {
      checkout_state: "paid",
      payment_state: "paid",
      fulfillment_state: "queued",
    },
  );

  await expectProcessed(database, stripeEvent(
    "evt_async_failed_late",
    "checkout.session.async_payment_failed",
    pendingSession,
  ));
  assert.deepEqual(
    database.one("SELECT checkout_state, payment_state, fulfillment_state FROM orders"),
    {
      checkout_state: "paid",
      payment_state: "paid",
      fulfillment_state: "queued",
    },
  );
});

test("a succeeded refund that later fails restores the paid order", async (t) => {
  const database = new D1DatabaseFixture();
  t.after(() => database.close());
  await expectProcessed(database, stripeEvent(
    "evt_checkout_refund_transition",
    "checkout.session.completed",
    checkout(),
  ));
  await expectProcessed(database, stripeEvent(
    "evt_refund_succeeded",
    "refund.updated",
    refund(),
  ));
  assert.equal(database.one("SELECT payment_state FROM orders").payment_state, "refunded");

  await expectProcessed(database, stripeEvent(
    "evt_refund_failed",
    "refund.failed",
    refund({ status: "failed" }),
  ));
  const restored = database.one(
    "SELECT payment_state, fulfillment_state, refunded_amount FROM orders",
  );
  assert.deepEqual(restored, {
    payment_state: "paid",
    fulfillment_state: "queued",
    refunded_amount: 0,
  });
});

test("multiple refunds are aggregated and any pending refund holds delivery", async (t) => {
  const database = new D1DatabaseFixture();
  t.after(() => database.close());
  await expectProcessed(database, stripeEvent(
    "evt_checkout_multi_refund",
    "checkout.session.completed",
    checkout(),
  ));
  await expectProcessed(database, stripeEvent(
    "evt_refund_partial",
    "refund.created",
    refund({ id: "re_partial", amount: 1000 }),
  ));
  await expectProcessed(database, stripeEvent(
    "evt_refund_pending",
    "refund.created",
    refund({ id: "re_pending", amount: 500, status: "requires_action" }),
  ));
  assert.deepEqual(
    database.one("SELECT payment_state, fulfillment_state, refunded_amount FROM orders"),
    {
      payment_state: "refund_pending",
      fulfillment_state: "on_hold",
      refunded_amount: 1000,
    },
  );

  await expectProcessed(database, stripeEvent(
    "evt_refund_pending_resolved",
    "refund.updated",
    refund({ id: "re_pending", amount: 500, status: "succeeded" }),
  ));
  assert.deepEqual(
    database.one("SELECT payment_state, fulfillment_state, refunded_amount FROM orders"),
    {
      payment_state: "partially_refunded",
      fulfillment_state: "queued",
      refunded_amount: 1500,
    },
  );
});

test("late older refund events cannot regress the newest Stripe state", async (t) => {
  const database = new D1DatabaseFixture();
  t.after(() => database.close());
  await expectProcessed(database, stripeEvent(
    "evt_checkout_refund_ordering",
    "checkout.session.completed",
    checkout(),
  ));
  await expectProcessed(database, stripeEvent(
    "evt_refund_newer_failed",
    "refund.failed",
    refund({ status: "failed" }),
    { created: 2_000 },
  ));
  await expectProcessed(database, stripeEvent(
    "evt_refund_older_succeeded",
    "refund.updated",
    refund({ status: "succeeded" }),
    { created: 1_000 },
  ));

  assert.deepEqual(
    database.one("SELECT status, stripe_created_at FROM refunds WHERE refund_id = ?", "re_fixture"),
    { status: "failed", stripe_created_at: 2_000 },
  );
  assert.deepEqual(
    database.one("SELECT payment_state, fulfillment_state, refunded_amount FROM orders"),
    { payment_state: "paid", fulfillment_state: "queued", refunded_amount: 0 },
  );
});

test("same-second ambiguous refund updates choose the conservative hold", async (t) => {
  const database = new D1DatabaseFixture();
  t.after(() => database.close());
  await expectProcessed(database, stripeEvent(
    "evt_checkout_refund_tie",
    "checkout.session.completed",
    checkout(),
  ));
  await expectProcessed(database, stripeEvent(
    "evt_refund_tie_failed",
    "refund.failed",
    refund({ status: "failed" }),
    { created: 3_000 },
  ));
  await expectProcessed(database, stripeEvent(
    "evt_refund_tie_pending",
    "refund.updated",
    refund({ status: "pending" }),
    { created: 3_000 },
  ));

  assert.deepEqual(
    database.one("SELECT status, stripe_created_at FROM refunds WHERE refund_id = ?", "re_fixture"),
    { status: "pending", stripe_created_at: 3_000 },
  );
  assert.deepEqual(
    database.one("SELECT payment_state, fulfillment_state FROM orders"),
    { payment_state: "refund_pending", fulfillment_state: "on_hold" },
  );
});

test("concurrent refund delivery finishes with atomically reconciled newest state", async (t) => {
  const database = new D1DatabaseFixture();
  t.after(() => database.close());
  await expectProcessed(database, stripeEvent(
    "evt_checkout_refund_concurrent",
    "checkout.session.completed",
    checkout(),
  ));
  const newer = stripeEvent(
    "evt_refund_concurrent_newer",
    "refund.failed",
    refund({ status: "failed" }),
    { created: 5_000 },
  );
  const older = stripeEvent(
    "evt_refund_concurrent_older",
    "refund.updated",
    refund({ status: "succeeded" }),
    { created: 4_000 },
  );

  const responses = await Promise.all([
    postEvent(database, newer),
    postEvent(database, older),
  ]);
  assert.deepEqual(responses.map((result) => result.status), [200, 200]);
  assert.deepEqual(
    database.one("SELECT status, stripe_created_at FROM refunds WHERE refund_id = ?", "re_fixture"),
    { status: "failed", stripe_created_at: 5_000 },
  );
  assert.deepEqual(
    database.one("SELECT payment_state, fulfillment_state, refunded_amount FROM orders"),
    { payment_state: "paid", fulfillment_state: "queued", refunded_amount: 0 },
  );
});

test("duplicate event IDs are acknowledged without duplicate mutations", async (t) => {
  const database = new D1DatabaseFixture();
  t.after(() => database.close());
  const event = stripeEvent(
    "evt_duplicate",
    "checkout.session.completed",
    checkout(),
  );
  await expectProcessed(database, event);
  const duplicate = await postEvent(database, event);

  assert.equal(duplicate.status, 200);
  assert.equal(await duplicate.text(), "duplicate\n");
  assert.equal(database.one("SELECT COUNT(*) AS count FROM orders").count, 1);
  assert.equal(database.one("SELECT COUNT(*) AS count FROM order_events").count, 1);
});

test("malformed relevant events are quarantined and return retryable errors", async (t) => {
  const database = new D1DatabaseFixture();
  t.after(() => database.close());
  const event = stripeEvent(
    "evt_bad_contract",
    "checkout.session.completed",
    checkout({ amount_subtotal: 7800 }),
  );
  const first = await postEvent(database, event);
  const second = await postEvent(database, event);

  assert.equal(first.status, 503);
  assert.equal(second.status, 503);
  assert.equal(first.headers.get("Retry-After"), "300");
  assert.deepEqual(
    database.one(
      "SELECT reason_code, attempt_count, resolved_at FROM rejected_events WHERE event_id = ?",
      event.id,
    ),
    { reason_code: "contract_invalid", attempt_count: 2, resolved_at: null },
  );
  assert.equal(database.one("SELECT COUNT(*) AS count FROM orders").count, 0);
  assert.equal(database.one("SELECT COUNT(*) AS count FROM order_events").count, 0);
});

test("configuration-invalid relevant events are quarantined for retry", async (t) => {
  const database = new D1DatabaseFixture();
  t.after(() => database.close());
  const event = stripeEvent(
    "evt_bad_configuration",
    "checkout.session.completed",
    checkout(),
  );
  const result = await postEvent(database, event, {
    STATUSPULSE_PAYMENT_LINK_ID: "",
  });

  assert.equal(result.status, 503);
  assert.equal(await result.text(), "retry later\n");
  assert.deepEqual(
    database.one(
      "SELECT reason_code, attempt_count FROM rejected_events WHERE event_id = ?",
      event.id,
    ),
    { reason_code: "configuration_invalid", attempt_count: 1 },
  );
});

test("a paid non-US order is durably held for manual refund without intake", async (t) => {
  const database = new D1DatabaseFixture();
  t.after(() => database.close());
  const event = stripeEvent(
    "evt_non_us",
    "checkout.session.completed",
    checkout({
      customer_details: {
        email: "canada@example.com",
        address: { country: "CA" },
      },
      collected_information: { business_name: "Canada Fixture Inc." },
    }),
  );
  const result = await postEvent(database, event);

  assert.equal(result.status, 200);
  assert.equal(await result.text(), "processed\n");
  assert.deepEqual(
    database.one(
      `SELECT eligibility_state, payment_state, fulfillment_state
         FROM orders WHERE checkout_session_id = ?`,
      "cs_test_fixture",
    ),
    {
      eligibility_state: "manual_refund_required",
      payment_state: "paid",
      fulfillment_state: "on_hold",
    },
  );
  const persistedState = JSON.stringify(database.all("SELECT * FROM orders"));
  assert.equal(persistedState.includes("canada@example.com"), false);
  assert.equal(persistedState.includes("Canada Fixture Inc."), false);
});

test("dispute updates hold and then safely release an undelivered order", async (t) => {
  const database = new D1DatabaseFixture();
  t.after(() => database.close());
  await expectProcessed(database, stripeEvent(
    "evt_checkout_dispute",
    "checkout.session.completed",
    checkout(),
  ));
  await expectProcessed(database, stripeEvent(
    "evt_dispute_open",
    "charge.dispute.created",
    {
      id: "dp_fixture",
      object: "dispute",
      payment_intent: "pi_test_fixture",
      status: "needs_response",
    },
  ));
  assert.deepEqual(
    database.one("SELECT payment_state, fulfillment_state FROM orders"),
    { payment_state: "under_review", fulfillment_state: "on_hold" },
  );

  await expectProcessed(database, stripeEvent(
    "evt_dispute_won",
    "charge.dispute.closed",
    {
      id: "dp_fixture",
      object: "dispute",
      payment_intent: "pi_test_fixture",
      status: "won",
    },
  ));
  assert.deepEqual(
    database.one("SELECT payment_state, fulfillment_state FROM orders"),
    { payment_state: "paid", fulfillment_state: "queued" },
  );

  await expectProcessed(database, stripeEvent(
    "evt_dispute_older_open_late",
    "charge.dispute.updated",
    {
      id: "dp_fixture",
      object: "dispute",
      payment_intent: "pi_test_fixture",
      status: "under_review",
    },
    { created: 1_000 },
  ));
  assert.deepEqual(
    database.one(
      "SELECT status, disposition FROM risk_events WHERE risk_id = ?",
      "dp_fixture",
    ),
    { status: "won", disposition: "safe" },
  );
  assert.deepEqual(
    database.one("SELECT payment_state, fulfillment_state FROM orders"),
    { payment_state: "paid", fulfillment_state: "queued" },
  );
});

test("a review received before checkout is reconciled and a loss cancels work", async (t) => {
  const database = new D1DatabaseFixture();
  t.after(() => database.close());
  const reviewOpen = await postEvent(database, stripeEvent(
    "evt_review_open",
    "review.opened",
    {
      id: "prv_fixture",
      object: "review",
      payment_intent: "pi_test_fixture",
      closed_reason: null,
    },
  ));
  assert.equal(await reviewOpen.text(), "recorded_unmatched\n");
  await expectProcessed(database, stripeEvent(
    "evt_checkout_after_review",
    "checkout.session.completed",
    checkout(),
  ));
  assert.deepEqual(
    database.one("SELECT payment_state, fulfillment_state FROM orders"),
    { payment_state: "under_review", fulfillment_state: "on_hold" },
  );

  await expectProcessed(database, stripeEvent(
    "evt_review_lost",
    "review.closed",
    {
      id: "prv_fixture",
      object: "review",
      payment_intent: "pi_test_fixture",
      closed_reason: "refunded_as_fraud",
    },
  ));
  assert.deepEqual(
    database.one("SELECT payment_state, fulfillment_state FROM orders"),
    { payment_state: "disputed", fulfillment_state: "canceled" },
  );
});

test("the versioned D1 migration builds the production schema on a fresh database", (t) => {
  const database = new DatabaseSync(":memory:");
  t.after(() => database.close());

  applyMigration(database);

  const tables = database.prepare(
    `SELECT name FROM sqlite_master
       WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
       ORDER BY name`,
  ).all().map(({ name }) => name);
  assert.deepEqual(tables, [
    "order_events",
    "orders",
    "refunds",
    "rejected_events",
    "risk_events",
    "schema_versions",
  ]);
  assert.deepEqual(
    database.prepare(
      "SELECT version FROM schema_versions ORDER BY version",
    ).all().map((row) => ({ ...row })),
    [{ version: 2 }],
  );
});

test("the versioned D1 migration replaces an empty legacy schema without PII", (t) => {
  const database = new DatabaseSync(":memory:");
  t.after(() => database.close());
  database.exec(`
    CREATE TABLE orders (
      checkout_session_id TEXT PRIMARY KEY,
      customer_email TEXT,
      customer_name TEXT,
      shipping_address TEXT
    );
    CREATE TABLE stripe_events (event_id TEXT PRIMARY KEY);
    CREATE TABLE entitlements (customer_email TEXT);
  `);

  applyMigration(database);

  const orderColumns = database.prepare(
    "SELECT name FROM pragma_table_info('orders') ORDER BY cid",
  ).all().map(({ name }) => name);
  assert.equal(orderColumns.includes("checkout_event_created_at"), true);
  assert.equal(orderColumns.includes("eligibility_state"), true);
  assert.equal(orderColumns.includes("customer_email"), false);
  assert.equal(orderColumns.includes("customer_name"), false);
  assert.equal(orderColumns.includes("shipping_address"), false);
  assert.equal(
    database.prepare(
      "SELECT COUNT(*) AS count FROM sqlite_master WHERE type = 'table' AND name = 'entitlements'",
    ).get().count,
    0,
  );
});

test("the versioned D1 migration atomically refuses populated legacy tables", (t) => {
  const database = new DatabaseSync(":memory:");
  t.after(() => database.close());
  database.exec(`
    CREATE TABLE orders (
      checkout_session_id TEXT PRIMARY KEY,
      customer_email TEXT
    );
    INSERT INTO orders(checkout_session_id, customer_email)
    VALUES ('cs_legacy', 'legacy@example.test');
  `);

  assert.throws(
    () => applyMigration(database),
    /CHECK constraint failed/,
  );
  assert.deepEqual(
    database.prepare(
      "SELECT checkout_session_id, customer_email FROM orders",
    ).all().map((row) => ({ ...row })),
    [{
      checkout_session_id: "cs_legacy",
      customer_email: "legacy@example.test",
    }],
  );
  assert.equal(
    database.prepare(
      "SELECT COUNT(*) AS count FROM sqlite_master WHERE type = 'table' AND name = 'refunds'",
    ).get().count,
    0,
  );
});

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
const WORKFLOW = readFileSync(
  new URL("./.github/workflows/deploy-worker.yml", import.meta.url),
  "utf8",
);
const MIGRATION_NAME = "0001_statuspulse_order_state_v2.sql";
const MIGRATION = readFileSync(
  new URL(`./migrations/${MIGRATION_NAME}`, import.meta.url),
  "utf8",
);
const V2_TABLE_COLUMNS = {
  orders: [
    "checkout_session_id", "payment_link_id", "payment_intent_id", "offer_id",
    "offer_version", "amount_subtotal", "amount_tax", "amount_total", "currency",
    "checkout_state", "checkout_event_created_at", "eligibility_state",
    "payment_state", "fulfillment_state", "refunded_amount", "created_at",
    "updated_at", "paid_at", "delivered_at",
  ],
  order_events: [
    "event_id", "event_type", "object_id", "object_state", "stripe_created_at",
    "processed_at",
  ],
  refunds: [
    "refund_id", "payment_intent_id", "amount", "currency", "status",
    "stripe_created_at", "created_at", "updated_at",
  ],
  risk_events: [
    "risk_id", "payment_intent_id", "risk_type", "status", "disposition",
    "stripe_created_at", "updated_at",
  ],
  rejected_events: [
    "event_id", "event_type", "object_id", "reason_code", "first_seen_at",
    "last_seen_at", "attempt_count", "resolved_at",
  ],
  schema_versions: ["version", "applied_at"],
};
const V2_INDEXES = [
  "order_events_object_idx",
  "orders_fulfillment_state_idx",
  "orders_payment_state_idx",
  "refunds_payment_intent_idx",
  "rejected_events_unresolved_idx",
  "risk_events_payment_intent_idx",
];

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

function applicationObjects(database) {
  return database.prepare(
    `SELECT name, type FROM sqlite_master
      WHERE name NOT LIKE 'sqlite_%'
        AND name NOT LIKE '_cf_%'
        AND name <> 'd1_migrations'
      ORDER BY name`,
  ).all().map((row) => ({ ...row }));
}

function schemaDefinitions(database) {
  return database.prepare(
    `SELECT name, type, sql FROM sqlite_master
      WHERE name NOT LIKE 'sqlite_%'
        AND name NOT LIKE '_cf_%'
        AND name <> 'd1_migrations'
      ORDER BY type, name`,
  ).all().map((row) => ({ ...row }));
}

const V2_SCHEMA_DEFINITIONS = (() => {
  const database = new DatabaseSync(":memory:");
  try {
    database.exec(SCHEMA);
    return schemaDefinitions(database);
  } finally {
    database.close();
  }
})();

function assertV2TableContract(database) {
  for (const [table, expectedColumns] of Object.entries(V2_TABLE_COLUMNS)) {
    const columns = database.prepare(
      "SELECT name FROM pragma_table_info(?) ORDER BY cid",
    ).all(table).map(({ name }) => name);
    assert.deepEqual(columns, expectedColumns, `${table} columns drifted`);
  }
  assert.deepEqual(
    database.prepare(
      "SELECT version FROM schema_versions ORDER BY version",
    ).all().map((row) => ({ ...row })),
    [{ version: 2 }],
  );
}

function assertV2SchemaContract(database) {
  const expectedObjects = [
    ...Object.keys(V2_TABLE_COLUMNS).map((name) => ({ name, type: "table" })),
    ...V2_INDEXES.map((name) => ({ name, type: "index" })),
  ].sort((left, right) => left.name.localeCompare(right.name));
  assert.deepEqual(applicationObjects(database), expectedObjects);
  assert.deepEqual(schemaDefinitions(database), V2_SCHEMA_DEFINITIONS);
  assertV2TableContract(database);
}

function v2MigrationState(database) {
  const historyObject = database.prepare(
    "SELECT type FROM sqlite_master WHERE name = 'd1_migrations'",
  ).get();
  let history = [];
  if (historyObject) {
    if (historyObject.type !== "table") {
      throw new Error("d1_migrations is not a table");
    }
    history = database.prepare(
      "SELECT name FROM d1_migrations ORDER BY name",
    ).all().map(({ name }) => name);
  }
  if (history.length === 0) {
    const objects = applicationObjects(database);
    if (objects.length !== 0) {
      throw new Error("statuspulse-v2 has unexpected unmigrated objects");
    }
    return "pending";
  }
  if (history.length === 1 && history[0] === MIGRATION_NAME) {
    assertV2SchemaContract(database);
    return "applied";
  }
  throw new Error("statuspulse-v2 has unexpected migration history");
}

function applyTrackedV2Migration(database) {
  const state = v2MigrationState(database);
  if (state === "applied") return "repeat";
  database.exec(`
    CREATE TABLE IF NOT EXISTS d1_migrations (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT UNIQUE NOT NULL,
      applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    BEGIN IMMEDIATE;
  `);
  try {
    database.exec(MIGRATION);
    database.prepare(
      "INSERT INTO d1_migrations(name) VALUES (?)",
    ).run(MIGRATION_NAME);
    database.exec("COMMIT");
  } catch (error) {
    database.exec("ROLLBACK");
    throw error;
  }
  assertV2SchemaContract(database);
  return "applied";
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
  assertV2SchemaContract(database);
});

test("the deployment workflow binds and migrates only statuspulse-v2", () => {
  const d1Targets = [...WORKFLOW.matchAll(
    /wrangler@4\.125\.0 d1 (?:execute|migrations apply) ([^\s]+)/g,
  )].map((match) => match[1]);
  assert.match(WORKFLOW, /database_name='statuspulse-v2'/);
  assert.match(WORKFLOW, /"database_name": "statuspulse-v2"/);
  assert.match(WORKFLOW, /d1 migrations apply statuspulse-v2 --remote/);
  assert.match(WORKFLOW, /d1 execute statuspulse-v2 --remote/);
  assert.ok(d1Targets.length >= 3);
  assert.deepEqual([...new Set(d1Targets)], ["statuspulse-v2"]);
  assert.doesNotMatch(WORKFLOW, /d1 (?:execute|migrations apply) statuspulse --remote/);
  assert.doesNotMatch(WORKFLOW, /legacy_stripe_events|ALTER TABLE|DROP TABLE/);
  assert.match(WORKFLOW, /SELECT name, type, sql FROM sqlite_master/);
  assert.match(WORKFLOW, /actual_schema.*!=.*expected_schema/);
});

test("the tracked v2 migration is repeat-safe and preserves live rows", (t) => {
  const database = new DatabaseSync(":memory:");
  t.after(() => database.close());

  assert.equal(v2MigrationState(database), "pending");
  assert.equal(applyTrackedV2Migration(database), "applied");
  database.prepare(
    `INSERT INTO rejected_events(
       event_id, event_type, object_id, reason_code,
       first_seen_at, last_seen_at, attempt_count
     ) VALUES (?, ?, ?, ?, ?, ?, ?)`,
  ).run("evt_repeat", "test.event", "obj_repeat", "fixture", 1, 1, 1);

  assert.equal(v2MigrationState(database), "applied");
  assert.equal(applyTrackedV2Migration(database), "repeat");
  assert.deepEqual(
    database.prepare(
      "SELECT event_id, reason_code FROM rejected_events",
    ).all().map((row) => ({ ...row })),
    [{ event_id: "evt_repeat", reason_code: "fixture" }],
  );
});

test("an unexpected populated or empty unmigrated v2 schema fails closed", (t) => {
  const populated = new DatabaseSync(":memory:");
  const empty = new DatabaseSync(":memory:");
  t.after(() => populated.close());
  t.after(() => empty.close());
  populated.exec(`
    CREATE TABLE entitlements (checkout_session_id TEXT PRIMARY KEY);
    INSERT INTO entitlements(checkout_session_id) VALUES ('cs_unexpected');
  `);
  empty.exec("CREATE TABLE orders (checkout_session_id TEXT PRIMARY KEY)");

  assert.throws(
    () => applyTrackedV2Migration(populated),
    /unexpected unmigrated objects/,
  );
  assert.throws(
    () => applyTrackedV2Migration(empty),
    /unexpected unmigrated objects/,
  );
  assert.deepEqual(
    populated.prepare("SELECT checkout_session_id FROM entitlements")
      .all().map((row) => ({ ...row })),
    [{ checkout_session_id: "cs_unexpected" }],
  );
});

test("an empty Wrangler migration table can safely resume the first v2 migration", (t) => {
  const database = new DatabaseSync(":memory:");
  t.after(() => database.close());
  database.exec(`
    CREATE TABLE d1_migrations (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT UNIQUE NOT NULL,
      applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
  `);

  assert.equal(v2MigrationState(database), "pending");
  assert.equal(applyTrackedV2Migration(database), "applied");
  assert.equal(v2MigrationState(database), "applied");
});

test("unexpected migration history and integrity-weakened schema fail closed", (t) => {
  const wrongHistory = new DatabaseSync(":memory:");
  const drifted = new DatabaseSync(":memory:");
  const nonTableHistory = new DatabaseSync(":memory:");
  t.after(() => wrongHistory.close());
  t.after(() => drifted.close());
  t.after(() => nonTableHistory.close());

  wrongHistory.exec(`
    CREATE TABLE d1_migrations (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT UNIQUE NOT NULL,
      applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    INSERT INTO d1_migrations(name) VALUES ('0000_unexpected.sql');
  `);
  assert.throws(
    () => v2MigrationState(wrongHistory),
    /unexpected migration history/,
  );

  applyTrackedV2Migration(drifted);
  drifted.exec(`
    DROP INDEX refunds_payment_intent_idx;
    DROP TABLE refunds;
    CREATE TABLE refunds (
      refund_id TEXT PRIMARY KEY,
      payment_intent_id TEXT,
      amount TEXT,
      currency TEXT,
      status TEXT,
      stripe_created_at TEXT,
      created_at TEXT,
      updated_at TEXT
    );
    CREATE INDEX refunds_payment_intent_idx ON refunds(refund_id);
  `);
  assert.throws(() => v2MigrationState(drifted), assert.AssertionError);

  nonTableHistory.exec(`
    CREATE VIEW d1_migrations AS
    SELECT 1 AS id, '${MIGRATION_NAME}' AS name, 'now' AS applied_at;
  `);
  assert.throws(
    () => v2MigrationState(nonTableHistory),
    /d1_migrations is not a table/,
  );
});

test("the v2 migration snapshot matches schema.sql exactly", (t) => {
  const snapshot = new DatabaseSync(":memory:");
  const migrated = new DatabaseSync(":memory:");
  t.after(() => snapshot.close());
  t.after(() => migrated.close());

  snapshot.exec(SCHEMA);
  applyMigration(migrated);
  assertV2SchemaContract(snapshot);
  assertV2SchemaContract(migrated);
  assert.deepEqual(
    applicationObjects(migrated),
    applicationObjects(snapshot),
  );
  assert.deepEqual(
    schemaDefinitions(migrated),
    schemaDefinitions(snapshot),
  );
});

test("the forward-only migration cannot mutate legacy business rows", (t) => {
  const legacy = new DatabaseSync(":memory:");
  t.after(() => legacy.close());
  legacy.exec(`
    CREATE TABLE entitlements (
      payment_id TEXT PRIMARY KEY,
      access_token_hash TEXT NOT NULL
    );
    CREATE TABLE legacy_stripe_events_v1 (
      event_id TEXT PRIMARY KEY,
      processed_at INTEGER NOT NULL
    );
    INSERT INTO entitlements(payment_id, access_token_hash)
    VALUES ('cs_legacy_paid', 'preserved_hash');
    INSERT INTO legacy_stripe_events_v1(event_id, processed_at) VALUES
      ('evt_legacy_one', 1700000005),
      ('evt_legacy_two', 1700000006);
  `);

  assert.doesNotMatch(MIGRATION, /\b(?:ALTER|DROP|DELETE|UPDATE|REPLACE)\b/i);
  applyMigration(legacy);
  assertV2TableContract(legacy);
  assert.deepEqual(
    legacy.prepare(
      "SELECT payment_id, access_token_hash FROM entitlements",
    ).all().map((row) => ({ ...row })),
    [{ payment_id: "cs_legacy_paid", access_token_hash: "preserved_hash" }],
  );
  assert.deepEqual(
    legacy.prepare(
      "SELECT event_id, processed_at FROM legacy_stripe_events_v1 ORDER BY event_id",
    ).all().map((row) => ({ ...row })),
    [
      { event_id: "evt_legacy_one", processed_at: 1700000005 },
      { event_id: "evt_legacy_two", processed_at: 1700000006 },
    ],
  );
});

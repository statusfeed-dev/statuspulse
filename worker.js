const SIGNATURE_TOLERANCE_SECONDS = 5 * 60;
const MAX_WEBHOOK_BYTES = 1024 * 1024;
const EXPECTED_OFFER_ID = "vendor_reliability_pilot";
const EXPECTED_OFFER_VERSION = "1";
const EXPECTED_CURRENCY = "usd";
const EXPECTED_SUBTOTAL = 7900;
const MAX_DEPENDENCIES = 20;
const HEALTH_QUERY = `SELECT CASE WHEN
  (SELECT COUNT(*) FROM sqlite_master
    WHERE type = 'table'
      AND name IN (
        'orders', 'order_events', 'refunds', 'risk_events',
        'rejected_events', 'schema_versions'
      )) = 6
  AND (SELECT COUNT(*) FROM pragma_table_info('orders')
        WHERE name IN (
          'checkout_state', 'checkout_event_created_at',
          'eligibility_state', 'fulfillment_state'
        )) = 4
  AND (SELECT COUNT(*) FROM pragma_table_info('refunds')
        WHERE name = 'stripe_created_at') = 1
  AND (SELECT COUNT(*) FROM pragma_table_info('risk_events')
        WHERE name = 'stripe_created_at') = 1
  AND COALESCE((SELECT MAX(version) FROM schema_versions), 0) >= 2
THEN 1 ELSE 0 END AS ok`;

const CHECKOUT_EVENTS = new Set([
  "checkout.session.completed",
  "checkout.session.async_payment_succeeded",
  "checkout.session.async_payment_failed",
  "checkout.session.expired",
]);
const REFUND_EVENTS = new Set([
  "refund.created",
  "refund.updated",
  "refund.failed",
]);
const DISPUTE_EVENTS = new Set([
  "charge.dispute.created",
  "charge.dispute.updated",
  "charge.dispute.closed",
]);
const REVIEW_EVENTS = new Set(["review.opened", "review.closed"]);
const RELEVANT_EVENTS = new Set([
  ...CHECKOUT_EVENTS,
  ...REFUND_EVENTS,
  ...DISPUTE_EVENTS,
  ...REVIEW_EVENTS,
]);
const REFUND_STATUSES = new Set([
  "pending",
  "requires_action",
  "succeeded",
  "failed",
  "canceled",
]);
const CHECKOUT_STATES = new Set(["pending", "paid", "failed", "expired"]);
const CHECKOUT_PAYMENT_STATUSES = new Set(["paid", "unpaid"]);
const NONPAID_CHECKOUT_EVENTS = new Set([
  "checkout.session.async_payment_failed",
  "checkout.session.expired",
]);
const FULFILLMENT_STATES = new Set([
  "pending",
  "queued",
  "in_progress",
  "delivered",
  "on_hold",
  "canceled",
]);
const RISK_DISPOSITIONS = new Set(["active", "safe", "lost"]);
const RECONCILIATION_IDENTIFIERS = new Set([
  "checkout_session_id",
  "payment_intent_id",
]);
const ELIGIBILITY_STATES = new Set([
  "pending",
  "eligible",
  "manual_refund_required",
]);
const SAFE_DISPUTE_STATUSES = new Set(["won", "prevented", "warning_closed"]);
const SAFE_REVIEW_REASONS = new Set(["approved", "redacted", "acknowledged"]);
const LOST_REVIEW_REASONS = new Set([
  "refunded",
  "refunded_as_fraud",
  "disputed",
  "canceled",
  "payment_never_settled",
]);

export class ValidationError extends Error {
  constructor(code, message) {
    super(message);
    this.name = "ValidationError";
    this.code = code;
  }
}

function response(body, status = 200, headers = {}) {
  return new Response(body, {
    status,
    headers: {
      "Cache-Control": "no-store",
      "Content-Type": "text/plain; charset=utf-8",
      "Referrer-Policy": "no-referrer",
      "X-Content-Type-Options": "nosniff",
      ...headers,
    },
  });
}

function hex(bytes) {
  return [...new Uint8Array(bytes)]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

function parseSignature(header) {
  const result = {};
  for (const item of (header || "").split(",")) {
    const separator = item.indexOf("=");
    if (separator <= 0) continue;
    const key = item.slice(0, separator);
    const value = item.slice(separator + 1);
    if (value) (result[key] ||= []).push(value);
  }
  return result;
}

function timingSafeEqual(left, right) {
  if (left.length !== right.length) return false;
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) {
    difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  }
  return difference === 0;
}

export async function verifyStripeSignature(payload, header, secret, now = Date.now()) {
  if (typeof secret !== "string" || !secret) {
    throw new ValidationError("webhook_secret_missing", "missing webhook secret");
  }
  const parts = parseSignature(header);
  const timestamp = Number(parts.t?.[0]);
  if (
    !Number.isInteger(timestamp)
    || !parts.v1?.length
    || Math.abs(now / 1000 - timestamp) > SIGNATURE_TOLERANCE_SECONDS
  ) {
    throw new ValidationError("signature_invalid", "invalid signature timestamp");
  }
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const digest = await crypto.subtle.sign(
    "HMAC",
    key,
    new TextEncoder().encode(`${timestamp}.${payload}`),
  );
  const expected = hex(digest);
  if (!parts.v1.some((candidate) => timingSafeEqual(expected, candidate))) {
    throw new ValidationError("signature_invalid", "invalid signature");
  }
  try {
    return JSON.parse(payload);
  } catch {
    throw new ValidationError("payload_invalid", "invalid webhook JSON");
  }
}

function requiredString(value, field, maximumLength, code = "contract_invalid") {
  if (typeof value !== "string") {
    throw new ValidationError(code, `${field} must be a string`);
  }
  const cleaned = value.trim();
  if (!cleaned || cleaned.length > maximumLength) {
    throw new ValidationError(code, `${field} is missing or too long`);
  }
  return cleaned;
}

function optionalString(value, maximumLength) {
  if (value === null || value === undefined || value === "") return null;
  if (typeof value !== "string") return null;
  const cleaned = value.trim();
  return cleaned ? cleaned.slice(0, maximumLength) : null;
}

function stripeId(value, field, code = "contract_invalid") {
  const candidate = typeof value === "object" && value !== null ? value.id : value;
  return requiredString(candidate, field, 255, code);
}

function configuredPaymentLink(value) {
  const link = requiredString(
    value,
    "configured Payment Link",
    255,
    "configuration_invalid",
  );
  if (!/^plink_[A-Za-z0-9]+$/.test(link)) {
    throw new ValidationError(
      "configuration_invalid",
      "configured Payment Link is invalid",
    );
  }
  return link;
}

function customFieldValue(fields, key) {
  if (!Array.isArray(fields)) return null;
  const field = fields.find((candidate) => candidate?.key === key);
  if (!field || typeof field !== "object") return null;
  if (field.type === "text") return field.text?.value;
  if (field.type === "dropdown") return field.dropdown?.value;
  return null;
}

export function validatePilotSession(session, expectedPaymentLink) {
  const configuredLink = configuredPaymentLink(expectedPaymentLink);
  const checkoutSessionId = stripeId(session?.id, "Checkout Session ID");
  if (session?.object !== "checkout.session") {
    throw new ValidationError("contract_invalid", "unexpected Stripe object");
  }
  if (session.payment_link !== configuredLink) {
    throw new ValidationError("payment_link_mismatch", "unexpected Payment Link");
  }
  if (session.mode !== "payment") {
    throw new ValidationError("contract_invalid", "unexpected Checkout mode");
  }
  if (
    session.metadata?.offer_id !== EXPECTED_OFFER_ID
    || session.metadata?.offer_version !== EXPECTED_OFFER_VERSION
  ) {
    throw new ValidationError("contract_invalid", "unexpected offer metadata");
  }
  if (session.currency !== EXPECTED_CURRENCY) {
    throw new ValidationError("contract_invalid", "unexpected currency");
  }
  if (!Number.isInteger(session.amount_subtotal) || session.amount_subtotal !== EXPECTED_SUBTOTAL) {
    throw new ValidationError("contract_invalid", "unexpected subtotal");
  }
  const tax = session.total_details?.amount_tax ?? 0;
  const discount = session.total_details?.amount_discount ?? 0;
  if (!Number.isInteger(tax) || tax < 0 || discount !== 0) {
    throw new ValidationError("contract_invalid", "unexpected tax or discount amount");
  }
  if (!Number.isInteger(session.amount_total) || session.amount_total !== EXPECTED_SUBTOTAL + tax) {
    throw new ValidationError("contract_invalid", "unexpected total");
  }
  if (session.automatic_tax?.enabled !== true) {
    throw new ValidationError("contract_invalid", "automatic tax is not enabled");
  }
  if (session.tax_id_collection?.enabled !== true) {
    throw new ValidationError("contract_invalid", "tax ID collection is not enabled");
  }
  if (session.billing_address_collection !== "required") {
    throw new ValidationError("contract_invalid", "billing address collection is not required");
  }
  if (!CHECKOUT_PAYMENT_STATUSES.has(session.payment_status)) {
    throw new ValidationError("contract_invalid", "unexpected payment status");
  }

  const paymentIntentId = optionalString(
    typeof session.payment_intent === "object"
      ? session.payment_intent?.id
      : session.payment_intent,
    255,
  );
  if (session.payment_status === "paid" && !paymentIntentId) {
    throw new ValidationError(
      "contract_invalid",
      "paid order is missing a PaymentIntent",
    );
  }

  // Intake is validated in memory for offer scope, but never returned or persisted in D1.
  let eligibilityState = "pending";
  if (session.payment_status === "paid") {
    if (session.automatic_tax?.status !== "complete") {
      throw new ValidationError(
        "contract_invalid",
        "automatic tax calculation is not complete",
      );
    }
    if (session.consent?.terms_of_service !== "accepted") {
      throw new ValidationError("contract_invalid", "terms consent was not accepted");
    }
    const dependencies = requiredString(
      customFieldValue(session.custom_fields, "dependencies"),
      "dependencies",
      255,
    );
    const dependencyCount = dependencies
      .split(/[\n,]/)
      .map((value) => value.trim())
      .filter(Boolean).length;
    if (dependencyCount < 1 || dependencyCount > MAX_DEPENDENCIES) {
      throw new ValidationError(
        "contract_invalid",
        "dependency count is outside the pilot scope",
      );
    }
    requiredString(session.customer_details?.email, "checkout email", 320);
    eligibilityState = session.customer_details?.address?.country === "US"
      ? "eligible"
      : "manual_refund_required";
    requiredString(
      session.collected_information?.business_name,
      "business name",
      150,
    );
  }

  return {
    checkoutSessionId,
    paymentLinkId: configuredLink,
    paymentIntentId,
    amountSubtotal: session.amount_subtotal,
    amountTax: tax,
    amountTotal: session.amount_total,
    currency: session.currency,
    paymentStatus: session.payment_status,
    eligibilityState,
  };
}

function incomingCheckoutState(eventType, paymentStatus) {
  if (
    eventType === "checkout.session.async_payment_succeeded"
    || (eventType === "checkout.session.completed" && paymentStatus === "paid")
  ) {
    return "paid";
  }
  if (eventType === "checkout.session.async_payment_failed") return "failed";
  if (eventType === "checkout.session.expired") return "expired";
  return "pending";
}

function validateCheckoutEventState(eventType, paymentStatus) {
  if (
    eventType === "checkout.session.async_payment_succeeded"
    && paymentStatus !== "paid"
  ) {
    throw new ValidationError(
      "contract_invalid",
      "async success is not marked paid",
    );
  }
  if (NONPAID_CHECKOUT_EVENTS.has(eventType) && paymentStatus === "paid") {
    throw new ValidationError(
      "contract_invalid",
      "failed checkout is marked paid",
    );
  }
}

export function mergeCheckoutState(previous, incoming) {
  if (!CHECKOUT_STATES.has(previous) || !CHECKOUT_STATES.has(incoming)) {
    throw new ValidationError("state_invalid", "invalid checkout state");
  }
  if (previous === "paid" || incoming === "paid") return "paid";
  if (previous !== "pending" && incoming === "pending") return previous;
  return incoming;
}

function normalizeRefunds(refunds) {
  if (!Array.isArray(refunds)) {
    throw new ValidationError("state_invalid", "refund state must be an array");
  }
  return refunds.map((refund) => {
    if (
      !Number.isInteger(refund?.amount)
      || refund.amount <= 0
      || !REFUND_STATUSES.has(refund.status)
    ) {
      throw new ValidationError("state_invalid", "invalid stored refund state");
    }
    return { amount: refund.amount, status: refund.status };
  });
}

function riskDisposition(risks) {
  if (!Array.isArray(risks)) {
    throw new ValidationError("state_invalid", "risk state must be an array");
  }
  const dispositions = new Set(risks.map((risk) => risk?.disposition));
  if ([...dispositions].some((value) => !RISK_DISPOSITIONS.has(value))) {
    throw new ValidationError("state_invalid", "invalid stored risk state");
  }
  if (dispositions.has("lost")) return "lost";
  if (dispositions.has("active")) return "active";
  return "safe";
}

function normalFulfillmentState(checkoutState, previousFulfillment) {
  if (checkoutState === "paid") {
    if (previousFulfillment === "delivered") return "delivered";
    if (previousFulfillment === "in_progress") return "in_progress";
    return "queued";
  }
  if (checkoutState === "pending") return "pending";
  return "canceled";
}

export function deriveOrderState(
  checkoutState,
  amountTotal,
  previousFulfillment,
  refunds,
  risks = [],
  eligibilityState = "eligible",
) {
  if (!CHECKOUT_STATES.has(checkoutState)) {
    throw new ValidationError("state_invalid", "invalid checkout state");
  }
  if (!Number.isInteger(amountTotal) || amountTotal <= 0) {
    throw new ValidationError("state_invalid", "invalid order total");
  }
  if (!FULFILLMENT_STATES.has(previousFulfillment)) {
    throw new ValidationError("state_invalid", "invalid fulfillment state");
  }
  if (!ELIGIBILITY_STATES.has(eligibilityState)) {
    throw new ValidationError("state_invalid", "invalid eligibility state");
  }
  const normalizedRefunds = normalizeRefunds(refunds);
  const succeededAmount = normalizedRefunds
    .filter((refund) => refund.status === "succeeded")
    .reduce((total, refund) => total + refund.amount, 0);
  const hasPendingRefund = normalizedRefunds.some(
    (refund) => refund.status === "pending" || refund.status === "requires_action",
  );
  const risk = riskDisposition(risks);
  const immutableDelivery = previousFulfillment === "delivered";

  let paymentState = checkoutState;
  if (risk === "lost") paymentState = "disputed";
  else if (risk === "active") paymentState = "under_review";
  else if (hasPendingRefund) paymentState = "refund_pending";
  else if (succeededAmount >= amountTotal) paymentState = "refunded";
  else if (succeededAmount > 0) paymentState = "partially_refunded";

  let fulfillmentState = normalFulfillmentState(checkoutState, previousFulfillment);
  if (!immutableDelivery) {
    if (risk === "lost" || succeededAmount >= amountTotal) {
      fulfillmentState = "canceled";
    } else if (
      risk === "active"
      || hasPendingRefund
      || eligibilityState === "manual_refund_required"
    ) {
      fulfillmentState = "on_hold";
    }
  }

  return {
    paymentState,
    fulfillmentState,
    refundedAmount: succeededAmount,
  };
}

function eventObjectState(eventType, object, normalizedState = null) {
  if (normalizedState) return normalizedState;
  if (CHECKOUT_EVENTS.has(eventType)) {
    return `${object.payment_status || "unknown"}:${object.status || "unknown"}`;
  }
  return `${object.status || "unknown"}:${object.amount || "unknown"}`;
}

async function eventWasProcessed(database, eventId) {
  return Boolean(await database.prepare(
    "SELECT 1 AS processed FROM order_events WHERE event_id = ? LIMIT 1",
  ).bind(eventId).first());
}

function eventStatement(database, event, objectState, now) {
  return database.prepare(
    `INSERT INTO order_events(
       event_id, event_type, object_id, object_state,
       stripe_created_at, processed_at
     ) VALUES (?, ?, ?, ?, ?, ?)`,
  ).bind(
    event.id,
    event.type,
    event.data.object.id,
    objectState,
    event.created,
    now,
  );
}

function resolveRejectedStatement(database, eventId, now) {
  return database.prepare(
    "UPDATE rejected_events SET resolved_at = ? WHERE event_id = ?",
  ).bind(now, eventId);
}

async function recordRejectedEvent(database, event, reasonCode, now, fallbackEventId) {
  const eventId = optionalString(event?.id, 255) || fallbackEventId;
  const eventType = optionalString(event?.type, 100) || "unknown";
  const objectId = optionalString(event?.data?.object?.id, 255) || "unknown";
  await database.prepare(
    `INSERT INTO rejected_events(
       event_id, event_type, object_id, reason_code,
       first_seen_at, last_seen_at, attempt_count, resolved_at
     ) VALUES (?, ?, ?, ?, ?, ?, 1, NULL)
     ON CONFLICT(event_id) DO UPDATE SET
       reason_code = excluded.reason_code,
       last_seen_at = excluded.last_seen_at,
       attempt_count = rejected_events.attempt_count + 1,
       resolved_at = NULL`,
  ).bind(
    eventId,
    eventType,
    objectId,
    reasonCode,
    now,
    now,
  ).run();
}

async function payloadFingerprint(payload) {
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(payload),
  );
  return `malformed_${hex(digest)}`;
}

async function validateExistingOrder(database, order) {
  const existing = await database.prepare(
    `SELECT checkout_session_id, payment_link_id, payment_intent_id,
            offer_id, offer_version, amount_subtotal, amount_total, currency
       FROM orders
      WHERE checkout_session_id = ?`,
  ).bind(order.checkoutSessionId).first();
  if (existing) {
    const conflicts = (
      existing.payment_link_id !== order.paymentLinkId
      || existing.offer_id !== EXPECTED_OFFER_ID
      || existing.offer_version !== EXPECTED_OFFER_VERSION
      || existing.amount_subtotal !== order.amountSubtotal
      || existing.amount_total !== order.amountTotal
      || existing.currency !== order.currency
      || (
        existing.payment_intent_id
        && order.paymentIntentId
        && existing.payment_intent_id !== order.paymentIntentId
      )
    );
    if (conflicts) {
      throw new ValidationError("checkout_conflict", "stored checkout contract conflict");
    }
  }
  if (order.paymentIntentId) {
    const reusedIntent = await database.prepare(
      `SELECT checkout_session_id
         FROM orders
        WHERE payment_intent_id = ? AND checkout_session_id <> ?`,
    ).bind(order.paymentIntentId, order.checkoutSessionId).first();
    if (reusedIntent) {
      throw new ValidationError("checkout_conflict", "PaymentIntent is already assigned");
    }
  }
}

function reconciliationStatement(database, identifierColumn, identifierValue, now) {
  if (!RECONCILIATION_IDENTIFIERS.has(identifierColumn)) {
    throw new ValidationError("state_invalid", "invalid reconciliation identifier");
  }
  return database.prepare(
    `WITH aggregate_state AS (
       SELECT candidate.checkout_session_id,
              COALESCE((
                SELECT SUM(refunds.amount)
                  FROM refunds
                 WHERE refunds.payment_intent_id = candidate.payment_intent_id
                   AND refunds.status = 'succeeded'
              ), 0) AS succeeded_refunds,
              EXISTS(
                SELECT 1
                  FROM refunds
                 WHERE refunds.payment_intent_id = candidate.payment_intent_id
                   AND refunds.status IN ('pending', 'requires_action')
              ) AS has_pending_refund,
              EXISTS(
                SELECT 1
                  FROM risk_events
                 WHERE risk_events.payment_intent_id = candidate.payment_intent_id
                   AND risk_events.disposition = 'lost'
              ) AS has_lost_risk,
              EXISTS(
                SELECT 1
                  FROM risk_events
                 WHERE risk_events.payment_intent_id = candidate.payment_intent_id
                   AND risk_events.disposition = 'active'
              ) AS has_active_risk
         FROM orders AS candidate
        WHERE candidate.${identifierColumn} = ?
     )
     UPDATE orders
        SET refunded_amount = COALESCE((
              SELECT succeeded_refunds FROM aggregate_state
            ), 0),
            payment_state = CASE
              WHEN (SELECT has_lost_risk FROM aggregate_state) = 1 THEN 'disputed'
              WHEN (SELECT has_active_risk FROM aggregate_state) = 1 THEN 'under_review'
              WHEN (SELECT has_pending_refund FROM aggregate_state) = 1
                THEN 'refund_pending'
              WHEN (SELECT succeeded_refunds FROM aggregate_state) >= orders.amount_total
                THEN 'refunded'
              WHEN (SELECT succeeded_refunds FROM aggregate_state) > 0
                THEN 'partially_refunded'
              ELSE orders.checkout_state
            END,
            fulfillment_state = CASE
              WHEN orders.fulfillment_state = 'delivered' THEN 'delivered'
              WHEN (SELECT has_lost_risk FROM aggregate_state) = 1
                OR (SELECT succeeded_refunds FROM aggregate_state) >= orders.amount_total
                THEN 'canceled'
              WHEN (SELECT has_active_risk FROM aggregate_state) = 1
                OR (SELECT has_pending_refund FROM aggregate_state) = 1
                OR orders.eligibility_state = 'manual_refund_required'
                THEN 'on_hold'
              WHEN orders.checkout_state = 'paid'
                AND orders.fulfillment_state = 'in_progress' THEN 'in_progress'
              WHEN orders.checkout_state = 'paid' THEN 'queued'
              WHEN orders.checkout_state = 'pending' THEN 'pending'
              ELSE 'canceled'
            END,
            updated_at = ?
      WHERE orders.checkout_session_id = (
        SELECT checkout_session_id FROM aggregate_state
      )`,
  ).bind(identifierValue, now);
}

function initialOrderState(checkoutState, eligibilityState) {
  if (checkoutState === "paid") {
    return {
      payment: "paid",
      fulfillment: eligibilityState === "manual_refund_required"
        ? "on_hold"
        : "queued",
    };
  }
  if (checkoutState === "pending") return { payment: "pending", fulfillment: "pending" };
  return { payment: checkoutState, fulfillment: "canceled" };
}

async function recordCheckout(database, event, order, now) {
  validateCheckoutEventState(event.type, order.paymentStatus);
  if (await eventWasProcessed(database, event.id)) {
    await reconciliationStatement(
      database,
      "checkout_session_id",
      order.checkoutSessionId,
      now,
    ).run();
    return "duplicate";
  }
  await validateExistingOrder(database, order);
  const incomingState = incomingCheckoutState(event.type, order.paymentStatus);
  const initialState = initialOrderState(incomingState, order.eligibilityState);
  const objectState = eventObjectState(event.type, event.data.object);
  const orderStatement = database.prepare(
    `INSERT INTO orders(
       checkout_session_id, payment_link_id, payment_intent_id,
       offer_id, offer_version, amount_subtotal, amount_tax, amount_total,
       currency, checkout_state, checkout_event_created_at,
       eligibility_state, payment_state, fulfillment_state,
       refunded_amount, created_at, updated_at, paid_at
     ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
     ON CONFLICT(checkout_session_id) DO UPDATE SET
       payment_intent_id = COALESCE(orders.payment_intent_id, excluded.payment_intent_id),
       amount_tax = excluded.amount_tax,
       amount_total = excluded.amount_total,
       checkout_state = CASE
         WHEN orders.checkout_state = 'paid' OR excluded.checkout_state = 'paid' THEN 'paid'
         WHEN excluded.checkout_event_created_at < orders.checkout_event_created_at
           THEN orders.checkout_state
         WHEN excluded.checkout_event_created_at = orders.checkout_event_created_at
           AND orders.checkout_state <> 'pending'
           AND excluded.checkout_state = 'pending' THEN orders.checkout_state
         ELSE excluded.checkout_state
       END,
       checkout_event_created_at = MAX(
         orders.checkout_event_created_at,
         excluded.checkout_event_created_at
       ),
       eligibility_state = CASE
         WHEN orders.eligibility_state = 'manual_refund_required'
           OR excluded.eligibility_state = 'manual_refund_required'
           THEN 'manual_refund_required'
         WHEN orders.eligibility_state = 'eligible'
           OR excluded.eligibility_state = 'eligible' THEN 'eligible'
         ELSE 'pending'
       END,
       updated_at = excluded.updated_at,
       paid_at = CASE
         WHEN excluded.checkout_state = 'paid' THEN COALESCE(orders.paid_at, excluded.paid_at)
         ELSE orders.paid_at
       END`,
  ).bind(
    order.checkoutSessionId,
    order.paymentLinkId,
    order.paymentIntentId,
    EXPECTED_OFFER_ID,
    EXPECTED_OFFER_VERSION,
    order.amountSubtotal,
    order.amountTax,
    order.amountTotal,
    order.currency,
    incomingState,
    event.created,
    order.eligibilityState,
    initialState.payment,
    initialState.fulfillment,
    now,
    now,
    incomingState === "paid" ? now : null,
  );
  await database.batch([
    orderStatement,
    reconciliationStatement(
      database,
      "checkout_session_id",
      order.checkoutSessionId,
      now,
    ),
    eventStatement(database, event, objectState, now),
    resolveRejectedStatement(database, event.id, now),
  ]);
  return "processed";
}

function validateRefund(refund) {
  if (refund?.object !== "refund") {
    throw new ValidationError("refund_invalid", "unexpected refund object");
  }
  const refundId = stripeId(refund.id, "refund ID", "refund_invalid");
  const paymentIntentId = stripeId(
    refund.payment_intent,
    "refund PaymentIntent",
    "refund_invalid",
  );
  if (refund.currency !== EXPECTED_CURRENCY) {
    throw new ValidationError("refund_invalid", "unexpected refund currency");
  }
  if (!Number.isInteger(refund.amount) || refund.amount <= 0) {
    throw new ValidationError("refund_invalid", "invalid refund amount");
  }
  const status = requiredString(refund.status, "refund status", 40, "refund_invalid");
  if (!REFUND_STATUSES.has(status)) {
    throw new ValidationError("refund_invalid", "unexpected refund status");
  }
  return { refundId, paymentIntentId, amount: refund.amount, status };
}

async function recordRefund(database, event, refund, now) {
  if (await eventWasProcessed(database, event.id)) {
    await reconciliationStatement(
      database,
      "payment_intent_id",
      refund.paymentIntentId,
      now,
    ).run();
    return "duplicate";
  }
  const existing = await database.prepare(
    `SELECT payment_intent_id, amount, currency
       FROM refunds
      WHERE refund_id = ?`,
  ).bind(refund.refundId).first();
  if (
    existing
    && (
      existing.payment_intent_id !== refund.paymentIntentId
      || existing.amount !== refund.amount
      || existing.currency !== EXPECTED_CURRENCY
    )
  ) {
    throw new ValidationError("refund_conflict", "stored refund contract conflict");
  }
  const objectState = eventObjectState(event.type, event.data.object);
  const refundStatement = database.prepare(
    `INSERT INTO refunds(
       refund_id, payment_intent_id, amount, currency, status,
       stripe_created_at, created_at, updated_at
     ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
     ON CONFLICT(refund_id) DO UPDATE SET
       payment_intent_id = excluded.payment_intent_id,
       amount = excluded.amount,
       currency = excluded.currency,
       status = excluded.status,
       stripe_created_at = excluded.stripe_created_at,
       updated_at = excluded.updated_at
     WHERE excluded.stripe_created_at > refunds.stripe_created_at
        OR (
          excluded.stripe_created_at = refunds.stripe_created_at
          AND CASE excluded.status
            WHEN 'requires_action' THEN 4
            WHEN 'pending' THEN 4
            WHEN 'succeeded' THEN 3
            ELSE 2
          END > CASE refunds.status
            WHEN 'requires_action' THEN 4
            WHEN 'pending' THEN 4
            WHEN 'succeeded' THEN 3
            ELSE 2
          END
        )`,
  ).bind(
    refund.refundId,
    refund.paymentIntentId,
    refund.amount,
    EXPECTED_CURRENCY,
    refund.status,
    event.created,
    now,
    now,
  );
  await database.batch([
    refundStatement,
    reconciliationStatement(
      database,
      "payment_intent_id",
      refund.paymentIntentId,
      now,
    ),
    eventStatement(database, event, objectState, now),
    resolveRejectedStatement(database, event.id, now),
  ]);
  const matched = await database.prepare(
    "SELECT 1 AS matched FROM orders WHERE payment_intent_id = ? LIMIT 1",
  ).bind(refund.paymentIntentId).first();
  return matched ? "processed" : "recorded_unmatched";
}

function normalizeDispute(dispute, eventType) {
  if (dispute?.object !== "dispute") {
    throw new ValidationError("risk_invalid", "unexpected dispute object");
  }
  const riskId = stripeId(dispute.id, "dispute ID", "risk_invalid");
  const paymentIntentId = stripeId(
    dispute.payment_intent,
    "dispute PaymentIntent",
    "risk_invalid",
  );
  const status = requiredString(dispute.status, "dispute status", 80, "risk_invalid");
  let disposition = "active";
  if (status === "lost") disposition = "lost";
  else if (SAFE_DISPUTE_STATUSES.has(status)) {
    disposition = "safe";
  } else if (eventType === "charge.dispute.closed") {
    // Unknown terminal statuses remain held for owner review rather than auto-release.
    disposition = "active";
  }
  return { riskId, paymentIntentId, riskType: "dispute", status, disposition };
}

function normalizeReview(review, eventType) {
  if (review?.object !== "review") {
    throw new ValidationError("risk_invalid", "unexpected review object");
  }
  const riskId = stripeId(review.id, "review ID", "risk_invalid");
  const paymentIntentId = stripeId(
    review.payment_intent,
    "review PaymentIntent",
    "risk_invalid",
  );
  const closedReason = optionalString(review.closed_reason, 80);
  let disposition = "active";
  if (eventType === "review.closed") {
    if (SAFE_REVIEW_REASONS.has(closedReason)) {
      disposition = "safe";
    }
    else if (LOST_REVIEW_REASONS.has(closedReason)) disposition = "lost";
  }
  return {
    riskId,
    paymentIntentId,
    riskType: "review",
    status: closedReason || "open",
    disposition,
  };
}

function validateRisk(event) {
  if (DISPUTE_EVENTS.has(event.type)) {
    return normalizeDispute(event.data.object, event.type);
  }
  return normalizeReview(event.data.object, event.type);
}

async function recordRisk(database, event, risk, now) {
  if (await eventWasProcessed(database, event.id)) {
    await reconciliationStatement(
      database,
      "payment_intent_id",
      risk.paymentIntentId,
      now,
    ).run();
    return "duplicate";
  }
  const existing = await database.prepare(
    `SELECT payment_intent_id, risk_type
       FROM risk_events
      WHERE risk_id = ?`,
  ).bind(risk.riskId).first();
  if (
    existing
    && (
      existing.payment_intent_id !== risk.paymentIntentId
      || existing.risk_type !== risk.riskType
    )
  ) {
    throw new ValidationError("risk_conflict", "stored risk contract conflict");
  }
  const riskStatement = database.prepare(
    `INSERT INTO risk_events(
       risk_id, payment_intent_id, risk_type, status, disposition,
       stripe_created_at, updated_at
     ) VALUES (?, ?, ?, ?, ?, ?, ?)
     ON CONFLICT(risk_id) DO UPDATE SET
       payment_intent_id = excluded.payment_intent_id,
       risk_type = excluded.risk_type,
       status = excluded.status,
       disposition = excluded.disposition,
       stripe_created_at = excluded.stripe_created_at,
       updated_at = excluded.updated_at
     WHERE excluded.stripe_created_at > risk_events.stripe_created_at
        OR (
          excluded.stripe_created_at = risk_events.stripe_created_at
          AND CASE excluded.disposition
            WHEN 'lost' THEN 3
            WHEN 'active' THEN 2
            ELSE 1
          END > CASE risk_events.disposition
            WHEN 'lost' THEN 3
            WHEN 'active' THEN 2
            ELSE 1
          END
        )`,
  ).bind(
    risk.riskId,
    risk.paymentIntentId,
    risk.riskType,
    risk.status,
    risk.disposition,
    event.created,
    now,
  );
  const objectState = eventObjectState(
    event.type,
    event.data.object,
    `${risk.status}:${risk.disposition}`,
  );
  await database.batch([
    riskStatement,
    reconciliationStatement(
      database,
      "payment_intent_id",
      risk.paymentIntentId,
      now,
    ),
    eventStatement(database, event, objectState, now),
    resolveRejectedStatement(database, event.id, now),
  ]);
  const matched = await database.prepare(
    "SELECT 1 AS matched FROM orders WHERE payment_intent_id = ? LIMIT 1",
  ).bind(risk.paymentIntentId).first();
  return matched ? "processed" : "recorded_unmatched";
}

async function readBoundedPayload(request) {
  const contentLength = request.headers.get("Content-Length");
  if (contentLength !== null) {
    const declaredLength = Number(contentLength);
    if (!Number.isFinite(declaredLength) || declaredLength < 0) {
      throw new ValidationError("payload_invalid", "invalid content length");
    }
    if (declaredLength > MAX_WEBHOOK_BYTES) {
      throw new ValidationError("payload_too_large", "payload too large");
    }
  }
  if (!request.body) return "";
  const reader = request.body.getReader();
  const chunks = [];
  let receivedBytes = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      receivedBytes += value.byteLength;
      if (receivedBytes > MAX_WEBHOOK_BYTES) {
        await reader.cancel("payload too large");
        throw new ValidationError("payload_too_large", "payload too large");
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const payload = new Uint8Array(receivedBytes);
  let offset = 0;
  for (const chunk of chunks) {
    payload.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return new TextDecoder().decode(payload);
}

async function quarantine(database, event, payload, reasonCode) {
  const now = Math.floor(Date.now() / 1000);
  const fallbackEventId = await payloadFingerprint(payload);
  await recordRejectedEvent(database, event, reasonCode, now, fallbackEventId);
  console.warn("Stripe event quarantined", {
    eventId: optionalString(event?.id, 255) || fallbackEventId,
    reasonCode,
  });
  return response("retry later\n", 503, { "Retry-After": "300" });
}

async function webhook(request, env) {
  let payload;
  try {
    payload = await readBoundedPayload(request);
  } catch (error) {
    if (error instanceof ValidationError && error.code === "payload_too_large") {
      return response("payload too large\n", 413);
    }
    return response("invalid webhook\n", 400);
  }

  const allowTest = env.STATUSPULSE_ALLOW_TEST === "true";
  const secret = allowTest
    ? env.STRIPE_TEST_WEBHOOK_SECRET
    : env.STRIPE_WEBHOOK_SECRET;
  if (typeof secret !== "string" || !secret) {
    return response("service unavailable\n", 503, { "Retry-After": "300" });
  }

  let event;
  try {
    event = await verifyStripeSignature(
      payload,
      request.headers.get("Stripe-Signature"),
      secret,
    );
  } catch {
    return response("invalid webhook\n", 400);
  }

  if (typeof event?.type !== "string" || !RELEVANT_EVENTS.has(event.type)) {
    return response("ignored\n");
  }
  if (!event.data?.object || typeof event.data.object !== "object") {
    return quarantine(env.DB, event, payload, "envelope_invalid");
  }
  try {
    requiredString(event.id, "event ID", 255, "envelope_invalid");
    requiredString(
      event.data.object.id,
      "event object ID",
      255,
      "envelope_invalid",
    );
    if (!Number.isInteger(event.created) || event.created <= 0) {
      throw new ValidationError(
        "envelope_invalid",
        "event creation timestamp is invalid",
      );
    }
  } catch (error) {
    return quarantine(env.DB, event, payload, error.code || "envelope_invalid");
  }
  if (allowTest ? event.livemode !== false : event.livemode !== true) {
    return quarantine(env.DB, event, payload, "mode_mismatch");
  }

  let expectedPaymentLink;
  try {
    expectedPaymentLink = configuredPaymentLink(env.STATUSPULSE_PAYMENT_LINK_ID);
  } catch (error) {
    return quarantine(env.DB, event, payload, error.code || "configuration_invalid");
  }

  if (CHECKOUT_EVENTS.has(event.type)) {
    const observedLink = event.data.object.payment_link;
    const claimsPilot = (
      event.data.object.metadata?.offer_id === EXPECTED_OFFER_ID
      && event.data.object.metadata?.offer_version === EXPECTED_OFFER_VERSION
    );
    if (
      (typeof observedLink === "string" && observedLink !== expectedPaymentLink)
      || ((observedLink === null || observedLink === undefined) && !claimsPilot)
    ) {
      return response("ignored\n");
    }
  }
  if (event.livemode === true && env.STATUSPULSE_ALLOW_LIVE !== "true") {
    return quarantine(env.DB, event, payload, "live_mode_disabled");
  }

  if (REFUND_EVENTS.has(event.type)) {
    const observedCurrency = event.data.object.currency;
    if (typeof observedCurrency === "string" && observedCurrency !== EXPECTED_CURRENCY) {
      return response("ignored\n");
    }
  }

  const now = Math.floor(Date.now() / 1000);
  try {
    let result;
    if (CHECKOUT_EVENTS.has(event.type)) {
      const order = validatePilotSession(event.data.object, expectedPaymentLink);
      result = await recordCheckout(env.DB, event, order, now);
    } else if (REFUND_EVENTS.has(event.type)) {
      const refund = validateRefund(event.data.object);
      result = await recordRefund(env.DB, event, refund, now);
    } else {
      const risk = validateRisk(event);
      result = await recordRisk(env.DB, event, risk, now);
    }
    return response(`${result}\n`);
  } catch (error) {
    if (error instanceof ValidationError) {
      return quarantine(env.DB, event, payload, error.code || "contract_invalid");
    }
    throw error;
  }
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    try {
      if (request.method === "GET" && url.pathname === "/") {
        return response("StatusPulse order service\n");
      }
      if (request.method === "GET" && url.pathname === "/healthz") {
        const result = await env.DB.prepare(HEALTH_QUERY).first();
        return result?.ok === 1
          ? response("ok\n")
          : response("database unavailable\n", 503);
      }
      if (request.method === "POST" && url.pathname === "/stripe/webhook") {
        return await webhook(request, env);
      }
      if (url.pathname === "/stripe/webhook") {
        return response("method not allowed\n", 405, { Allow: "POST" });
      }
      return response("not found\n", 404);
    } catch (error) {
      console.error("Unhandled StatusPulse Worker error", {
        name: error?.name || "Error",
      });
      return response("internal error\n", 500);
    }
  },
};

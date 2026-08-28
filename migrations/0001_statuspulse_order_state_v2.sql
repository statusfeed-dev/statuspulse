-- One-time replacement of the unused legacy subscription/order schema.
-- The deploy preflight renames an exact legacy stripe_events table to
-- legacy_stripe_events_v1, which this migration deliberately never touches.
-- Keeping stripe_events in the guard also makes a direct/bypassed migration
-- fail atomically instead of dropping unarchived legacy event rows.

CREATE TABLE IF NOT EXISTS stripe_events (_migration_placeholder INTEGER);
CREATE TABLE IF NOT EXISTS entitlements (_migration_placeholder INTEGER);
CREATE TABLE IF NOT EXISTS access_sessions (_migration_placeholder INTEGER);
CREATE TABLE IF NOT EXISTS deliveries (_migration_placeholder INTEGER);
CREATE TABLE IF NOT EXISTS funnel_events (_migration_placeholder INTEGER);
CREATE TABLE IF NOT EXISTS orders (_migration_placeholder INTEGER);
CREATE TABLE IF NOT EXISTS order_events (_migration_placeholder INTEGER);
CREATE TABLE IF NOT EXISTS refunds (_migration_placeholder INTEGER);
CREATE TABLE IF NOT EXISTS risk_events (_migration_placeholder INTEGER);
CREATE TABLE IF NOT EXISTS rejected_events (_migration_placeholder INTEGER);

DROP TABLE IF EXISTS _statuspulse_migration_guard;
CREATE TABLE _statuspulse_migration_guard (
    row_count INTEGER NOT NULL CHECK(row_count = 0)
);
INSERT INTO _statuspulse_migration_guard SELECT COUNT(*) FROM stripe_events;
INSERT INTO _statuspulse_migration_guard SELECT COUNT(*) FROM entitlements;
INSERT INTO _statuspulse_migration_guard SELECT COUNT(*) FROM access_sessions;
INSERT INTO _statuspulse_migration_guard SELECT COUNT(*) FROM deliveries;
INSERT INTO _statuspulse_migration_guard SELECT COUNT(*) FROM funnel_events;
INSERT INTO _statuspulse_migration_guard SELECT COUNT(*) FROM orders;
INSERT INTO _statuspulse_migration_guard SELECT COUNT(*) FROM order_events;
INSERT INTO _statuspulse_migration_guard SELECT COUNT(*) FROM refunds;
INSERT INTO _statuspulse_migration_guard SELECT COUNT(*) FROM risk_events;
INSERT INTO _statuspulse_migration_guard SELECT COUNT(*) FROM rejected_events;

DROP TABLE _statuspulse_migration_guard;
DROP TABLE stripe_events;
DROP TABLE entitlements;
DROP TABLE access_sessions;
DROP TABLE deliveries;
DROP TABLE funnel_events;
DROP TABLE orders;
DROP TABLE order_events;
DROP TABLE refunds;
DROP TABLE risk_events;
DROP TABLE rejected_events;

CREATE TABLE orders (
    checkout_session_id TEXT PRIMARY KEY,
    payment_link_id TEXT NOT NULL,
    payment_intent_id TEXT UNIQUE,
    offer_id TEXT NOT NULL,
    offer_version TEXT NOT NULL,
    amount_subtotal INTEGER NOT NULL CHECK(amount_subtotal >= 0),
    amount_tax INTEGER NOT NULL CHECK(amount_tax >= 0),
    amount_total INTEGER NOT NULL CHECK(amount_total > 0),
    currency TEXT NOT NULL CHECK(currency = 'usd'),
    checkout_state TEXT NOT NULL CHECK(checkout_state IN (
        'pending', 'paid', 'failed', 'expired'
    )),
    checkout_event_created_at INTEGER NOT NULL,
    eligibility_state TEXT NOT NULL CHECK(eligibility_state IN (
        'pending', 'eligible', 'manual_refund_required'
    )),
    payment_state TEXT NOT NULL CHECK(payment_state IN (
        'pending', 'paid', 'failed', 'expired', 'refund_pending',
        'partially_refunded', 'refunded', 'under_review', 'disputed'
    )),
    fulfillment_state TEXT NOT NULL CHECK(fulfillment_state IN (
        'pending', 'queued', 'in_progress', 'delivered', 'on_hold', 'canceled'
    )),
    refunded_amount INTEGER NOT NULL DEFAULT 0 CHECK(refunded_amount >= 0),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    paid_at INTEGER,
    delivered_at INTEGER
);
CREATE INDEX orders_payment_state_idx
    ON orders(payment_state, updated_at);
CREATE INDEX orders_fulfillment_state_idx
    ON orders(fulfillment_state, updated_at);

CREATE TABLE order_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    object_state TEXT NOT NULL,
    stripe_created_at INTEGER NOT NULL,
    processed_at INTEGER NOT NULL
);
CREATE INDEX order_events_object_idx
    ON order_events(event_type, object_id, processed_at);

CREATE TABLE refunds (
    refund_id TEXT PRIMARY KEY,
    payment_intent_id TEXT NOT NULL,
    amount INTEGER NOT NULL CHECK(amount > 0),
    currency TEXT NOT NULL CHECK(currency = 'usd'),
    status TEXT NOT NULL CHECK(status IN (
        'pending', 'requires_action', 'succeeded', 'failed', 'canceled'
    )),
    stripe_created_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX refunds_payment_intent_idx
    ON refunds(payment_intent_id, status);

CREATE TABLE risk_events (
    risk_id TEXT PRIMARY KEY,
    payment_intent_id TEXT NOT NULL,
    risk_type TEXT NOT NULL CHECK(risk_type IN ('dispute', 'review')),
    status TEXT NOT NULL,
    disposition TEXT NOT NULL CHECK(disposition IN ('active', 'safe', 'lost')),
    stripe_created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX risk_events_payment_intent_idx
    ON risk_events(payment_intent_id, disposition);

CREATE TABLE rejected_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    first_seen_at INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL,
    attempt_count INTEGER NOT NULL CHECK(attempt_count > 0),
    resolved_at INTEGER
);
CREATE INDEX rejected_events_unresolved_idx
    ON rejected_events(resolved_at, last_seen_at);

CREATE TABLE IF NOT EXISTS schema_versions (
    version INTEGER PRIMARY KEY,
    applied_at INTEGER NOT NULL
);
INSERT OR IGNORE INTO schema_versions(version, applied_at)
VALUES (2, unixepoch());

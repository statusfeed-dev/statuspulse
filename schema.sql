CREATE TABLE IF NOT EXISTS stripe_events (event_id TEXT PRIMARY KEY, processed_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS entitlements (checkout_session_id TEXT PRIMARY KEY, subscription_id TEXT NOT NULL, active INTEGER NOT NULL, created_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS entitlements_subscription_idx ON entitlements(subscription_id);
CREATE TABLE IF NOT EXISTS access_sessions (token_hash TEXT PRIMARY KEY, checkout_session_id TEXT NOT NULL, expires_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS access_sessions_expiry_idx ON access_sessions(expires_at);
CREATE TABLE IF NOT EXISTS deliveries (
    checkout_session_id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK(status IN ('queued', 'completed', 'failed')),
    queued_at INTEGER NOT NULL,
    completed_at INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS funnel_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_name TEXT NOT NULL CHECK(event_name IN ('payment_succeeded','fulfillment_queued','delivery_completed','delivery_failed','refund')),
    occurred_at INTEGER NOT NULL,
    source_id TEXT NOT NULL,
    metadata TEXT,
    UNIQUE(event_name, source_id)
);
CREATE INDEX IF NOT EXISTS funnel_events_time_idx ON funnel_events(occurred_at);
DELETE FROM access_sessions WHERE expires_at <= unixepoch();
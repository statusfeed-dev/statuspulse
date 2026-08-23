CREATE TABLE IF NOT EXISTS stripe_events (event_id TEXT PRIMARY KEY, processed_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS entitlements (checkout_session_id TEXT PRIMARY KEY, subscription_id TEXT NOT NULL, active INTEGER NOT NULL, created_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS entitlements_subscription_idx ON entitlements(subscription_id);
CREATE TABLE IF NOT EXISTS access_sessions (token_hash TEXT PRIMARY KEY, checkout_session_id TEXT NOT NULL, expires_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS access_sessions_expiry_idx ON access_sessions(expires_at);
DELETE FROM access_sessions WHERE expires_at <= unixepoch();
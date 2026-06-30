-- IVP application database schema (PostgreSQL RDS).
-- The dummy datasets in dummy.py mirror these shapes one-for-one.

CREATE TABLE IF NOT EXISTS transactions (
    txn_id        TEXT PRIMARY KEY,
    card_id       TEXT NOT NULL,
    driver_id     TEXT NOT NULL,
    merchant      TEXT NOT NULL,
    category      TEXT NOT NULL,
    city          TEXT,
    amount_gbp    NUMERIC(12,2) NOT NULL,
    occurred_at   TIMESTAMP NOT NULL,
    status        TEXT NOT NULL DEFAULT 'approved',
    is_anomaly    BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_txn_card ON transactions(card_id);
CREATE INDEX IF NOT EXISTS idx_txn_time ON transactions(occurred_at);

CREATE TABLE IF NOT EXISTS cards (
    card_id          TEXT PRIMARY KEY,
    driver_id        TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'active',
    monthly_limit_gbp NUMERIC(12,2) NOT NULL,
    vehicle_reg      TEXT
);

CREATE TABLE IF NOT EXISTS fuel_prices (
    city                  TEXT NOT NULL,
    station               TEXT NOT NULL,
    price_per_litre_gbp   NUMERIC(6,3) NOT NULL,
    updated_at            DATE NOT NULL,
    PRIMARY KEY (city, station)
);

CREATE TABLE IF NOT EXISTS spend_series (
    month      DATE PRIMARY KEY,
    spend_gbp  NUMERIC(14,2) NOT NULL
);

-- Identity tables (populated from ivp_local.db via scripts/etl_from_sqlite.py).
-- Optional: the app falls back to the demo persona table in api/auth.py when
-- AUTH_BACKEND != "rds" or these tables are empty.
CREATE TABLE IF NOT EXISTS users (
    id           TEXT PRIMARY KEY,
    org_id       TEXT,
    name         TEXT NOT NULL,
    email        TEXT,
    role         TEXT NOT NULL DEFAULT 'viewer',
    department   TEXT,
    region       TEXT,
    manager_id   TEXT,
    driver_id    TEXT
);
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);

CREATE TABLE IF NOT EXISTS auth_credentials (
    user_id         TEXT PRIMARY KEY REFERENCES users(id),
    password_hash   TEXT NOT NULL,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    updated_at      TIMESTAMP
);

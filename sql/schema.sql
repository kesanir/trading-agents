CREATE TABLE market_features (
    ts TIMESTAMP PRIMARY KEY,
    symbol VARCHAR(20),
    net_gex DOUBLE PRECISION,
    zero_gamma DOUBLE PRECISION,
    gamma_slope DOUBLE PRECISION,
    chop_score DOUBLE PRECISION,
    regime VARCHAR(20),
    trend_prob DOUBLE PRECISION
);
CREATE TABLE signals (
    id SERIAL PRIMARY KEY,
    ts TIMESTAMP,
    instrument VARCHAR(20),
    direction VARCHAR(10),
    setup VARCHAR(50),
    confidence DOUBLE PRECISION,
    entry DOUBLE PRECISION,
    stop DOUBLE PRECISION,
    target DOUBLE PRECISION,
    regime VARCHAR(20),
    mag7_confirmation JSONB
);
CREATE TABLE trade_journal (
    id SERIAL PRIMARY KEY,
    signal_id INT REFERENCES signals(id),
    entry_time TIMESTAMP,
    exit_time TIMESTAMP,
    pnl DOUBLE PRECISION,
    mfe DOUBLE PRECISION,
    mae DOUBLE PRECISION,
    outcome VARCHAR(20),
    notes TEXT
);
-- ClickHouse schema for trading decisions logging
-- Run: clickhouse-client < scripts/sql/create_trading_decisions.sql

CREATE TABLE IF NOT EXISTS kospi.trading_decisions (
    -- Core
    timestamp DateTime64(3),
    signal String,              -- 'BUY', 'SELL', 'CLOSE', 'MODE_CHANGE'
    price Float64,
    mode String,                -- 'MODE_A', 'MODE_B'
    reason String,

    -- DL Probabilities
    up_prob_h1 Float32,
    up_prob_h3 Float32,
    up_prob_h5 Float32,
    up_prob_h10 Float32,

    -- Z-scores
    zscore_h1 Float32,
    zscore_h10 Float32,

    -- Technical indicators
    ma_fast Float64,
    ma_slow Float64,
    ma_bullish UInt8,
    rsi Float32,
    atr Float32,
    cloud_top Float64,
    cloud_bottom Float64,
    above_cloud UInt8,

    -- Market context
    spread Float32,
    ofi_zscore Float32,
    regime String,

    -- Position result (updated on close)
    trade_id String,            -- Links entry to exit
    entry_price Float64,
    exit_price Float64,
    pnl_points Float32,
    hold_time_minutes UInt16,

    -- Metadata
    strategy String DEFAULT 'dual_mode',
    session_id String
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(timestamp)
ORDER BY (timestamp, trade_id);

-- Index for common queries
ALTER TABLE kospi.trading_decisions ADD INDEX idx_mode (mode) TYPE set(10) GRANULARITY 4;
ALTER TABLE kospi.trading_decisions ADD INDEX idx_signal (signal) TYPE set(10) GRANULARITY 4;
ALTER TABLE kospi.trading_decisions ADD INDEX idx_session (session_id) TYPE bloom_filter() GRANULARITY 4;

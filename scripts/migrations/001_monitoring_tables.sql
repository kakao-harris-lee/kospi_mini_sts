-- Migration: 001_monitoring_tables
-- Feature: 001-monitoring-alerting
-- Date: 2026-01-09
-- Description: Create tables for monitoring and alerting system

-- Strategy Performance Metrics Table
-- Stores aggregated performance data for trading strategies
CREATE TABLE IF NOT EXISTS kospi.strategy_performance (
    datetime DateTime,
    strategy String,
    symbol String,
    period String,
    total_pnl Float64,
    realized_pnl Float64,
    unrealized_pnl Float64,
    trade_count UInt32,
    win_count UInt32,
    loss_count UInt32,
    win_rate Float32,
    avg_win Float64,
    avg_loss Float64,
    profit_factor Float32,
    max_drawdown Float64,
    sharpe_ratio Float32
) ENGINE = MergeTree()
ORDER BY (strategy, symbol, datetime)
PARTITION BY toYYYYMM(datetime)
TTL datetime + INTERVAL 1 YEAR;

-- Alert Audit Log Table
-- Stores all alerts for audit and debugging purposes
CREATE TABLE IF NOT EXISTS kospi.alert_log (
    id UUID,
    created_at DateTime,
    alert_type String,
    channel String,
    priority String,
    title String,
    message String,
    delivery_status String,
    delivered_at Nullable(DateTime),
    retry_count UInt8,
    last_error Nullable(String),
    source_id Nullable(UUID),
    source_type Nullable(String)
) ENGINE = MergeTree()
ORDER BY (created_at, alert_type)
PARTITION BY toYYYYMM(created_at)
TTL created_at + INTERVAL 90 DAY;

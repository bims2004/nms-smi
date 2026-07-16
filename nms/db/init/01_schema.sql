-- Schema NMS Fase 1
-- Dijalankan otomatis oleh postgres saat volume masih kosong.

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- =========================================================
-- Perangkat yang di-poll (router/switch)
-- =========================================================
CREATE TABLE devices (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    ip              INET NOT NULL,
    vendor          TEXT NOT NULL DEFAULT 'generic',      -- mikrotik | huawei | generic
    poll_method     TEXT NOT NULL DEFAULT 'snmp',         -- snmp | mikrotik_api
    snmp_community  TEXT DEFAULT 'public',
    snmp_port       INT  DEFAULT 161,
    api_username    TEXT,                                  -- untuk mikrotik_api
    api_password    TEXT,
    api_port        INT  DEFAULT 8728,
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE (ip, poll_method)
);

-- =========================================================
-- Pelanggan / service yang dimonitor
-- =========================================================
CREATE TABLE customers (
    id                SERIAL PRIMARY KEY,
    name              TEXT NOT NULL,
    service_id        TEXT UNIQUE,                         -- ID pelanggan internal ISP
    device_id         INT NOT NULL REFERENCES devices(id),
    monitor_type      TEXT NOT NULL,                       -- 'snmp_if' | 'pppoe'
    if_index          INT,                                 -- untuk snmp_if (dedicated)
    if_name           TEXT,                                -- deskriptif / opsional
    pppoe_username    TEXT,                                -- untuk pppoe di Mikrotik
    threshold_bps     BIGINT NOT NULL DEFAULT 1000,        -- di bawah ini = traffic zero
    enabled           BOOLEAN NOT NULL DEFAULT TRUE,
    status            TEXT NOT NULL DEFAULT 'unknown',     -- up | down | unknown
    status_changed_at TIMESTAMPTZ,
    CONSTRAINT chk_monitor CHECK (
        (monitor_type = 'snmp_if' AND if_index IS NOT NULL)
        OR (monitor_type = 'pppoe' AND pppoe_username IS NOT NULL)
    )
);

-- =========================================================
-- Time-series traffic (hypertable)
-- =========================================================
CREATE TABLE traffic_samples (
    time        TIMESTAMPTZ NOT NULL,
    customer_id INT NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    in_bps      BIGINT,            -- NULL pada sample pertama (belum ada delta)
    out_bps     BIGINT,
    in_octets   NUMERIC,           -- raw counter 64-bit
    out_octets  NUMERIC,
    link_up     BOOLEAN            -- ifOperStatus up / sesi PPPoE aktif
);

SELECT create_hypertable('traffic_samples', 'time');
CREATE INDEX idx_samples_cust_time ON traffic_samples (customer_id, time DESC);

-- Retensi 90 hari, sesuaikan dengan kebutuhan laporan
SELECT add_retention_policy('traffic_samples', INTERVAL '90 days');

-- =========================================================
-- Alert / incident log
-- =========================================================
CREATE TABLE alerts (
    id          SERIAL PRIMARY KEY,
    customer_id INT NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    alert_type  TEXT NOT NULL,                 -- link_down | session_down | traffic_zero
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ,
    notified    BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX idx_alerts_open ON alerts (customer_id) WHERE resolved_at IS NULL;

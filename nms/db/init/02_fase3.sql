-- =========================================================
-- Fase 3: kesehatan perangkat, jadwal pemeliharaan,
--         deteksi degradasi berbasis baseline, eskalasi.
--
-- File ini IDEMPOTENT: aman dijalankan berulang, baik pada
-- database baru (otomatis oleh initdb) maupun database yang
-- sudah berisi data (lewat scripts/upgrade-db.sh).
-- =========================================================

-- ---------- Kesehatan perangkat ----------
-- Sebelumnya perangkat yang mati membuat semua pelanggannya tampak
-- "tanpa data" tanpa ada alert. Kolom ini membuat perangkat sendiri
-- bisa dipantau, sehingga satu perangkat mati = satu alert, bukan
-- ratusan alert pelanggan palsu.
ALTER TABLE devices ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE devices ADD COLUMN IF NOT EXISTS status_changed_at TIMESTAMPTZ;
ALTER TABLE devices ADD COLUMN IF NOT EXISTS last_ok_at TIMESTAMPTZ;
ALTER TABLE devices ADD COLUMN IF NOT EXISTS fail_count INT NOT NULL DEFAULT 0;

-- ---------- Deteksi degradasi ----------
ALTER TABLE customers ADD COLUMN IF NOT EXISTS baseline_enabled BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE customers ADD COLUMN IF NOT EXISTS baseline_drop_pct INT NOT NULL DEFAULT 80;

-- ---------- Alert: dukung alert level perangkat + ack + eskalasi ----------
ALTER TABLE alerts ALTER COLUMN customer_id DROP NOT NULL;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS device_id INT REFERENCES devices(id) ON DELETE CASCADE;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS severity TEXT NOT NULL DEFAULT 'major';
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS escalated_at TIMESTAMPTZ;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS ack_by TEXT;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS ack_at TIMESTAMPTZ;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS suppressed BOOLEAN NOT NULL DEFAULT FALSE;

-- Tutup alert terbuka yang duplikat sebelum unique index dibuat.
-- Tanpa ini, database yang pernah mengalami race condition akan menolak
-- migrasi. Yang terbaru dipertahankan.
WITH dupes AS (
    SELECT id, ROW_NUMBER() OVER (
               PARTITION BY customer_id ORDER BY started_at DESC, id DESC
           ) AS rn
    FROM alerts
    WHERE resolved_at IS NULL AND customer_id IS NOT NULL
)
UPDATE alerts SET resolved_at = now()
WHERE id IN (SELECT id FROM dupes WHERE rn > 1);

-- Satu alert terbuka per pelanggan / per perangkat
CREATE UNIQUE INDEX IF NOT EXISTS uq_alert_open_customer
    ON alerts (customer_id) WHERE resolved_at IS NULL AND customer_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_alert_open_device
    ON alerts (device_id) WHERE resolved_at IS NULL AND device_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_alerts_started ON alerts (started_at DESC);

-- ---------- Jadwal pemeliharaan ----------
-- Selama jendela aktif, alert tetap dicatat tapi tidak dikirim ke Telegram
-- dan ditandai suppressed, sehingga laporan SLA bisa mengecualikannya.
CREATE TABLE IF NOT EXISTS maintenance_windows (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    starts_at   TIMESTAMPTZ NOT NULL,
    ends_at     TIMESTAMPTZ NOT NULL,
    device_id   INT REFERENCES devices(id) ON DELETE CASCADE,
    customer_id INT REFERENCES customers(id) ON DELETE CASCADE,
    note        TEXT,
    created_by  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_mw_range CHECK (ends_at > starts_at)
);

CREATE INDEX IF NOT EXISTS idx_mw_active ON maintenance_windows (starts_at, ends_at);

-- ---------- Cache baseline ----------
-- Baseline dihitung per pelanggan per (hari, jam) dalam waktu lokal.
CREATE TABLE IF NOT EXISTS traffic_baseline (
    customer_id INT NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    dow         SMALLINT NOT NULL,   -- 0=Minggu .. 6=Sabtu (waktu lokal)
    hour        SMALLINT NOT NULL,   -- 0..23 (waktu lokal)
    median_bps  BIGINT NOT NULL,
    samples     INT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (customer_id, dow, hour)
);

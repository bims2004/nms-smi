-- =========================================================
-- Monitoring ODP dengan pelanggan sebagai sensornya.
--
-- Tidak ada perangkat aktif di dalam box ODP — cuma splitter pasif. Jadi ODP
-- tidak bisa di-ping, di-SNMP, atau ditanyai apa pun. Satu-satunya bukti
-- bahwa ODP bermasalah adalah pelanggan di bawahnya mati BERSAMAAN.
--
-- Satu pelanggan mati = drop cable-nya sendiri.
-- Semua pelanggan di ODP mati bersamaan = feeder putus / splitter rusak.
--
-- IDEMPOTENT: aman dijalankan berulang.
-- =========================================================

CREATE TABLE IF NOT EXISTS odps (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    device_id   INT REFERENCES devices(id) ON DELETE SET NULL,
    lokasi      TEXT,                    -- alamat / patokan untuk teknisi
    latitude    NUMERIC(10, 7),
    longitude   NUMERIC(10, 7),
    kapasitas   INT,                     -- jumlah port splitter
    catatan     TEXT,
    enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    status      TEXT NOT NULL DEFAULT 'unknown',
    status_changed_at TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_odp_status') THEN
        ALTER TABLE odps ADD CONSTRAINT chk_odp_status
            CHECK (status IN ('up', 'down', 'unknown'));
    END IF;
END $$;

ALTER TABLE customers ADD COLUMN IF NOT EXISTS odp_id INT
    REFERENCES odps(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_customers_odp ON customers (odp_id)
    WHERE odp_id IS NOT NULL;

-- ---------- alert untuk ODP ----------
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS odp_id INT
    REFERENCES odps(id) ON DELETE CASCADE;

-- Alert pelanggan yang disebabkan ODP menunjuk ke alert ODP-nya.
--
-- SENGAJA TIDAK memakai kolom `suppressed`. Kolom itu dipakai jendela
-- pemeliharaan, dan laporan SLA punya tombol untuk mengecualikannya. ODP
-- putus BUKAN pemeliharaan — pelanggannya benar-benar mati dan itu harus
-- tetap dihitung melawan SLA. Yang perlu ditekan hanyalah notifikasinya:
-- satu pesan "ODP-CAKRA-03 down (8 pelanggan)" jauh lebih berguna daripada
-- delapan pesan terpisah yang membanjiri Telegram pada saat yang sama.
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS parent_alert_id INT
    REFERENCES alerts(id) ON DELETE SET NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_alert_open_odp ON alerts (odp_id)
    WHERE resolved_at IS NULL AND odp_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_alerts_parent ON alerts (parent_alert_id)
    WHERE parent_alert_id IS NOT NULL;

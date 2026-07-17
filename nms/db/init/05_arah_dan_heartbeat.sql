-- =========================================================
-- 1. Arah interface  2. Denyut nadi NMS
-- IDEMPOTENT: aman dijalankan berulang.
-- =========================================================

-- ---------- Arah interface ----------
-- ifInOctets/ifOutOctets bermakna dari sudut pandang PERANGKAT, bukan
-- pelanggan. Di port yang menghadap pelanggan, "in" = masuk ke port =
-- datang dari pelanggan = UPLOAD pelanggan. Di port yang menghadap upstream,
-- kebalikannya.
--
-- Counter mentah tetap disimpan apa adanya (in_bps = ifIn, selalu). Kolom ini
-- hanya dipakai saat MENAMPILKAN. Database menyimpan kebenaran; tampilan yang
-- menafsirkan. Kalau in/out ditukar saat menyimpan, data lama jadi tidak bisa
-- ditafsirkan lagi begitu setelan berubah.
ALTER TABLE customers ADD COLUMN IF NOT EXISTS if_direction TEXT
    NOT NULL DEFAULT 'ke_pelanggan';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_if_direction') THEN
        ALTER TABLE customers ADD CONSTRAINT chk_if_direction
            CHECK (if_direction IN ('ke_pelanggan', 'ke_upstream'));
    END IF;
END $$;

-- PPPoE tidak terpengaruh: poller Mikrotik sudah memetakan tx/rx ke sudut
-- pandang pelanggan sejak awal.

-- ---------- Denyut nadi NMS ----------
-- Kalau NMS mati semalam, tidak ada sampel yang tercatat — dan laporan SLA
-- membaca "tidak ada gangguan" sebagai uptime 100%. Itu bohong yang berbahaya
-- karena arahnya menguntungkan kita sendiri.
--
-- Tabel ini mencatat detak collector. Periode tanpa detak = NMS buta, dan
-- laporan SLA bisa mengatakannya terus terang.
CREATE TABLE IF NOT EXISTS nms_heartbeat (
    time      TIMESTAMPTZ NOT NULL,
    component TEXT NOT NULL,          -- 'collector' | 'alerter'
    PRIMARY KEY (time, component)
);

CREATE INDEX IF NOT EXISTS idx_heartbeat_time ON nms_heartbeat (time DESC);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
        -- Detak jauh lebih jarang dari sampel traffic, tapi disimpan lebih
        -- lama: dipakai laporan SLA yang siklusnya bulanan-tahunan.
        BEGIN
            PERFORM add_retention_policy('nms_heartbeat', INTERVAL '400 days',
                                         if_not_exists => TRUE);
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE 'Retensi heartbeat dilewati: %', SQLERRM;
        END;
    END IF;
END $$;

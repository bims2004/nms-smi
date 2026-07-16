-- =========================================================
-- Kompresi kolom TimescaleDB.
--
-- Data time-series adalah kasus terbaik untuk kompresi kolom: nilai
-- berurutan saling mirip, jadi yang disimpan cukup selisihnya. Sampel
-- traffic per menit sangat cocok — customer_id berulang terus, waktunya
-- berjarak tetap, dan counter octet naik nyaris linier.
--
-- Angka acuan (baris ~73 byte + indeks ~27%):
--   500 pelanggan, polling 60 detik = 720.000 baris/hari ~ 72 MB/hari
--   90 hari  ~ 6,5 GB tanpa kompresi
--
-- URUTAN PENTING: ambang kompresi (7 hari) HARUS lebih lama dari jendela
-- refresh continuous aggregate (3 hari, lihat 03_rollup.sql). Kalau tidak,
-- rollup akan terus-menerus membongkar chunk yang baru saja dipadatkan —
-- boros CPU dan hasilnya malah lebih lambat.
--
-- IDEMPOTENT: aman dijalankan berulang.
-- =========================================================

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
        RAISE NOTICE 'TimescaleDB tidak terpasang — kompresi dilewati.';
        RETURN;
    END IF;

    -- ---------- traffic_samples ----------
    -- segmentby customer_id: baris milik satu pelanggan dikelompokkan, jadi
    --   customer_id disimpan sekali per kelompok, bukan sekali per baris.
    -- orderby time DESC: waktu jadi berurutan dalam kelompok sehingga bisa
    --   disimpan sebagai selisih antar-baris.
    IF NOT EXISTS (
        SELECT 1 FROM timescaledb_information.hypertables
        WHERE hypertable_name = 'traffic_samples' AND compression_enabled
    ) THEN
        EXECUTE $ddl$
            ALTER TABLE traffic_samples SET (
                timescaledb.compress,
                timescaledb.compress_segmentby = 'customer_id',
                timescaledb.compress_orderby   = 'time DESC'
            )
        $ddl$;
        RAISE NOTICE 'Kompresi traffic_samples diaktifkan.';
    ELSE
        RAISE NOTICE 'Kompresi traffic_samples sudah aktif.';
    END IF;

    PERFORM add_compression_policy('traffic_samples', INTERVAL '7 days',
                                   if_not_exists => TRUE);

    -- ---------- traffic_hourly ----------
    IF to_regclass('public.traffic_hourly') IS NOT NULL THEN
        BEGIN
            EXECUTE 'ALTER MATERIALIZED VIEW traffic_hourly '
                    'SET (timescaledb.compress = true)';
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE 'Kompresi traffic_hourly dilewati: %', SQLERRM;
        END;
        BEGIN
            PERFORM add_compression_policy('traffic_hourly', INTERVAL '90 days',
                                           if_not_exists => TRUE);
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE 'Policy kompresi traffic_hourly dilewati: %', SQLERRM;
        END;
    END IF;

    RAISE NOTICE 'Selesai. Chunk lama dipadatkan bertahap oleh job latar '
                 'belakang. Untuk memadatkan sekarang juga, lihat README.';
END $$;

-- =========================================================
-- Rollup jam-an untuk riwayat pemakaian jangka panjang.
--
-- Masalahnya: traffic_samples dihapus otomatis setelah 90 hari.
-- Menyimpan sampel per menit selamanya terlalu boros — 1 pelanggan
-- = 525.600 baris/tahun. Rollup jam-an cuma 8.760 baris/tahun,
-- sekitar 60x lebih hemat, dan cukup untuk pertanyaan
-- "berapa pemakaian pelanggan X bulan lalu".
--
-- Continuous aggregate diperbarui sendiri oleh TimescaleDB di latar
-- belakang, jadi tidak ada cron yang perlu diurus.
--
-- IDEMPOTENT: aman dijalankan berulang.
-- =========================================================

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
        RAISE NOTICE 'TimescaleDB tidak terpasang — rollup dilewati. '
                     'Riwayat tetap terbatas retensi traffic_samples.';
        RETURN;
    END IF;

    IF to_regclass('public.traffic_hourly') IS NOT NULL THEN
        RAISE NOTICE 'traffic_hourly sudah ada — dilewati.';
        RETURN;
    END IF;

    EXECUTE $ddl$
        CREATE MATERIALIZED VIEW traffic_hourly
        WITH (timescaledb.continuous) AS
        SELECT customer_id,
               time_bucket(INTERVAL '1 hour', time) AS bucket,
               avg(in_bps)  AS avg_in,
               avg(out_bps) AS avg_out,
               max(in_bps)  AS max_in,
               max(out_bps) AS max_out,
               count(*)     AS samples,
               count(*) FILTER (WHERE link_up IS FALSE) AS down_samples
        FROM traffic_samples
        WHERE in_bps IS NOT NULL
        GROUP BY customer_id, bucket
        WITH NO DATA
    $ddl$;

    -- Perbarui tiap 30 menit. end_offset 1 jam supaya jam yang masih
    -- berjalan tidak dihitung setengah jadi.
    PERFORM add_continuous_aggregate_policy('traffic_hourly',
        start_offset      => INTERVAL '3 days',
        end_offset        => INTERVAL '1 hour',
        schedule_interval => INTERVAL '30 minutes',
        if_not_exists     => TRUE);

    -- Rollup disimpan 2 tahun; sampel mentahnya tetap 90 hari.
    PERFORM add_retention_policy('traffic_hourly', INTERVAL '730 days',
                                 if_not_exists => TRUE);

    RAISE NOTICE 'traffic_hourly dibuat. Isi data lama dengan: '
                 'CALL refresh_continuous_aggregate(''traffic_hourly'', NULL, NULL);';
END $$;

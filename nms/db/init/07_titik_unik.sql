-- =========================================================
-- Cegah dua pelanggan memantau titik yang sama.
--
-- Sejak Fase 1 tidak ada yang melarang dua pelanggan menempel di ifIndex
-- yang sama pada perangkat yang sama. Akibatnya: keduanya membaca counter
-- yang sama, keduanya beralarm saat port itu mati, dan laporan SLA
-- menghitung satu gangguan sebagai dua. Hampir selalu salah ketik.
--
-- Migrasi ini HANYA memasang constraint kalau datanya memang bersih. Kalau
-- ada duplikat, dia menolak memasang dan menyebutkan yang mana — memaksa
-- diam-diam akan menggagalkan seluruh upgrade, dan itu lebih buruk.
--
-- IDEMPOTENT: aman dijalankan berulang.
-- =========================================================

DO $$
DECLARE
    n_if    INT;
    n_ppp   INT;
    contoh  TEXT;
BEGIN
    -- ---------- snmp_if: (device, if_index) ----------
    SELECT count(*) INTO n_if FROM (
        SELECT device_id, if_index FROM customers
        WHERE if_index IS NOT NULL AND monitor_type = 'snmp_if'
        GROUP BY 1, 2 HAVING count(*) > 1
    ) x;

    IF n_if > 0 THEN
        SELECT string_agg(t, '; ') INTO contoh FROM (
            SELECT 'device ' || device_id || ' ifIndex ' || if_index
                   || ' dipakai ' || count(*) || 'x' AS t
            FROM customers
            WHERE if_index IS NOT NULL AND monitor_type = 'snmp_if'
            GROUP BY device_id, if_index HAVING count(*) > 1
            LIMIT 5
        ) y;
        RAISE WARNING 'Constraint ifIndex unik TIDAK dipasang: ada % titik '
                      'yang dipantau lebih dari satu pelanggan. %', n_if, contoh;
        RAISE WARNING 'Rapikan lewat Kelola -> Pelanggan, lalu jalankan '
                      'upgrade-db.sh lagi.';
    ELSE
        IF NOT EXISTS (SELECT 1 FROM pg_indexes
                       WHERE indexname = 'uq_customer_if_point') THEN
            CREATE UNIQUE INDEX uq_customer_if_point
                ON customers (device_id, if_index)
                WHERE if_index IS NOT NULL AND monitor_type = 'snmp_if';
            RAISE NOTICE 'Constraint ifIndex unik dipasang.';
        END IF;
    END IF;

    -- ---------- pppoe: (device, pppoe_username) ----------
    SELECT count(*) INTO n_ppp FROM (
        SELECT device_id, pppoe_username FROM customers
        WHERE pppoe_username IS NOT NULL AND monitor_type = 'pppoe'
        GROUP BY 1, 2 HAVING count(*) > 1
    ) x;

    IF n_ppp > 0 THEN
        RAISE WARNING 'Constraint username PPPoE unik TIDAK dipasang: ada % '
                      'username yang dipantau lebih dari satu pelanggan.', n_ppp;
    ELSE
        IF NOT EXISTS (SELECT 1 FROM pg_indexes
                       WHERE indexname = 'uq_customer_pppoe_point') THEN
            CREATE UNIQUE INDEX uq_customer_pppoe_point
                ON customers (device_id, pppoe_username)
                WHERE pppoe_username IS NOT NULL AND monitor_type = 'pppoe';
            RAISE NOTICE 'Constraint username PPPoE unik dipasang.';
        END IF;
    END IF;
END $$;

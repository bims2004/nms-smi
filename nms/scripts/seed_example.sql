-- =========================================================
-- CONTOH seed data. Sesuaikan IP, community, credential, dll.
-- Jalankan: docker compose exec -T db psql -U nms -d nms < scripts/seed_example.sql
-- =========================================================

-- 1) Device: Huawei CE6870 (SNMP) untuk pelanggan dedicated
INSERT INTO devices (name, ip, vendor, poll_method, snmp_community)
VALUES ('CE6870-CORE', '10.10.10.1', 'huawei', 'snmp', 'publicRO')
RETURNING id;

-- 2) Device: Mikrotik RB5009 (API) untuk pelanggan PPPoE
--    Buat user API read-only di Mikrotik:
--    /user group add name=nms-ro policy=read,api
--    /user add name=nms password=RahasiaKuat group=nms-ro
INSERT INTO devices (name, ip, vendor, poll_method, api_username, api_password)
VALUES ('RB5009-BRAS', '10.10.10.2', 'mikrotik', 'mikrotik_api', 'nms', 'RahasiaKuat')
RETURNING id;

-- 3) Pelanggan dedicated di port switch (cari ifIndex dulu, lihat README)
INSERT INTO customers
    (name, service_id, device_id, monitor_type, if_index, if_name, threshold_bps)
VALUES
    ('PT Contoh Jaya', 'CUST-0001',
     (SELECT id FROM devices WHERE name = 'CE6870-CORE'),
     'snmp_if', 17, '10GE1/0/17', 1000);

-- 4) Pelanggan PPPoE di Mikrotik
INSERT INTO customers
    (name, service_id, device_id, monitor_type, pppoe_username, threshold_bps)
VALUES
    ('Budi Home', 'CUST-0002',
     (SELECT id FROM devices WHERE name = 'RB5009-BRAS'),
     'pppoe', 'budi@home', 1000);

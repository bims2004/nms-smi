# NMS Fase 1 — Traffic Down Monitoring

Network Monitoring System sederhana untuk ISP. Memonitor traffic pelanggan
secara realtime dan mengirim alert Telegram ketika traffic down.

## Arsitektur

```
[Huawei/Switch (SNMP)]  [Mikrotik BRAS (API)]
         │                      │
         └───── collector ──────┘      poll tiap 60s
                    │
                    ▼
              TimescaleDB  ◄──── alerter (cek tiap 60s)
                                    │
                                    ▼
                             Telegram (NOC)
```

Tiga container:

| Service     | Fungsi |
|-------------|--------|
| `db`        | TimescaleDB (PostgreSQL 16) — inventory + time-series traffic |
| `collector` | Poll SNMP (dedicated) & Mikrotik API (PPPoE), hitung bps dari delta counter |
| `alerter`   | Deteksi DOWN/RECOVERY, kirim notif Telegram, catat incident di tabel `alerts` |

## Logika deteksi DOWN

Sample dianggap *down* jika:
- `link_up = FALSE` → ifOperStatus down (dedicated) atau sesi PPPoE hilang, **atau**
- `in_bps + out_bps < threshold_bps` (default 1000 bps) → traffic zero.

Customer dinyatakan **DOWN** jika `CONSECUTIVE_DOWN_SAMPLES` (default 3)
sample terakhir berturut-turut down → alert dibuat + notif 🔴.
Saat sample terakhir kembali normal → alert di-resolve + notif 🟢 dengan durasi down.

Sample pertama setelah collector restart punya bps NULL dan dianggap
*unknown* — memutus hitungan berturut-turut sehingga tidak memicu false alarm.

## Setup

### 1. Konfigurasi

```bash
cp .env.example .env
nano .env   # isi TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, password DB
```

Membuat bot Telegram:
1. Chat ke `@BotFather` → `/newbot` → simpan token.
2. Masukkan bot ke grup NOC.
3. Ambil chat_id grup: buka `https://api.telegram.org/bot<TOKEN>/getUpdates`
   setelah mengirim pesan apa pun di grup — chat id grup biasanya negatif
   (misal `-1001234567890`).

### 2. Jalankan

```bash
docker compose up -d --build
docker compose logs -f collector alerter
```

Schema otomatis dibuat saat volume DB masih kosong (`db/init/01_schema.sql`).

### 3. Daftarkan device & pelanggan

Edit lalu jalankan contoh seed:

```bash
docker compose exec -T db psql -U nms -d nms < scripts/seed_example.sql
```

**Mencari ifIndex pelanggan dedicated** (dari server NMS):

```bash
snmpwalk -v2c -c <community> <ip-switch> 1.3.6.1.2.1.31.1.1.1.1
# IF-MIB::ifName.17 = STRING: 10GE1/0/17   ← angka 17 ini ifIndex-nya
```

**User API read-only di Mikrotik:**

```
/user group add name=nms-ro policy=read,api
/user add name=nms password=RahasiaKuat group=nms-ro
```

### 4. Verifikasi data masuk

```bash
docker compose exec db psql -U nms -d nms -c \
  "SELECT time, customer_id, in_bps, out_bps, link_up
   FROM traffic_samples ORDER BY time DESC LIMIT 10;"

docker compose exec db psql -U nms -d nms -c \
  "SELECT name, status, status_changed_at FROM customers;"
```

## Uji coba alert

Cara paling gampang: disable interface pelanggan lab / putus sesi PPPoE test,
tunggu ± `CONSECUTIVE_DOWN_SAMPLES × POLL_INTERVAL` (default ±3 menit),
notif 🔴 masuk ke grup. Nyalakan lagi → notif 🟢 recovery + durasi down.

## Query berguna

```sql
-- Pelanggan yang sedang down
SELECT c.name, c.service_id, a.alert_type, a.started_at
FROM alerts a JOIN customers c ON c.id = a.customer_id
WHERE a.resolved_at IS NULL;

-- Riwayat incident 7 hari terakhir
SELECT c.name, a.alert_type, a.started_at, a.resolved_at,
       a.resolved_at - a.started_at AS durasi
FROM alerts a JOIN customers c ON c.id = a.customer_id
WHERE a.started_at > now() - interval '7 days'
ORDER BY a.started_at DESC;

-- Traffic rata-rata per 5 menit satu pelanggan (bahan grafik fase 2)
SELECT time_bucket('5 minutes', time) AS bucket,
       avg(in_bps) AS in_bps, avg(out_bps) AS out_bps
FROM traffic_samples
WHERE customer_id = 1 AND time > now() - interval '6 hours'
GROUP BY bucket ORDER BY bucket;
```

## Tuning

| Variabel | Default | Catatan |
|---|---|---|
| `POLL_INTERVAL` | 60 | Bisa 30s kalau device kuat; makin rapat makin cepat deteksi |
| `CONSECUTIVE_DOWN_SAMPLES` | 3 | 3 × 60s = deteksi ±3 menit. Turunkan = lebih sensitif, naikkan = lebih tahan false alarm |
| `threshold_bps` (per customer) | 1000 | Kolom di tabel `customers`, bisa beda tiap pelanggan |

## Catatan operasional

- Counter yang dipakai SNMP adalah **ifHCInOctets/ifHCOutOctets (64-bit)** —
  aman untuk link gigabit ke atas.
- Interface PPPoE **tidak** dipoll via SNMP karena ifIndex-nya berubah tiap
  reconnect; tracking dilakukan via username di `/ppp/active`.
- Kalau device tidak merespon sama sekali, collector **tidak menulis sample**
  untuk customer di device itu. Alerter menganggap data basi = tidak mengambil
  keputusan (mencegah alert massal palsu saat SNMP timeout sesaat).
  Deteksi device down menyeluruh bisa ditambahkan di fase berikutnya.
- Retensi data time-series: 90 hari (ubah di `db/init/01_schema.sql`).

## Roadmap

- **Fase 2:** Dashboard Django + grafik realtime (websocket) + status board.
- **Fase 3:** Baseline anomaly detection, eskalasi, laporan SLA/uptime.

## Preflight check

Sebelum `docker compose up`, verifikasi environment & reachability device:

```bash
./scripts/preflight.sh                          # Docker + .env
./scripts/preflight.sh snmp 10.10.10.1 publicRO # test SNMP + list ifIndex
./scripts/preflight.sh api  10.10.10.2          # test port API Mikrotik
./scripts/preflight.sh telegram                 # test kirim pesan ke grup NOC
```

Mode `snmp` sekaligus menampilkan daftar ifIndex device — pakai ini untuk
mengisi kolom `if_index` di tabel `customers`.

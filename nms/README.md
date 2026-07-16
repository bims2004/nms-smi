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

- **Fase 2:** Dashboard Django — selesai, lihat bagian di bawah.
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

---

# Fase 2 — Dashboard web

Django dashboard untuk mengelola inventory lewat form (bukan SQL manual) dan
memantau status pelanggan secara langsung.

Dashboard memakai tabel yang **sama persis** dengan collector/alerter. Semua
model Django dibuat `managed = False`, jadi `migrate` tidak pernah mengubah
tabel `devices`, `customers`, `traffic_samples`, atau `alerts`. Schema tetap
dimiliki `db/init/01_schema.sql`.

## Akses

```
http://<ip-server>:8000
```

Buat user login pertama:

```bash
docker compose exec web python manage.py createsuperuser
```

## Halaman

| Halaman | Isi |
|---|---|
| **Pelanggan** (`/`) | Status board semua pelanggan. LED hijau/merah, traffic in/out, sparkline 1 jam. Auto-refresh 20 detik |
| **Detail pelanggan** | Grafik traffic 1j/6j/24j/7h, uptime, konfigurasi, riwayat gangguan |
| **Gangguan** (`/gangguan/`) | Daftar gangguan berlangsung & riwayat, termasuk status kirim Telegram |
| **Kelola** (`/admin/`) | Form CRUD perangkat & pelanggan |
| **Lihat interface** | Baca daftar interface/sesi PPPoE langsung dari perangkat |

## Alur mendaftarkan pelanggan

1. **Kelola → Perangkat → Tambah** — isi nama, IP management, vendor, metode
   polling. SNMP butuh community; Mikrotik API butuh user read-only.
2. **Perangkat → Lihat interface → Ambil sekarang** — server membaca IF-MIB
   (atau `/ppp/active` untuk Mikrotik) dan menampilkan ifIndex tiap port.
   Ini menggantikan `snmpwalk` manual.
3. Klik **Daftarkan** di baris yang sesuai — form pelanggan terbuka dengan
   perangkat, tipe, dan ifIndex sudah terisi.
4. Lengkapi nama pelanggan, ID layanan, ambang traffic. Simpan.
5. Collector mengambil sampel pada siklus berikutnya (default 60 detik).

Form menolak kombinasi yang tidak valid, misalnya monitoring PPPoE yang
diarahkan ke perangkat SNMP, atau interface fisik tanpa ifIndex.

## Arti warna LED

| LED | Keadaan |
|---|---|
| Hijau | Traffic normal di atas ambang |
| Merah | Down — link/sesi mati atau traffic di bawah ambang |
| Oranye | Tidak ada sampel 15 menit terakhir (collector atau perangkat bermasalah) |
| Abu | Belum ada keputusan, atau pelanggan dinonaktifkan |

Oranye berbeda dari merah: itu berarti NMS-nya yang tidak dapat data, bukan
pelanggannya yang pasti down. Cek collector dan reachability perangkat dulu.

## Keamanan

- `DJANGO_SECRET_KEY` wajib diganti: `openssl rand -base64 48`
- `DJANGO_DEBUG=0` di produksi (default sudah 0)
- Isi `DJANGO_ALLOWED_HOSTS` dengan IP/hostname server, jangan biarkan `*`
- Password API Mikrotik tersimpan plaintext di kolom `api_password` — sama
  seperti sebelumnya di SQL. Batasi akses database dan pakai user API
  read-only, jangan admin
- Kalau ditaruh di belakang reverse proxy, set `WEB_BIND=127.0.0.1` dan isi
  `DJANGO_CSRF_TRUSTED_ORIGINS`

## Catatan teknis

- Grafik dan sparkline dirender sebagai SVG di server — tidak ada library
  chart di browser, jadi tetap jalan di jaringan management yang terisolasi
- Query time-series memakai `time_bucket()` dari TimescaleDB
- Halaman "Lihat interface" melakukan SNMP walk dari container `web`, jadi ACL
  perangkat harus mengizinkan IP server yang sama seperti collector

---

# Fase 3 — Kualitas alert & laporan SLA

Empat penambahan, semuanya menempel di struktur yang sudah ada.

## 1. Perangkat ikut dipantau

Sebelumnya, kalau satu switch mati, NMS tidak mengirim alert apa pun —
pelanggannya hanya berubah jadi "tanpa data". Sekarang collector mencatat
kesehatan tiap perangkat, dan:

- Perangkat yang gagal di-poll `DEVICE_FAIL_THRESHOLD` kali berturut-turut
  menghasilkan **satu** alert `device_down`.
- Selama perangkat down, status pelanggan di bawahnya **berhenti dinilai**.
  Satu switch mati tidak lagi terlihat seperti ratusan pelanggan down.
- Saat perangkat pulih, evaluasi pelanggan berjalan lagi seperti biasa.

Lihat di halaman **Perangkat**.

## 2. Jadwal pemeliharaan

Kelola → Jadwal pemeliharaan → Tambah. Selama jendela aktif:

- Gangguan **tetap dicatat** (jadi riwayatnya tidak hilang),
- tapi **tidak dikirim** ke Telegram,
- dan ditandai `suppressed` sehingga bisa dikecualikan dari laporan SLA.

Cakupan bisa dibatasi ke satu perangkat, satu pelanggan, atau dikosongkan
keduanya untuk menahan alert semua pelanggan.

## 3. Deteksi degradasi (baseline)

Aturan Fase 1 hanya menangkap traffic yang benar-benar nol. Pelanggan yang
biasanya 300 Mbps lalu turun jadi 20 Mbps tetap dianggap normal — padahal
itu gangguan nyata.

Aktifkan per pelanggan: Kelola → Pelanggan → Deteksi degradasi.

Cara kerjanya: median traffic tiap kombinasi (hari, jam) waktu lokal selama
28 hari terakhir jadi baseline. Traffic malam Minggu tidak dibandingkan
dengan Senin pagi. Kalau traffic turun lebih dari `baseline_drop_pct` di
bawah baseline jam tersebut, terbit alert `traffic_degraded` dengan severity
**minor** (tidak mengurangi uptime SLA).

Batasannya, dan ini penting:

- Butuh riwayat minimal beberapa minggu sebelum baseline bisa dipercaya
  (minimal 20 sampel per kombinasi hari-jam).
- Baseline di bawah 1 Mbps diabaikan — pelanggan yang memang sepi tidak
  akan memicu alert.
- **Kurang cocok untuk pelanggan rumahan** yang pola pemakaiannya acak.
  Paling berguna untuk dedicated/korporat yang polanya berulang.

Baseline dihitung ulang tiap `BASELINE_REFRESH_HOURS` jam.

## 4. Eskalasi & penandaan penanganan

Gangguan **major** yang belum ditandai ditangani dan belum pulih setelah
`ESCALATION_MINUTES` akan dikirim ulang ke grup sebagai pengingat, sekali saja.

Tombol **Tandai ditangani** di halaman Gangguan menghentikan pengingat itu.
Gangguannya sendiri tetap terbuka sampai traffic benar-benar pulih — ack
hanya berarti "sudah ada yang pegang", bukan "sudah beres".

Isi `ESCALATION_MINUTES=0` untuk mematikan fitur ini.

## 5. Laporan SLA

Halaman **SLA**: pilih bulan, lihat uptime tiap pelanggan, unduh CSV
(siap dibuka di Excel, sudah ber-BOM UTF-8).

Cara hitung:

- Hanya gangguan **major** yang mengurangi uptime. Degradasi tidak.
- Gangguan yang tumpang tindih dihitung sekali, tidak dobel.
- Gangguan saat pemeliharaan terjadwal dikecualikan secara default; ada
  kotak centang untuk memasukkannya.
- Bulan berjalan dihitung sampai saat ini, bukan sampai akhir bulan —
  supaya uptime bulan ini tidak selalu terlihat bagus.

Angka ini dihitung dari data polling NMS sendiri. Kalau NMS sempat mati,
periode itu terhitung sebagai "tidak ada gangguan". Untuk klaim SLA yang
mengikat kontrak, cocokkan dulu dengan catatan tiket gangguan.

## Menerapkan Fase 3 ke instalasi yang sudah jalan

File di `db/init/` hanya dijalankan Postgres saat volume database masih
kosong. Untuk database yang sudah berisi data, jalankan:

```bash
git pull
./scripts/upgrade-db.sh        # backup otomatis, lalu terapkan perubahan schema
docker compose up -d --build
```

Script itu membuat backup terkompresi dulu sebelum mengubah apa pun, dan
semua migrasi ditulis idempotent — aman dijalankan berulang.

Instalasi baru tidak perlu langkah ini; `docker compose up -d --build`
sudah cukup.

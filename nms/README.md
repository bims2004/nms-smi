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

---

# Fase 4 — Grafik riwayat & pemakaian

## Yang sebenarnya sudah ada sejak Fase 1

Data traffic **sudah tersimpan otomatis** di `traffic_samples` tiap 60 detik
sejak collector pertama kali jalan. Yang belum ada sebelumnya cuma cara
melihatnya: grafik lama hanya menampilkan beberapa jam terakhir.

## Pemilih periode

Di halaman detail pelanggan sekarang ada:

- **Rentang cepat**: 1 jam, 6 jam, 24 jam, 7 hari, 30 hari, 90 hari.
- **Pemilih tanggal**: pilih tanggal, lihat pemakaian sehari penuh
  (00:00–23:59 waktu lokal), lengkap dengan tombol maju-mundur hari.

Ukuran bucket menyesuaikan otomatis: 1 menit untuk rentang pendek sampai
6 jam untuk 90 hari. Tanpa ini, 30 hari data per menit = 43.200 titik yang
tidak mungkin digambar.

## Angka pemakaian

| Angka | Arti |
|---|---|
| **Volume total** | Data terpakai dalam periode, pecah jadi download & upload |
| **Rata-rata** | Rerata bps sepanjang periode |
| **Puncak** | Nilai tertinggi yang pernah tersentuh |
| **95th percentile** | Nilai yang dilampaui hanya 5% waktu — dasar penagihan yang lazim di ISP |

Cara volume dihitung, dan kenapa begitu:

Volume dihitung dari **jarak waktu tiap sampel ke sampel sebelumnya**, bukan
dari lebar bucket grafik. Kalau pakai lebar bucket, bucket di tepi periode
yang cuma terisi sebagian akan dihitung penuh dan volumenya menggelembung —
versi pertama fitur ini punya bug persis itu, 30 hari terbaca 3x lipat dari
yang sebenarnya.

Jeda data lebih dari 10 menit **tidak dihitung**. Kalau perangkat mati
semalam, kita memang tidak tahu berapa traffic yang lewat, jadi lebih baik
tidak mengarang. Konsekuensinya: volume pelanggan yang sering down akan
lebih rendah dari kenyataan, bukan lebih tinggi.

95th percentile dihitung dari rata-rata 5 menit, mengambil nilai in/out yang
lebih besar tiap interval — cara yang lazim dipakai transit provider. Hanya
bisa dihitung dari sampel mentah, jadi terbatas 90 hari terakhir.

## Riwayat lebih dari 90 hari

`traffic_samples` dihapus otomatis setelah 90 hari. Menyimpan sampel per
menit selamanya terlalu boros: 1 pelanggan = 525.600 baris per tahun.

`db/init/03_rollup.sql` membuat **continuous aggregate** `traffic_hourly` —
rollup per jam yang diperbarui sendiri oleh TimescaleDB di latar belakang
(tidak ada cron yang perlu diurus). Ukurannya sekitar 60x lebih hemat, dan
disimpan 2 tahun.

Dashboard memilih sumbernya otomatis: rentang di bawah 60 hari dibaca dari
sampel mentah, di atas itu dari rollup. Grafik yang memakai rollup diberi
tanda "dari rollup jam-an".

Untuk mengisi rollup dengan data yang sudah terkumpul:

```sql
CALL refresh_continuous_aggregate('traffic_hourly', NULL, NULL);
```

**Yang belum teruji:** pembuatan continuous aggregate-nya sendiri belum bisa
saya uji, karena lingkungan tes saya tidak punya TimescaleDB — yang teruji
baru jalur query-nya (terhadap tabel tiruan berbentuk sama) dan penanganan
saat TimescaleDB tidak ada. Verifikasi di server kamu setelah migrasi:

```bash
docker compose exec db psql -U nms -d nms -c "\d+ traffic_hourly"
docker compose exec db psql -U nms -d nms \
  -c "SELECT view_name, materialization_hypertable_name
      FROM timescaledb_information.continuous_aggregates;"
```

Kalau `traffic_hourly` tidak terbentuk, dashboard tetap jalan normal —
fungsi `rollup_available()` mendeteksinya dan jatuh ke sampel mentah,
hanya saja riwayat terbatas 90 hari.

---

# Diagnosa

Kalau ada yang tidak beres — status pelanggan tidak berubah dari
"Belum diketahui", dashboard tidak bisa dibuka, alert tidak sampai —
jalankan ini dulu sebelum membaca log mentah:

```bash
./scripts/diagnose.sh
```

Script ini menelusuri seluruh rantai dari perangkat sampai dashboard dan
menunjuk mata rantai mana yang putus, lengkap dengan perintah perbaikannya.
Yang diperiksa:

| Lapis | Isi pemeriksaan |
|---|---|
| Berkas | `.env` ada, `POSTGRES_PASSWORD` & `DJANGO_SECRET_KEY` terisi |
| Container | db / collector / alerter / web berjalan, bukan restart-loop |
| Dashboard | benar-benar menjawab di port-nya; mengingatkan URL harus pakai port; cek ufw |
| Database | TimescaleDB aktif, `traffic_samples` benar hypertable, rollup ada, schema Fase 3 sudah diterapkan |
| Perangkat | **probe langsung dari dalam container collector** — jalur jaringan yang sama persis dengan proses polling |
| ifIndex | walk `ifName` ke perangkat, cocokkan dengan ifIndex yang terdaftar |
| Aliran data | jumlah sampel, umur sampel terakhir, laju sampel vs harapan |
| Pelanggan | umur sampel & bps terakhir per pelanggan, status, kewajaran ambang |
| Alert | notifikasi yang gagal terkirim |

Dua pemeriksaan yang paling sering menyelamatkan waktu:

**Probe dari dalam container.** SNMP yang jalan dari laptop kamu belum tentu
jalan dari container — ACL di perangkat mengizinkan IP mana? Script ini
mengetes dari tempat yang sebenarnya melakukan polling.

**Verifikasi ifIndex.** Ini penyebab paling sering pelanggan SNMP tidak punya
data. Kalau ifIndex yang terdaftar tidak ada di perangkat, script langsung
menyebutkan ifIndex apa saja yang tersedia beserta namanya. Kalau ifIndex-nya
ada tapi namanya sudah berbeda dari yang terdaftar, itu juga diperingatkan —
ifIndex bisa bergeser setelah reboot atau perubahan modul, dan datanya diam-diam
jadi milik interface lain.

Keluar dengan status 1 kalau ada masalah, jadi bisa dipakai di cron atau CI.

Diagnosa mendalamnya juga bisa dipanggil langsung:

```bash
docker compose exec collector python -m nms.diagnose
```

---

# Memperkecil penyimpanan

## Ukuran sebenarnya

Terukur dari schema ini: satu baris `traffic_samples` memakan **73 byte** heap,
ditambah indeks sekitar 27% dari total.

| Pelanggan | 90 hari | Tanpa kompresi | Dipadatkan (perkiraan 8x) |
|---|---|---|---|
| 1 | 129.600 baris | 12 MB | ~1,5 MB |
| 50 | 6,5 juta baris | 600 MB | ~75 MB |
| 500 | 65 juta baris | 6 GB | ~750 MB |

**Terus terang: di skala kamu sekarang (1 pelanggan) ini belum ada gunanya.**
90 hari data cuma 12 MB. Kompresi menghemat sekitar 10 MB — tidak berarti apa-apa.

Yang membuatnya tetap layak dipasang sekarang: mengaktifkan kompresi belakangan
berarti harus memadatkan ulang semua chunk yang sudah terlanjur menumpuk. Lebih
murah dipasang selagi datanya masih sedikit. Anggap ini asuransi untuk saat
pelanggan bertambah, bukan perbaikan masalah hari ini.

## Kompresi kolom TimescaleDB

`db/init/04_kompresi.sql` mengaktifkan kompresi kolom. Data time-series adalah
kasus terbaiknya: nilai berurutan saling mirip, jadi yang disimpan cukup
selisihnya.

```sql
ALTER TABLE traffic_samples SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'customer_id',
    timescaledb.compress_orderby   = 'time DESC'
);
SELECT add_compression_policy('traffic_samples', INTERVAL '7 days');
```

Dua keputusan di situ, dan alasannya:

**Ambang 7 hari, bukan lebih pendek.** Refresh continuous aggregate menyentuh
data 3 hari ke belakang (lihat `03_rollup.sql`). Kalau kompresi mengejar lebih
cepat dari itu, rollup akan terus-menerus membongkar chunk yang baru saja
dipadatkan — boros CPU, hasilnya malah lebih lambat. Jarak 7 vs 3 hari memberi
jeda yang aman.

**segmentby customer_id.** Ini pilihan yang bisa menjadi bumerang. Kalau tiap
segmen cuma berisi belasan baris, overhead per segmen lebih besar dari yang
dihemat dan ukuran data justru **membengkak** — ini kejadian nyata yang
dilaporkan orang. Hitungannya untuk kita: chunk default 7 hari, polling 60
detik, jadi **10.080 baris per pelanggan per chunk**. Angka itu sama berapa pun
jumlah pelanggan, dan jauh di atas ambang bahaya. Aman.

`./scripts/diagnose.sh` memverifikasi keduanya secara empiris: melaporkan rasio
kompresi nyata, memperingatkan kalau rasionya di bawah 3x, dan menjerit kalau
di bawah 1x (artinya malah membesar).

## Memadatkan data lama sekarang juga

Policy hanya memadatkan chunk baru secara bertahap. Untuk yang sudah ada:

```bash
docker compose exec db psql -U nms -d nms -c \
  "SELECT compress_chunk(c, if_not_compressed => true)
   FROM show_chunks('traffic_samples', older_than => INTERVAL '7 days') c;"
```

Butuh ruang disk kosong sementara. Untuk tabel besar, jalankan bertahap.

## Kalau masih kurang

**Perpendek retensi data mentah.** Sekarang 90 hari. Rollup jam-an sudah
menyimpan riwayat 2 tahun, jadi data mentah hanya benar-benar dibutuhkan untuk
95th percentile — yang siklusnya bulanan. 45 hari sudah cukup dan memangkas
setengah:

```sql
SELECT remove_retention_policy('traffic_samples');
SELECT add_retention_policy('traffic_samples', INTERVAL '45 days');
```

**Perlambat polling.** 60 detik ke 120 detik memangkas separuh, langsung.
Harganya: gangguan terdeteksi 2x lebih lambat. Untuk pelanggan dedicated
biasanya tidak sepadan.

## Yang saya temukan tapi belum saya ubah

`in_octets` dan `out_octets` **ditulis ke database tapi tidak pernah dibaca
oleh apa pun** — collector menghitung bps dari cache di memorinya sendiri.
Secara ukuran, keduanya sekitar 20 byte per baris.

Saya biarkan karena dua alasan: nilainya berguna saat mendiagnosa counter yang
wrap atau reset, dan justru counter yang naik nyaris linier seperti ini adalah
kasus paling ideal untuk kompresi — setelah dipadatkan, harganya mendekati nol.
Menghapusnya sekarang berarti membuang alat diagnosa demi hemat yang sudah
diberikan kompresi secara gratis.

Catatan jujur: keduanya bertipe `NUMERIC`, bukan `BIGINT`. Saya pilih NUMERIC di
Fase 1 karena counter SNMP 64-bit *unsigned* bisa mencapai 1,8e19, melebihi
batas BIGINT (9,2e18). Uncompressed, keduanya sama persis besarnya — sudah saya
ukur, selisihnya 2 byte dan ditelan padding. Setelah dikompresi, BIGINT
kemungkinan lebih hemat karena bisa memakai delta encoding, tapi **saya tidak
bisa mengukurnya** dan tidak mau menebak. Kalau nanti terbukti jadi masalah,
mengubah tipe kolom pada hypertable berisi data adalah operasi berat, jadi jangan
dilakukan tanpa alasan terukur.

## Yang belum teruji

Sama seperti rollup: **kompresinya sendiri belum bisa saya uji** karena
lingkungan tes saya tidak punya TimescaleDB. Yang sudah teruji: migrasinya
idempotent dan lewat dengan aman kalau TimescaleDB tidak ada, nama-nama fungsi
statistiknya sudah saya cocokkan dengan dokumentasi resmi, dan jalur fallback
di diagnosa berjalan. Angka 8x di tabel atas adalah **perkiraan**, bukan hasil
ukur — data seperti ini umumnya 5-15x, tapi yang berlaku adalah angka yang
keluar di servermu.

Verifikasi setelah migrasi:

```bash
./scripts/diagnose.sh          # lihat bagian Penyimpanan
```

---

# Alarm suara di dashboard

Dashboard bisa membunyikan alarm saat ada gangguan **baru**. Berguna kalau
dashboard dipasang di layar dinding NOC dan tidak ada yang memelototinya
terus-menerus.

Tombol **Alarm** ada di kepala halaman Status pelanggan.

## Suaranya disintesis, bukan file

Semua suara dibangkitkan lewat Web Audio API — tidak ada file audio, tidak ada
request keluar. Dua alasan:

1. Dashboard ini harus tetap jalan di jaringan management yang terisolasi.
   Ini konsisten dengan keputusan sejak Fase 2: grafik pun dirender di server,
   tanpa library dari CDN.
2. Sampel suara meme yang beredar (vine boom, airhorn, dan sejenisnya) adalah
   materi berhak cipta. Saya tidak membundelnya.

Pilihan yang tersedia:

| Suara | Bentuk | Untuk |
|---|---|---|
| **Kaget** | hentakan tajam lalu jatuh ke bass ~38 Hz | bikin kaget, default untuk major |
| **Sirene** | dua nada bergantian, 4 siklus | alarm ruang kontrol klasik |
| **Bip** | tiga nada pendek | buat yang tidak mau kaget |
| **Pulih** | dua nada naik | otomatis saat gangguan hilang |

"Pulih" sengaja dibuat berbeda jauh dari suara gangguan — supaya tidak perlu
melihat layar untuk tahu itu kabar baik.

## Kalau mau pakai file sendiri

Taruh file di `web/monitor/static/monitor/alarm/`, lalu panggil dari console
browser atau tambahkan ke `_alarm.html`:

```js
NmsAlarm.setCustom('/static/monitor/alarm/punya-saya.mp3');
```

Lalu tambahkan `<option value="custom">Punya sendiri</option>` ke `#alarmSound`.

Lisensi file yang kamu pasang jadi tanggung jawabmu — kalau dashboard ini cuma
dipakai internal NOC, risikonya kecil, tapi itu keputusanmu, bukan saya.

## Pengaturan

Tersimpan di browser (localStorage), per-perangkat. Operator di layar dinding
bisa menyalakan alarm tanpa memaksa semua orang mendengarnya.

- **Suara** — pilihan untuk gangguan major
- **Volume**
- **minor** — gangguan degradasi ikut berbunyi (default: mati)
- **pulih** — bunyi saat pelanggan kembali normal (default: nyala)
- **Tes** — coba suaranya

## Yang perlu diketahui sebelum dipakai serius

**Alarm ini pelengkap Telegram, bukan pengganti.** Dia hanya berbunyi kalau ada
browser yang membuka dashboard. Browser ditutup, laptop tidur, atau operator
pulang — alarm mati total. Telegram tetap jalur yang bisa diandalkan.

**Tab latar belakang membuat alarm terlambat.** Browser memperlambat timer di
tab yang tidak aktif, kadang sampai sekali per menit. Dashboard menyegarkan
tiap 20 detik saat tab aktif, jadi kalau tabnya tersembunyi, alarm bisa telat
sampai satu menit. Untuk layar dinding yang selalu tampak, ini tidak masalah.

**Browser menolak memutar suara sebelum ada klik.** Ini aturan browser, bukan
bug yang bisa diakali — makanya alarm harus dinyalakan manual sekali tiap
membuka halaman baru. Karena itu tombolnya sekalian membunyikan suara sebagai
konfirmasi: kalau kamu mendengarnya, alarm benar-benar siap.

**Default hanya major, dan itu disengaja.** Alarm yang terlalu sering berbunyi
akan dimatikan orang — dan alarm yang dimatikan lebih buruk daripada tidak ada
alarm, karena memberi rasa aman yang palsu. Degradasi (minor) sengaja senyap
kecuali kamu meminta.

**Alarm hanya untuk gangguan baru.** Membuka dashboard saat sudah ada 30
gangguan lama tidak akan membunyikan 30 alarm sekaligus. Gangguan yang sedang
dalam jendela pemeliharaan juga tidak pernah membunyikan alarm.

---

# Grafik realtime

Tombol **Live** di deretan pemilih periode. Nyala secara default untuk rentang
pendek — **15 menit, 1 jam, 6 jam, 24 jam** — dan bisa dimatikan. Pilihannya
diingat browser.

Saat nyala, titiknya berdenyut hijau dan label kanan menampilkan jam sampel
terakhir yang masuk. Kalau server bermasalah, tombolnya berubah merah.

Rentang **15 menit** ditambahkan khusus untuk memelototi traffic saat
troubleshooting.

Untuk rentang 7 hari ke atas dan mode tanggal, tombolnya **disembunyikan**,
bukan ditampilkan dalam keadaan mati — tombol yang tidak bisa diklik cuma
bikin bingung.

## Batasan yang tidak bisa diakali

**Datanya tetap datang tiap 60 detik.** Collector mengambil sampel sesuai
`POLL_INTERVAL`. Grafik menyegarkan tiap 30 detik supaya sampel baru cepat
terlihat, tapi menyegarkan lebih cepat dari itu tidak akan memunculkan apa pun
— datanya memang belum ada. Kalau butuh lebih halus, yang diturunkan adalah
`POLL_INTERVAL`, bukan interval refresh — dan itu menambah beban ke perangkat
yang di-poll.

## Kenapa cuma rentang pendek

Rentang 7 hari ke atas tidak ikut menyegarkan diri. Satu titik di grafik 90
hari mewakili 6 jam; menyegarkannya tiap setengah menit membebani database
tanpa ada yang bisa terlihat berubah. Mode tanggal juga tidak — data masa lalu
tidak berubah.

## Cara kerjanya

Grafik tetap **dirender di server**. Endpoint `/pelanggan/<id>/live/` memakai
ulang `parse_period`, `fetch_series`, `usage_stats`, dan `build_chart` yang
sama persis dengan halaman utamanya, lalu merender partial yang sama
(`_chart.html`, `_stats.html`). JavaScript-nya cuma menukar isi kotak.

Alasannya: kalau logika penggambaran diduplikasi di JavaScript, cepat atau
lambat grafik live akan berbeda dari grafik hasil reload, dan selisih semacam
itu susah dilacak. Pengujian memastikan SVG dari endpoint live identik dengan
SVG di halaman.

Efek sampingnya juga bagus: tidak ada library chart dari CDN, konsisten dengan
keputusan sejak Fase 2 — dashboard tetap jalan di jaringan management yang
terisolasi.

Kalau server bermasalah, setelah 3 kegagalan berturut-turut interval melambat
jadi 2 menit supaya tidak terus menghantam server yang sedang susah. Titiknya
berubah merah. Saat tab dibuka kembali, grafik langsung disegarkan — browser
memperlambat timer di tab tersembunyi, jadi tanpa ini yang terlihat pertama
kali adalah data basi.

## Catatan: bug komentar template

Versi pertama fitur ini mencetak komentar template mentah ke halaman —
`{# Dipakai dua tempat: ... #}` muncul sebagai teks di atas panel statistik
dan grafik.

Sebabnya: sintaks `{# ... #}` Django **hanya berlaku untuk satu baris**.
Komentar yang membentang dua baris tidak dianggap komentar sama sekali.
Yang multi-baris harus `{% comment %}...{% endcomment %}`.

Kenapa lolos: pengujian saat itu memeriksa isi JSON, kesamaan SVG, dan status
HTTP — semuanya lulus, karena teks liar tidak merusak satu pun dari itu. Tidak
ada yang melihat halamannya.

Penjaganya sekarang ada di `web/monitor/tests.py`:

```bash
docker compose exec web python manage.py test monitor
```

Uji itu memindai semua template dan menolak komentar `{# #}` multi-baris, plus
memastikan semua template bisa di-parse. Sudah dibuktikan menangkap bug yang
sengaja disisipkan.

---

# Ketahanan, skala, dan pengamanan

## Arah port in/out

`ifInOctets` bermakna dari sudut pandang **perangkat**, bukan pelanggan. Di
port yang menghadap pelanggan, "in" = masuk ke port = datang dari pelanggan =
**upload** pelanggan. Versi sebelumnya melabelinya "Download (in)" — terbalik.

Sekarang ada setelan **Arah port** per pelanggan (Kelola → Pelanggan):

| Setelan | Download | Upload |
|---|---|---|
| Port menghadap pelanggan (default) | ifOut | ifIn |
| Port menghadap upstream/uplink | ifIn | ifOut |

PPPoE tidak terpengaruh — poller Mikrotik sudah memetakan tx/rx ke sudut
pandang pelanggan sejak awal.

**Counter di database tetap mentah** (`in_bps` = ifIn, selalu). Penerjemahan
hanya terjadi saat menampilkan, di satu fungsi (`flip_series`). Kalau in/out
ditukar saat menyimpan, data lama tidak bisa ditafsirkan lagi begitu setelan
berubah. Kalau penukaran disebar ke banyak tempat, cepat atau lambat ada yang
tertukar dua kali — dan salah semacam itu tidak kelihatan: angkanya tetap
masuk akal, cuma terbalik.

Periksa data lama setelah upgrade: kalau grafik pelanggan menunjukkan upload
jauh lebih besar dari download, kemungkinan arahnya perlu diganti.

## Polling paralel

Perangkat sekarang di-poll bersamaan (`POLL_WORKERS`, default 8).

Kenapa perlu, dengan angka: perangkat yang mati memblokir selama
`SNMP_TIMEOUT × (retries+1)` = 10 detik. Berurutan, **6 perangkat mati
sekaligus** sudah membuat siklus meleset dari `POLL_INTERVAL` 60 detik. Yang
ironis: sistemnya paling lambat justru saat keadaan paling gawat — perangkat
sehat menjawab dalam milidetik.

`./scripts/diagnose.sh` menghitungnya untuk jumlah perangkatmu dan memberi
tahu kalau `POLL_WORKERS` perlu dinaikkan.

Koneksi database tetap dipegang satu thread. psycopg2 tidak aman dipakai
beberapa thread sekaligus; thread hanya menangani I/O jaringan, penulisan
dikembalikan ke thread utama.

## Dead man's switch

NMS yang mati tidak bisa memberi tahu bahwa dirinya mati. Lubang ini tidak
bisa ditambal dari dalam — yang bisa hanya berdetak keluar.

Isi `HEARTBEAT_URL` di `.env` dengan URL ping dari healthchecks.io, Uptime
Kuma push monitor, atau cronitor. Collector menyentuhnya tiap siklus; kalau
berhenti, layanan di seberang yang berteriak.

## Laporan SLA sekarang jujur

Collector mencatat detaknya ke tabel `nms_heartbeat`. Halaman SLA memeriksa
jeda detak dan **memberi peringatan merah** kalau NMS sendiri sempat buta:

> NMS sendiri tidak mencatat selama 3j 12m (4,4% dari periode), dalam 2 jeda.
> Selama itu gangguan pelanggan tidak terdeteksi dan tidak tercatat, jadi
> angka uptime di bawah lebih tinggi dari kenyataan.

Peringatan yang sama ikut ke ekspor CSV. Angka yang menyesatkan tidak boleh
lebih mudah disebar daripada peringatannya.

Ini menambal cacat yang saya tulis sendiri di bagian SLA sebelumnya: tanpa
pencatatan detak, semalam NMS mati terbaca sebagai "tidak ada gangguan" alias
uptime 100% — bohong yang arahnya kebetulan menguntungkan kita sendiri.

Periode sebelum pembaruan ini tidak punya catatan detak, jadi tidak bisa
diperiksa. Halaman SLA mengatakannya terus terang, bukan berpura-pura tahu.

## Backup

```bash
./scripts/backup.sh              # ke ./backups
./scripts/backup.sh /mnt/nas     # ke folder lain
```

Otomatis tiap hari:

```bash
crontab -e
0 2 * * * cd /home/bimma/nms-smi && ./scripts/backup.sh >> backup.log 2>&1
```

Backup yang tidak pernah diperiksa bukan backup, cuma perasaan aman. Script
ini memverifikasi tiap hasilnya: uji integritas gzip (menangkap file terpotong
karena disk penuh), hitung blok data, dan pastikan tabel `alerts`, `customers`,
`devices` benar-benar ada di dalamnya. Kalau gagal, filenya dihapus dan exit
code 1 — supaya cron mengirim email, bukan diam-diam menyimpan file rusak.

Backup lebih tua dari `BACKUP_KEEP_DAYS` (default 30) dibuang otomatis.

Script memperingatkan kalau backup disimpan di server yang sama dengan
databasenya — kalau disknya mati, backup ikut hilang, justru saat paling
dibutuhkan.

Pulihkan:

```bash
./scripts/restore.sh backups/nms-20260717-020000.sql.gz
```

Restore menampilkan isi database sekarang, minta ketik `PULIHKAN`, membuat
jaring pengaman dari keadaan sekarang, lalu menghentikan collector & alerter
supaya tidak menulis di tengah proses.

**Yang paling tidak tergantikan adalah riwayat gangguan.** Data traffic bisa
dikumpulkan lagi mulai besok; riwayat gangguan tidak bisa — dan itu yang
dikirim ke pelanggan.

## Impor CSV

Dashboard → Impor CSV. Unggah berkas atau tempel isinya.

**Semua berhasil, atau tidak sama sekali.** Satu baris rusak = nol pelanggan
masuk. Impor setengah jalan meninggalkan keadaan yang susah dirapikan: orang
tidak tahu mana yang sudah masuk dan mana yang belum.

Selalu ada pratinjau sebelum menyimpan. Validasinya memakai `full_clean()`
yang sama persis dengan form biasa — tidak ada jalan pintas yang melewati
aturan.

Perhatikan kolom `titik`: untuk `snmp_if` isinya **ifIndex berupa angka**,
bukan nama interface. Keduanya sering berbeda. Kalau tidak yakin, buka
Perangkat → Lihat interface.

## HTTPS

```bash
docker compose --profile https up -d
```

Lalu di `.env`: `HTTPS_ENABLED=1`, `NMS_HOST=<ip-server>`, dan
`WEB_BIND=127.0.0.1` supaya port 8000 tidak lagi bisa diakses tanpa TLS.

Caddy memakai sertifikat terbitan sendiri, karena jaringan management biasanya
tidak punya DNS publik — dan tanpa itu Let's Encrypt tidak bisa memverifikasi
apa pun. Browser akan memperingatkan pada kunjungan pertama. Itu jujur:
sertifikatnya memang tidak dijamin pihak ketiga. Yang tetap didapat: password
login dan session tidak lagi lewat jaringan sebagai teks polos.

Punya domain publik? Ganti blok di `Caddyfile` dengan nama domainnya dan hapus
`tls internal`; Caddy mengurus Let's Encrypt sendiri.

**Jangan setel `HTTPS_ENABLED=1` tanpa proxy TLS di depannya.** Cookie-secure
di server HTTP membuat login mustahil: browser mengirim cookienya, Django
menolaknya, dan orang terjebak di halaman login tanpa pesan error apa pun.

## Pengamanan lain

`settings.py` sekarang memasang `X_FRAME_OPTIONS=DENY`, `nosniff`, cookie
HttpOnly, SameSite=Lax, dan masa sesi 12 jam — dashboard NOC sering dibiarkan
terbuka di layar dinding; sesi abadi berarti siapa pun yang lewat bisa
memakainya.

Dengan `HTTPS_ENABLED=1`, `manage.py check --deploy` bersih kecuali dua
peringatan: HSTS preload dan include-subdomains. Keduanya sengaja dibiarkan —
preload nyaris tidak bisa dibatalkan dan tidak pantas dipilihkan diam-diam
oleh sebuah template.

`DJANGO_ALLOWED_HOSTS` tidak lagi mencontohkan `*`. Django memperingatkan
sendiri kalau `*` dipakai di mode produksi.

## Enkripsi kredensial perangkat

Password API Mikrotik disimpan terenkripsi (Fernet), bukan teks polos. Isi
`NMS_SECRET_KEY` di `.env`:

```bash
python3 -c "import secrets;print(secrets.token_urlsafe(48))"
```

Lalu buka & simpan ulang tiap perangkat lewat dashboard. Nilai lama tanpa
prefiks tetap terbaca, jadi tidak ada yang rusak selama peralihan.

**Yang dilindungi:** backup `.sql.gz` yang bocor. Dump sering disalin ke NAS
atau dikirim lewat chat — dan saya baru saja menyuruhmu melakukannya. Tanpa
enkripsi, satu file backup = password semua perangkat jaringan.

**Yang TIDAK dilindungi:** orang yang sudah bisa membaca `.env`. Kuncinya ada
di sana juga. Menaruh kunci di tempat lain di server yang sama cuma memindahkan
masalah sambil terlihat lebih aman.

**Yang tetap tidak tergantikan:** pakai user read-only di perangkat.

```
/user group add name=nms-ro policy=read,api
/user add name=nms group=nms-ro password=...
```

Kalau kredensial ini bocor, yang didapat penyerang hanyalah kemampuan membaca
counter interface — bukan mengubah konfigurasi. Itu perlindungan yang jauh
lebih berarti daripada enkripsi mana pun.

**Jangan mengganti `NMS_SECRET_KEY`** setelah ada password tersimpan. Yang
lama tidak akan bisa dibuka, dan sistem akan berhenti dengan pesan jelas
(bukan diam-diam gagal login berjam-jam).

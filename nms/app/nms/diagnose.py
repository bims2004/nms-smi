"""Diagnosa sistem yang sedang berjalan.

Bedanya dengan preflight.sh: preflight dijalankan SEBELUM deploy untuk cek
prasyarat. Ini dijalankan SETELAH deploy, untuk menjawab pertanyaan yang
paling sering muncul — "kenapa status pelanggan saya masih Belum diketahui?"

Alih-alih menyuruh orang membaca log mentah, script ini menelusuri rantainya
dari perangkat sampai dashboard dan menunjuk mata rantai mana yang putus.

Jalankan:  docker compose exec collector python -m nms.diagnose
"""
import sys
from datetime import datetime, timezone

from . import config, db

G, R, Y, B, N = "\033[32m", "\033[31m", "\033[33m", "\033[34m", "\033[0m"

problems = []


def ok(msg):
    print(f"  {G}OK{N}    {msg}")


def bad(msg, fix=None):
    print(f"  {R}MASALAH{N} {msg}")
    problems.append((msg, fix))


def warn(msg):
    print(f"  {Y}PERHATIAN{N} {msg}")


def info(msg):
    print(f"        {msg}")


def head(msg):
    print(f"\n{B}== {msg} =={N}")


def age_str(dt):
    if dt is None:
        return "belum pernah"
    secs = (datetime.now(timezone.utc) - dt).total_seconds()
    if secs < 90:
        return f"{int(secs)} detik lalu"
    if secs < 5400:
        return f"{int(secs / 60)} menit lalu"
    if secs < 172800:
        return f"{secs / 3600:.1f} jam lalu"
    return f"{secs / 86400:.1f} hari lalu"


# ------------------------------------------------------------------ database
def check_db(conn):
    head("Database")
    with conn.cursor() as cur:
        cur.execute("SELECT version()")
        info(cur.fetchone()[0].split(",")[0])

        cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")
        if cur.fetchone():
            ok("TimescaleDB terpasang")
            cur.execute("""
                SELECT count(*) FROM timescaledb_information.hypertables
                WHERE hypertable_name = 'traffic_samples'
            """)
            if cur.fetchone()[0]:
                ok("traffic_samples adalah hypertable")
            else:
                bad("traffic_samples BUKAN hypertable — retensi & performa "
                    "tidak akan jalan",
                    "Tabel dibuat sebelum ekstensi aktif. Paling bersih: "
                    "docker compose down -v lalu up lagi (data hilang).")

            cur.execute("""
                SELECT count(*) FROM timescaledb_information.continuous_aggregates
                WHERE view_name = 'traffic_hourly'
            """)
            if cur.fetchone()[0]:
                ok("Rollup traffic_hourly aktif (riwayat > 90 hari tersimpan)")
            else:
                warn("Rollup traffic_hourly belum ada — riwayat terbatas "
                     "90 hari. Jalankan ./scripts/upgrade-db.sh")
        else:
            bad("TimescaleDB TIDAK terpasang",
                "Pastikan image db di docker-compose.yml adalah "
                "timescale/timescaledb, bukan postgres biasa.")

    # Kolom Fase 3 & 4
    with conn.cursor() as cur:
        cur.execute("""
            SELECT count(*) FROM information_schema.columns
            WHERE table_name = 'devices' AND column_name = 'fail_count'
        """)
        if cur.fetchone()[0]:
            ok("Schema Fase 3 terpasang")
        else:
            bad("Schema Fase 3 BELUM diterapkan",
                "Jalankan: ./scripts/upgrade-db.sh")


# ----------------------------------------------------------------- inventory
def check_inventory(conn):
    head("Inventaris")
    with db.dict_cursor(conn) as cur:
        cur.execute("SELECT count(*) FILTER (WHERE enabled) AS n, count(*) AS t "
                    "FROM devices")
        d = cur.fetchone()
        cur.execute("SELECT count(*) FILTER (WHERE enabled) AS n, count(*) AS t "
                    "FROM customers")
        c = cur.fetchone()

    if d["t"] == 0:
        bad("Belum ada perangkat terdaftar",
            "Tambah lewat: Kelola -> Perangkat -> Tambah")
        return False
    ok(f"{d['n']} perangkat aktif (dari {d['t']})")

    if c["t"] == 0:
        bad("Belum ada pelanggan terdaftar",
            "Cara termudah: Perangkat -> Lihat interface -> Daftarkan. "
            "ifIndex akan terisi otomatis.")
        return False
    ok(f"{c['n']} pelanggan aktif (dari {c['t']})")
    return True


# ------------------------------------------------------------------ devices
def check_devices(conn):
    head("Kesehatan perangkat")
    from .pollers.probe import probe_device

    with db.dict_cursor(conn) as cur:
        cur.execute("SELECT * FROM devices WHERE enabled ORDER BY name")
        devices = cur.fetchall()

    reachable = {}
    for dv in devices:
        print(f"\n  {dv['name']} ({dv['ip']}) — {dv['poll_method']}")
        info(f"status tersimpan : {dv['status']}, gagal berturut: {dv['fail_count']}")
        info(f"terakhir merespon: {age_str(dv['last_ok_at'])}")

        try:
            alive = probe_device(dv)
        except Exception as e:
            alive = False
            info(f"probe error: {e}")

        reachable[dv["id"]] = alive
        if alive:
            ok("perangkat merespon sekarang")
        else:
            if dv["poll_method"] == "snmp":
                bad(f"{dv['name']} tidak merespon SNMP dari dalam container",
                    f"Urut cek: (1) community benar? (2) ACL SNMP di perangkat "
                    f"sudah izinkan IP server? (3) firewall UDP "
                    f"{dv['snmp_port'] or 161}? Tes manual: snmpwalk -v2c -c "
                    f"<community> {dv['ip']} 1.3.6.1.2.1.1.3.0")
            else:
                bad(f"{dv['name']} tidak merespon di port API "
                    f"{dv['api_port'] or 8728}",
                    f"Cek service API aktif: /ip service print. "
                    f"Cek user punya policy api+read.")
    return reachable


# ---------------------------------------------------------------- ifIndex
def check_ifindex(conn, reachable):
    """Penyebab paling sering pelanggan SNMP tidak ada data: ifIndex salah."""
    head("Verifikasi ifIndex pelanggan SNMP")

    # Kolom di-alias eksplisit. Kalau pakai d.*, kolom id/name/status milik
    # perangkat akan menimpa milik pelanggan dan diagnosanya jadi salah orang.
    with db.dict_cursor(conn) as cur:
        cur.execute("""
            SELECT c.id            AS cust_id,
                   c.name          AS cust_name,
                   c.if_index      AS if_index,
                   c.if_name       AS if_name,
                   d.id            AS dev_id,
                   d.name          AS dev_name,
                   d.ip            AS ip,
                   d.snmp_community AS snmp_community,
                   d.snmp_port     AS snmp_port
            FROM customers c JOIN devices d ON d.id = c.device_id
            WHERE c.enabled AND c.monitor_type = 'snmp_if'
            ORDER BY d.name, c.if_index
        """)
        rows = cur.fetchall()

    if not rows:
        info("Tidak ada pelanggan tipe SNMP interface.")
        return

    from .pollers.snmp import walk_if_names

    # Walk sekali per perangkat, bukan sekali per pelanggan
    cache = {}
    for r in rows:
        dev_id = r["dev_id"]
        if dev_id in cache:
            continue
        if not reachable.get(dev_id, False):
            cache[dev_id] = None  # perangkat mati, tidak usah dicoba
            continue
        try:
            cache[dev_id] = walk_if_names(r)
        except Exception as e:
            cache[dev_id] = None
            info(f"tidak bisa walk ifName di {r['dev_name']}: {e}")

    for r in rows:
        live = cache.get(r["dev_id"])
        if live is None:
            warn(f"{r['cust_name']}: tidak bisa diverifikasi "
                 f"({r['dev_name']} tidak merespon)")
            continue

        if r["if_index"] in live:
            actual = live[r["if_index"]]
            if r["if_name"] and actual and r["if_name"] != actual:
                warn(f"{r['cust_name']}: ifIndex {r['if_index']} sekarang "
                     f"bernama '{actual}', terdaftar sebagai '{r['if_name']}'. "
                     f"ifIndex bisa bergeser setelah reboot atau perubahan "
                     f"modul — datanya jadi milik interface lain.")
            else:
                ok(f"{r['cust_name']}: ifIndex {r['if_index']} = {actual}")
        else:
            sample = ", ".join(f"{i}={n}" for i, n in
                               list(sorted(live.items()))[:8])
            bad(f"{r['cust_name']}: ifIndex {r['if_index']} TIDAK ADA di "
                f"{r['dev_name']} — ini sebabnya tidak ada data",
                f"ifIndex yang tersedia: {sample}{' ...' if len(live) > 8 else ''}. "
                f"Perbaiki lewat Perangkat -> Lihat interface -> Daftarkan.")


# ------------------------------------------------------------------ samples
def check_samples(conn):
    head("Aliran data")
    with db.dict_cursor(conn) as cur:
        cur.execute("SELECT count(*) AS n, max(time) AS last FROM traffic_samples")
        s = cur.fetchone()

    if s["n"] == 0:
        bad("Belum ada satu pun sampel traffic tersimpan",
            "Collector belum pernah berhasil polling. Lihat bagian "
            "Kesehatan perangkat di atas.")
        return
    ok(f"{s['n']} sampel tersimpan, terbaru {age_str(s['last'])}")

    if (datetime.now(timezone.utc) - s["last"]).total_seconds() > 300:
        bad("Sampel terakhir sudah basi (lebih dari 5 menit)",
            "Collector berhenti atau perangkat tidak lagi merespon. "
            "Cek: docker compose logs collector --tail 30")

    # Laju sampel vs harapan
    with db.dict_cursor(conn) as cur:
        cur.execute("""
            SELECT count(*) AS got FROM traffic_samples
            WHERE time > now() - interval '5 minutes'
        """)
        got = cur.fetchone()["got"]
        cur.execute("SELECT count(*) AS n FROM customers WHERE enabled")
        n_cust = cur.fetchone()["n"]

    expect = n_cust * (300 // max(1, config.POLL_INTERVAL))
    if expect and got < expect * 0.5:
        bad(f"Sampel 5 menit terakhir: {got}, seharusnya sekitar {expect}",
            "Sebagian pelanggan tidak ter-polling. Lihat rincian per "
            "pelanggan di bawah.")
    elif expect:
        ok(f"Laju sampel wajar: {got} dalam 5 menit (harapan ~{expect})")


# ---------------------------------------------------------------- customers
def check_customers(conn):
    head("Status per pelanggan")
    with db.dict_cursor(conn) as cur:
        cur.execute("""
            SELECT c.id, c.name, c.status, c.threshold_bps, c.monitor_type,
                   c.if_index, c.pppoe_username, d.name AS dev,
                   s.time AS last_time, s.in_bps, s.out_bps, s.link_up
            FROM customers c
            JOIN devices d ON d.id = c.device_id
            LEFT JOIN LATERAL (
                SELECT time, in_bps, out_bps, link_up FROM traffic_samples
                WHERE customer_id = c.id ORDER BY time DESC LIMIT 1
            ) s ON TRUE
            WHERE c.enabled ORDER BY c.name
        """)
        rows = cur.fetchall()

    for r in rows:
        titik = (f"ifIndex {r['if_index']}" if r["monitor_type"] == "snmp_if"
                 else r["pppoe_username"])
        print(f"\n  {r['name']} — {r['dev']} / {titik}")

        if r["last_time"] is None:
            bad(f"{r['name']}: belum ada sampel sama sekali",
                "Titik monitornya tidak ter-polling. Kalau perangkatnya "
                "merespon, kemungkinan besar ifIndex/username salah.")
            continue

        info(f"sampel terakhir : {age_str(r['last_time'])}")
        if r["in_bps"] is None:
            info("bps             : belum bisa dihitung (baru 1 sampel)")
            warn(f"{r['name']}: status akan tetap 'Belum diketahui' sampai "
                 f"ada {config.CONSECUTIVE_DOWN_SAMPLES} sampel berturut-turut")
            continue

        total = r["in_bps"] + r["out_bps"]
        info(f"bps             : in {r['in_bps'] / 1e6:.2f}M / "
             f"out {r['out_bps'] / 1e6:.2f}M, link_up={r['link_up']}")
        info(f"ambang          : {r['threshold_bps'] / 1e6:.3f}M")
        info(f"status          : {r['status']}")

        if r["status"] == "unknown":
            secs = (datetime.now(timezone.utc) - r["last_time"]).total_seconds()
            if secs > config.POLL_INTERVAL * (config.CONSECUTIVE_DOWN_SAMPLES + 2):
                bad(f"{r['name']}: sampel ada tapi status tetap unknown",
                    "Alerter mungkin tidak jalan. Cek: "
                    "docker compose logs alerter --tail 30")
            else:
                info("(status masih diputuskan, tunggu beberapa menit)")

        if total < r["threshold_bps"]:
            warn(f"{r['name']}: traffic di bawah ambang — akan dianggap DOWN")

        # Ambang terlalu rendah = link mati tetap terlihat hidup
        if r["threshold_bps"] < 50000 and r["monitor_type"] == "snmp_if":
            warn(f"{r['name']}: ambang {r['threshold_bps']} bps sangat rendah. "
                 f"Traffic protokol switch (LLDP/STP) saja bisa melampauinya, "
                 f"jadi link kosong tetap terlihat hidup. Untuk dedicated, "
                 f"~100000 lebih masuk akal.")


def check_storage(conn):
    head("Penyimpanan")
    with db.dict_cursor(conn) as cur:
        cur.execute("SELECT pg_size_pretty(pg_database_size(current_database())) AS s")
        info(f"total database  : {cur.fetchone()['s']}")

        cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")
        has_ts = cur.fetchone() is not None

        if not has_ts:
            cur.execute("""
                SELECT pg_size_pretty(pg_total_relation_size('traffic_samples')) AS s
            """)
            info(f"traffic_samples : {cur.fetchone()['s']} (tanpa kompresi)")
            return

        # Ukuran hypertable + rincian kompresi
        try:
            cur.execute("""
                SELECT pg_size_pretty(hypertable_size('traffic_samples')) AS s
            """)
            info(f"traffic_samples : {cur.fetchone()['s']}")
        except Exception as e:
            info(f"ukuran traffic_samples tidak terbaca: {e}")
            conn.rollback()

        try:
            cur.execute("""
                SELECT
                    count(*) FILTER (WHERE is_compressed)     AS chunk_padat,
                    count(*) FILTER (WHERE NOT is_compressed) AS chunk_biasa
                FROM timescaledb_information.chunks
                WHERE hypertable_name = 'traffic_samples'
            """)
            c = cur.fetchone()
            info(f"chunk           : {c['chunk_padat']} padat, "
                 f"{c['chunk_biasa']} belum")

            if c["chunk_padat"] == 0 and c["chunk_biasa"] > 1:
                warn("Belum ada chunk yang dipadatkan. Kalau kompresi baru "
                     "diaktifkan, job latar belakang butuh waktu — atau chunk "
                     "memang belum cukup tua (ambangnya 7 hari).")
        except Exception:
            conn.rollback()

        try:
            cur.execute("""
                SELECT
                    pg_size_pretty(sum(before_compression_total_bytes)) AS sebelum,
                    pg_size_pretty(sum(after_compression_total_bytes))  AS sesudah,
                    sum(before_compression_total_bytes)::numeric
                        / nullif(sum(after_compression_total_bytes), 0) AS rasio,
                    round(100.0 * (1 - sum(after_compression_total_bytes)::numeric
                                     / nullif(sum(before_compression_total_bytes), 0)),
                          1) AS hemat
                FROM chunk_compression_stats('traffic_samples')
                WHERE after_compression_total_bytes IS NOT NULL
            """)
            r = cur.fetchone()
            if r and r["sesudah"] and r["rasio"]:
                rasio = float(r["rasio"])
                pesan = (f"Kompresi: {r['sebelum']} -> {r['sesudah']} "
                         f"(hemat {r['hemat']}%, rasio {rasio:.1f}x)")
                if rasio < 1:
                    bad(pesan + " — MALAH MEMBESAR",
                        "Segmen terlalu kecil untuk dipadatkan. Cek jumlah "
                        "baris per pelanggan per chunk; kalau sedikit, "
                        "perbesar chunk: SELECT set_chunk_time_interval("
                        "'traffic_samples', INTERVAL '14 days');")
                elif rasio < 3:
                    warn(pesan + " — di bawah harapan")
                    info("Data seperti ini biasanya 5-15x. Rasio rendah "
                         "biasanya berarti segmen terlalu kecil.")
                else:
                    ok(pesan)
            else:
                info("kompresi        : belum ada chunk padat")
        except Exception:
            conn.rollback()
            info("statistik kompresi tidak tersedia "
                 "(kompresi belum diaktifkan?)")

        # Ukuran segmen menentukan bagus-tidaknya kompresi. Segmen yang cuma
        # berisi belasan baris justru bikin data membengkak — overhead per
        # segmen lebih besar dari yang dihemat.
        try:
            cur.execute("""
                SELECT count(*)::numeric / nullif(count(DISTINCT customer_id), 0)
                       AS per_segmen
                FROM traffic_samples
                WHERE time > now() - interval '7 days'
            """)
            r = cur.fetchone()
            if r and r["per_segmen"]:
                n = float(r["per_segmen"])
                if n < 1000:
                    warn(f"Hanya ~{n:.0f} baris per pelanggan dalam 7 hari. "
                         f"Segmen sekecil ini membuat kompresi tidak efektif.")
                else:
                    info(f"baris/segmen    : ~{n:.0f} per pelanggan per chunk "
                         f"(sehat, di atas 1000)")
        except Exception:
            conn.rollback()

        # Laju pertumbuhan nyata
        try:
            cur.execute("""
                SELECT count(*) AS n FROM traffic_samples
                WHERE time > now() - interval '24 hours'
            """)
            per_day = cur.fetchone()["n"]
            if per_day:
                # ~73 byte per baris heap + ~27% indeks, terukur dari schema ini
                mb_day = per_day * 73 * 1.27 / 1e6
                info(f"laju            : {per_day} baris/hari "
                     f"(~{mb_day:.0f} MB/hari sebelum dipadatkan)")
        except Exception:
            conn.rollback()

        try:
            cur.execute("""
                SELECT pg_size_pretty(hypertable_size('traffic_hourly')) AS s
            """)
            info(f"traffic_hourly  : {cur.fetchone()['s']}")
        except Exception:
            conn.rollback()

        # Retensi aktif
        try:
            cur.execute("""
                SELECT hypertable_name, config->>'drop_after' AS umur
                FROM timescaledb_information.jobs
                WHERE proc_name = 'policy_retention'
            """)
            for r in cur.fetchall():
                info(f"retensi         : {r['hypertable_name']} disimpan "
                     f"{r['umur']}")
        except Exception:
            conn.rollback()


def check_ketahanan(conn):
    head("Ketahanan")
    # Dead man's switch
    if config.HEARTBEAT_URL:
        ok("Dead man's switch aktif — collector berdetak ke layanan luar")
    else:
        warn("HEARTBEAT_URL kosong. Kalau NMS mati semalam, tidak ada yang "
             "tahu — dan laporan SLA akan menghitung periode itu sebagai "
             "uptime 100%.")
        info("Isi dengan URL ping dari healthchecks.io / Uptime Kuma di .env")

    # Detak internal
    with db.dict_cursor(conn) as cur:
        try:
            cur.execute("""
                SELECT max(time) AS terakhir,
                       count(*) FILTER (WHERE time > now() - interval '24 hours') AS sehari
                FROM nms_heartbeat WHERE component = 'collector'
            """)
            h = cur.fetchone()
            if h["terakhir"] is None:
                warn("Belum ada catatan detak collector")
            else:
                info(f"detak terakhir  : {age_str(h['terakhir'])}")
                harap = 86400 // max(1, config.POLL_INTERVAL)
                if h["sehari"] < harap * 0.9:
                    hilang = 100 * (1 - h["sehari"] / harap)
                    bad(f"Collector tidak mencatat {hilang:.0f}% dari 24 jam "
                        f"terakhir ({h['sehari']} dari ~{harap} detak)",
                        "Selama itu gangguan tidak terdeteksi, dan laporan "
                        "SLA membacanya sebagai tidak ada gangguan.")
                else:
                    ok(f"Collector berdetak lengkap 24 jam terakhir "
                       f"({h['sehari']} detak)")
        except Exception:
            conn.rollback()
            warn("Tabel nms_heartbeat belum ada — jalankan ./scripts/upgrade-db.sh")

    # Paralelisme vs jumlah perangkat
    with db.dict_cursor(conn) as cur:
        cur.execute("SELECT count(*) AS n FROM devices WHERE enabled")
        n = cur.fetchone()["n"]
    blokir = config.SNMP_TIMEOUT * (config.SNMP_RETRIES + 1)
    terburuk = (n / max(1, config.POLL_WORKERS)) * blokir
    info(f"paralel         : {config.POLL_WORKERS} pekerja, {n} perangkat")
    if terburuk > config.POLL_INTERVAL:
        bad(f"Kalau SEMUA perangkat mati, satu siklus butuh ~{terburuk:.0f} "
            f"detik — melewati POLL_INTERVAL {config.POLL_INTERVAL} detik",
            f"Naikkan POLL_WORKERS ke minimal "
            f"{int(n * blokir / config.POLL_INTERVAL) + 1} di .env")
    else:
        ok(f"Skenario terburuk (semua perangkat mati): ~{terburuk:.0f} detik, "
           f"masih di bawah POLL_INTERVAL {config.POLL_INTERVAL} detik")

    # Enkripsi kredensial
    from . import crypto
    with db.dict_cursor(conn) as cur:
        cur.execute("""
            SELECT count(*) FILTER (WHERE api_password LIKE 'enc:%%') AS aman,
                   count(*) FILTER (WHERE api_password IS NOT NULL
                                    AND api_password <> ''
                                    AND api_password NOT LIKE 'enc:%%') AS polos
            FROM devices
        """)
        e = cur.fetchone()
    if e["polos"]:
        bad(f"{e['polos']} password perangkat masih tersimpan sebagai teks polos",
            "Isi NMS_SECRET_KEY di .env, lalu buka & simpan ulang perangkatnya "
            "lewat Kelola -> Perangkat. Yang paling berisiko: backup .sql.gz "
            "yang disalin keluar server.")
    elif e["aman"]:
        ok(f"{e['aman']} password perangkat tersimpan terenkripsi")


# ------------------------------------------------------------------- alerts
def check_alerts(conn):
    head("Alert")
    with db.dict_cursor(conn) as cur:
        cur.execute("""
            SELECT count(*) FILTER (WHERE resolved_at IS NULL) AS open,
                   count(*) FILTER (WHERE resolved_at IS NULL
                                    AND NOT notified AND NOT suppressed) AS failed,
                   count(*) AS total
            FROM alerts
        """)
        a = cur.fetchone()
    info(f"total {a['total']}, terbuka {a['open']}")
    if a["failed"]:
        bad(f"{a['failed']} alert gagal terkirim ke Telegram",
            "Cek TELEGRAM_BOT_TOKEN & TELEGRAM_CHAT_ID di .env, lalu "
            "docker compose restart alerter")
    else:
        ok("Tidak ada notifikasi yang gagal terkirim")


# --------------------------------------------------------------------- main
def main():
    print(f"{B}Diagnosa NMS{N}  —  {datetime.now().strftime('%d-%m-%Y %H:%M:%S')}")
    try:
        conn = db.get_conn()
    except Exception as e:
        print(f"\n  {R}MASALAH{N} Tidak bisa konek ke database: {e}")
        print("\n  Cek: docker compose ps db")
        print("  Cek POSTGRES_PASSWORD di .env sama dengan saat volume dibuat.")
        sys.exit(1)

    check_db(conn)
    if check_inventory(conn):
        reachable = check_devices(conn)
        try:
            check_ifindex(conn, reachable)
        except Exception as e:
            warn(f"Verifikasi ifIndex dilewati: {e}")
        check_samples(conn)
        check_customers(conn)
        check_alerts(conn)
        check_ketahanan(conn)
    check_storage(conn)

    head("Ringkasan")
    if not problems:
        print(f"  {G}Tidak ada masalah terdeteksi.{N}")
        print("  Kalau status pelanggan masih 'Belum diketahui', tunggu "
              f"{config.CONSECUTIVE_DOWN_SAMPLES * config.POLL_INTERVAL // 60 + 1} "
              "menit — alerter butuh beberapa sampel berturut-turut.")
    else:
        print(f"  {R}{len(problems)} masalah ditemukan:{N}\n")
        for i, (msg, fix) in enumerate(problems, 1):
            print(f"  {i}. {msg}")
            if fix:
                print(f"     -> {fix}")
            print()
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()

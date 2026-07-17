"""Alert engine.

Aturan Fase 1 (tetap berlaku):
- Sampel dianggap 'down' kalau link_up = FALSE, atau in+out < threshold_bps.
- Pelanggan DOWN kalau N sampel terakhir berturut-turut down.
- Pelanggan RECOVERY kalau sampel terakhir normal.

Tambahan Fase 3:
- Kesehatan perangkat: perangkat yang gagal di-poll berkali-kali menghasilkan
  SATU alert device_down. Selama perangkat down, evaluasi pelanggan di
  bawahnya dilewati — mencegah ratusan alert palsu ketika yang sebenarnya
  terjadi hanya satu switch mati.
- Jendela pemeliharaan: alert tetap dicatat tapi tidak dikirim ke Telegram
  dan ditandai suppressed, supaya laporan SLA bisa mengecualikannya.
- Degradasi: traffic yang turun jauh di bawah baseline historis pada jam yang
  sama menghasilkan alert traffic_degraded (severity minor).
- Eskalasi: alert major yang belum di-ack dan belum pulih setelah
  ESCALATION_MINUTES dikirim ulang sebagai pengingat.
"""
import logging
import time
from datetime import datetime, timezone

from . import baseline, config, db, notifier

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("alerter")


# ---------------------------------------------------------------- klasifikasi
def classify(sample: dict, threshold: int) -> str:
    """Klasifikasi satu sampel: 'down' | 'up' | 'unknown'."""
    if sample["link_up"] is False:
        return "down"
    if sample["in_bps"] is None or sample["out_bps"] is None:
        return "unknown"
    total = sample["in_bps"] + sample["out_bps"]
    return "down" if total < threshold else "up"


def alert_type_for(sample: dict, monitor_type: str) -> str:
    if sample["link_up"] is False:
        return "session_down" if monitor_type == "pppoe" else "link_down"
    return "traffic_zero"


# ---------------------------------------------------------------- pemeliharaan
def load_maintenance(conn):
    """Kembalikan (set device_id, set customer_id, ada_global)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT device_id, customer_id
            FROM maintenance_windows
            WHERE now() BETWEEN starts_at AND ends_at
            """
        )
        devs, custs, glob = set(), set(), False
        for dev_id, cust_id in cur.fetchall():
            if dev_id is None and cust_id is None:
                glob = True
            if dev_id is not None:
                devs.add(dev_id)
            if cust_id is not None:
                custs.add(cust_id)
        return devs, custs, glob


def in_maintenance(mw, device_id, customer_id) -> bool:
    devs, custs, glob = mw
    return glob or device_id in devs or customer_id in custs


# ---------------------------------------------------------------- data
def fetch_recent_samples(conn, customer_id: int, n: int):
    window_sec = max(config.POLL_INTERVAL * (n + 2), 300)
    with db.dict_cursor(conn) as cur:
        cur.execute(
            """
            SELECT time, in_bps, out_bps, link_up
            FROM traffic_samples
            WHERE customer_id = %s
              AND time > now() - make_interval(secs => %s)
            ORDER BY time DESC
            LIMIT %s
            """,
            (customer_id, window_sec, n),
        )
        return cur.fetchall()


def get_open_alert(conn, *, customer_id=None, device_id=None):
    with db.dict_cursor(conn) as cur:
        if customer_id is not None:
            cur.execute(
                """
                SELECT * FROM alerts
                WHERE customer_id = %s AND resolved_at IS NULL
                ORDER BY started_at DESC LIMIT 1
                """,
                (customer_id,),
            )
        else:
            cur.execute(
                """
                SELECT * FROM alerts
                WHERE device_id = %s AND resolved_at IS NULL
                ORDER BY started_at DESC LIMIT 1
                """,
                (device_id,),
            )
        return cur.fetchone()


def set_customer_status(conn, customer_id: int, status: str):
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE customers
            SET status = %s, status_changed_at = now()
            WHERE id = %s AND status IS DISTINCT FROM %s
            """,
            (status, customer_id, status),
        )


def set_device_status(conn, device_id: int, status: str):
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE devices
            SET status = %s, status_changed_at = now()
            WHERE id = %s AND status IS DISTINCT FROM %s
            """,
            (status, device_id, status),
        )


# ---------------------------------------------------------------- buka/tutup
def open_alert(conn, *, customer=None, device, a_type, severity, suppressed,
               parent_alert_id=None):
    started = datetime.now(timezone.utc)
    cust_id = customer["id"] if customer else None
    dev_id = None if customer else device["id"]
    with db.dict_cursor(conn) as cur:
        cur.execute(
            """
            INSERT INTO alerts (customer_id, device_id, alert_type, severity,
                                started_at, suppressed, parent_alert_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING id
            """,
            (cust_id, dev_id, a_type, severity, started, suppressed,
             parent_alert_id),
        )
        row = cur.fetchone()
    if row is None:
        return  # sudah ada alert terbuka, tidak perlu apa-apa
    alert_id = row["id"]

    if suppressed:
        log.info("Alert %s dicatat tapi ditahan (pemeliharaan): %s", a_type,
                 customer["name"] if customer else device["name"])
        return

    if parent_alert_id is not None:
        # Imbas ODP yang sudah dilaporkan sendiri. Alertnya TETAP dicatat dan
        # TETAP dihitung melawan SLA — pelanggannya memang mati. Yang ditekan
        # hanya notifikasinya, supaya Telegram tidak dibanjiri delapan pesan
        # untuk satu feeder putus.
        log.info("Alert %s dicatat, notifikasi ditekan (imbas ODP): %s",
                 a_type, customer["name"] if customer else device["name"])
        return

    if customer:
        sent = notifier.notify_down(customer, device, a_type, started, severity)
    else:
        sent = notifier.notify_device_down(device, started)
    if sent:
        with conn.cursor() as cur:
            cur.execute("UPDATE alerts SET notified = TRUE WHERE id = %s",
                        (alert_id,))
    log.info("ALERT %s: %s", a_type,
             customer["name"] if customer else device["name"])


def resolve_alert(conn, alert, *, customer=None, device):
    resolved = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute("UPDATE alerts SET resolved_at = %s WHERE id = %s",
                    (resolved, alert["id"]))
    if not alert["suppressed"] and alert["notified"]:
        if customer:
            notifier.notify_recovery(customer, device, alert["alert_type"],
                                     alert["started_at"], resolved)
        else:
            notifier.notify_device_recovery(device, alert["started_at"], resolved)
    log.info("RECOVERY: %s", customer["name"] if customer else device["name"])


def retry_notification(conn, alert, *, customer=None, device):
    """Kirim ulang notifikasi yang sebelumnya gagal terkirim."""
    if alert["notified"] or alert["suppressed"]:
        return
    if customer:
        sent = notifier.notify_down(customer, device, alert["alert_type"],
                                    alert["started_at"], alert["severity"])
    else:
        sent = notifier.notify_device_down(device, alert["started_at"])
    if sent:
        with conn.cursor() as cur:
            cur.execute("UPDATE alerts SET notified = TRUE WHERE id = %s",
                        (alert["id"],))


# ---------------------------------------------------------------- eskalasi
def run_escalation(conn):
    """Ingatkan lagi untuk gangguan major yang belum di-ack dan belum pulih."""
    if config.ESCALATION_MINUTES <= 0:
        return
    with db.dict_cursor(conn) as cur:
        cur.execute(
            """
            SELECT a.id, a.alert_type, a.started_at,
                   c.name AS cust_name, c.service_id,
                   d.name AS dev_name, d.ip AS dev_ip
            FROM alerts a
            LEFT JOIN customers c ON c.id = a.customer_id
            LEFT JOIN devices  d ON d.id = COALESCE(a.device_id, c.device_id)
            WHERE a.resolved_at IS NULL
              AND a.ack_at IS NULL
              AND a.escalated_at IS NULL
              AND a.notified
              AND NOT a.suppressed
              AND a.severity = 'major'
              AND a.started_at < now() - make_interval(mins => %s)
            """,
            (config.ESCALATION_MINUTES,),
        )
        rows = cur.fetchall()

    for r in rows:
        label = r["cust_name"] or r["dev_name"]
        ok = notifier.notify_escalation(
            label, r["service_id"], r["dev_name"], r["dev_ip"],
            r["alert_type"], r["started_at"],
        )
        if ok:
            with conn.cursor() as cur:
                cur.execute("UPDATE alerts SET escalated_at = now() WHERE id = %s",
                            (r["id"],))
            log.info("ESKALASI: %s", label)


# ---------------------------------------------------------------- siklus utama
def get_open_odp_alert(conn, odp_id):
    with db.dict_cursor(conn) as cur:
        cur.execute(
            "SELECT * FROM alerts WHERE odp_id = %s AND resolved_at IS NULL",
            (odp_id,),
        )
        return cur.fetchone()


def set_odp_status(conn, odp_id, status):
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE odps SET status = %s,
                   status_changed_at = CASE WHEN status <> %s
                                            THEN now() ELSE status_changed_at END
               WHERE id = %s""",
            (status, status, odp_id),
        )


def open_odp_alert(conn, odp, jumlah_down, total, suppressed):
    with db.dict_cursor(conn) as cur:
        cur.execute(
            """INSERT INTO alerts (odp_id, alert_type, severity, started_at,
                                   suppressed, notified)
               VALUES (%s, 'odp_down', 'major', now(), %s, FALSE)
               RETURNING *""",
            (odp["id"], suppressed),
        )
        a = cur.fetchone()

    if not suppressed:
        pesan = (
            f"🔴 ODP DOWN — {odp['name']}\n"
            f"{jumlah_down} dari {total} pelanggan mati bersamaan.\n"
        )
        if odp.get("lokasi"):
            pesan += f"Lokasi: {odp['lokasi']}\n"
        pesan += (
            "Kemungkinan feeder putus atau splitter rusak — "
            "bukan gangguan per pelanggan."
        )
        if notifier.send_telegram(pesan):
            with conn.cursor() as cur:
                cur.execute("UPDATE alerts SET notified = TRUE WHERE id = %s",
                            (a["id"],))
    return a


def resolve_odp_alert(conn, alert, odp):
    with conn.cursor() as cur:
        cur.execute("UPDATE alerts SET resolved_at = now() WHERE id = %s",
                    (alert["id"],))
        # Lepaskan anak-anaknya. Alert pelanggan yang masih terbuka setelah
        # ODP pulih berarti gangguan sendiri, bukan lagi imbas ODP.
        cur.execute(
            "UPDATE alerts SET parent_alert_id = NULL WHERE parent_alert_id = %s",
            (alert["id"],),
        )
    durasi = datetime.now(timezone.utc) - alert["started_at"]
    if alert["notified"]:
        notifier.send_telegram(
            f"🟢 ODP PULIH — {odp['name']}\nDurasi: {fmt_durasi(durasi)}"
        )


def fmt_durasi(d):
    total = int(d.total_seconds())
    j, sisa = divmod(total, 3600)
    m = sisa // 60
    if j:
        return f"{j} jam {m} menit"
    return f"{m} menit"


def korelasi_odp(conn, customers, penilaian, dead_devices, mw):
    """Tentukan ODP mana yang dianggap mati. Return {odp_id: alert_row}.

    Tidak ada perangkat aktif di dalam box ODP — cuma splitter pasif, tidak
    bisa ditanyai apa pun. Satu-satunya bukti bahwa ODP bermasalah adalah
    pelanggan di bawahnya mati BERSAMAAN.

    Satu pelanggan mati  = drop cable-nya sendiri.
    Semua mati bersamaan = feeder putus atau splitter rusak.
    """
    with db.dict_cursor(conn) as cur:
        cur.execute("SELECT * FROM odps WHERE enabled")
        odps = {o["id"]: o for o in cur.fetchall()}
    if not odps:
        return {}

    # Kelompokkan pelanggan per ODP
    anggota = {}
    for c in customers:
        if c.get("odp_id") in odps:
            anggota.setdefault(c["odp_id"], []).append(c)

    hasil = {}
    for odp_id, odp in odps.items():
        custs = anggota.get(odp_id, [])
        open_a = get_open_odp_alert(conn, odp_id)

        # Pelanggan yang perangkatnya mati tidak bisa dipakai sebagai bukti —
        # matinya karena switch/OLT, bukan karena ODP. Kalau semua bukti
        # berasal dari perangkat mati, ODP tidak bisa dinilai sama sekali.
        terpakai = [c for c in custs if c["device_id"] not in dead_devices]

        # Pelanggan yang belum cukup sampel juga bukan bukti apa-apa.
        dinilai = [c for c in terpakai
                   if penilaian.get(c["id"], {}).get("state") in ("up", "down")]

        total = len(dinilai)
        turun = [c for c in dinilai if penilaian[c["id"]]["state"] == "down"]

        cukup = (
            total >= config.ODP_MIN_DOWN
            and len(turun) >= config.ODP_MIN_DOWN
            and len(turun) / total >= config.ODP_DOWN_RATIO
        )

        if cukup:
            set_odp_status(conn, odp_id, "down")
            if open_a is None:
                suppressed = any(
                    in_maintenance(mw, c["device_id"], c["id"]) for c in turun
                )
                open_a = open_odp_alert(conn, odp, len(turun), total, suppressed)
                log.warning("ODP %s down: %d dari %d pelanggan mati",
                            odp["name"], len(turun), total)
            hasil[odp_id] = open_a
        else:
            if total:
                set_odp_status(conn, odp_id, "up" if not turun else "up")
            if open_a is not None:
                resolve_odp_alert(conn, open_a, odp)
                log.info("ODP %s pulih", odp["name"])

    return hasil


def run_check(conn):
    n = config.CONSECUTIVE_DOWN_SAMPLES
    mw = load_maintenance(conn)
    baselines = baseline.load_current_baselines(conn)

    with db.dict_cursor(conn) as cur:
        cur.execute("SELECT * FROM devices WHERE enabled")
        devices = {d["id"]: d for d in cur.fetchall()}
        cur.execute("SELECT * FROM customers WHERE enabled")
        customers = cur.fetchall()

    # ---- lapis 1: kesehatan perangkat ----
    dead_devices = set()
    for dev in devices.values():
        suppressed = in_maintenance(mw, dev["id"], None)
        open_a = get_open_alert(conn, device_id=dev["id"])

        if dev["fail_count"] >= config.DEVICE_FAIL_THRESHOLD:
            dead_devices.add(dev["id"])
            set_device_status(conn, dev["id"], "down")
            if open_a is None:
                open_alert(conn, device=dev, a_type="device_down",
                           severity="major", suppressed=suppressed)
            else:
                retry_notification(conn, open_a, device=dev)
        else:
            if dev["last_ok_at"] is not None:
                set_device_status(conn, dev["id"], "up")
            if open_a is not None:
                resolve_alert(conn, open_a, device=dev)

    # ---- lapis 2a: NILAI pelanggan, belum menulis alert ----
    #
    # Penilaian dipisah dari penulisan supaya ODP bisa diputuskan lebih dulu.
    # Kalau alert pelanggan langsung dikirim, delapan notifikasi sudah
    # terlanjur membanjiri Telegram sebelum kita sempat sadar bahwa
    # penyebabnya cuma satu ODP.
    penilaian = {}
    for c in customers:
        dev = devices.get(c["device_id"])
        if dev is None:
            continue

        # Perangkat mati: pelanggannya tidak bisa dinilai. Jangan buat alert
        # baru, jangan pula menutup alert lama secara palsu.
        if dev["id"] in dead_devices:
            continue

        samples = fetch_recent_samples(conn, c["id"], n)
        if len(samples) < n:
            continue  # data belum cukup atau sudah basi

        states = [classify(s, c["threshold_bps"]) for s in samples]
        if all(s == "down" for s in states):
            penilaian[c["id"]] = {
                "state": "down", "sample": samples[0],
                "samples": samples, "dev": dev,
            }
        elif states[0] == "up":
            penilaian[c["id"]] = {
                "state": "up", "sample": samples[0],
                "samples": samples, "dev": dev,
            }
        # campuran / unknown: tidak dinilai sama sekali

    # ---- lapis 2b: korelasi ODP, sebelum notifikasi pelanggan ----
    odp_alerts = korelasi_odp(conn, customers, penilaian, dead_devices, mw)

    cust_by_id = {c["id"]: c for c in customers}

    # ---- lapis 2c: tulis alert pelanggan ----
    for cid, p in penilaian.items():
        c = cust_by_id[cid]
        dev = p["dev"]
        suppressed = in_maintenance(mw, c["device_id"], c["id"])
        open_a = get_open_alert(conn, customer_id=cid)
        induk = odp_alerts.get(c.get("odp_id"))

        if p["state"] == "down":
            set_customer_status(conn, cid, "down")
            if open_a is None:
                # Kalau ODP-nya yang mati, notifikasi pelanggan ditekan —
                # satu pesan "ODP-X down (8 pelanggan)" jauh lebih berguna
                # daripada delapan pesan terpisah pada saat bersamaan.
                # Alertnya TETAP dicatat dan TETAP dihitung melawan SLA:
                # pelanggannya memang benar-benar mati.
                open_alert(conn, customer=c, device=dev,
                           a_type=alert_type_for(p["sample"], c["monitor_type"]),
                           severity="major", suppressed=suppressed,
                           parent_alert_id=induk["id"] if induk else None)
            else:
                if induk and open_a["parent_alert_id"] is None:
                    # Gangguan yang tadinya sendirian ternyata bagian dari ODP
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE alerts SET parent_alert_id = %s WHERE id = %s",
                            (induk["id"], open_a["id"]),
                        )
                elif not induk:
                    retry_notification(conn, open_a, customer=c, device=dev)
            continue

        # Sampel terakhir normal -> tutup gangguan yang masih terbuka
        if open_a is not None:
            resolve_alert(conn, open_a, customer=c, device=dev)
            open_a = None
        set_customer_status(conn, cid, "up")

        # ---- lapis 3: degradasi terhadap baseline ----
        if not c["baseline_enabled"]:
            continue
        base = baselines.get(cid)
        if base is None:
            continue
        totals = [
            s["in_bps"] + s["out_bps"] for s in p["samples"]
            if s["in_bps"] is not None and s["out_bps"] is not None
        ]
        if len(totals) < n:
            continue
        degraded = all(
            baseline.is_degraded(t, base, c["baseline_drop_pct"]) for t in totals
        )
        if degraded and open_a is None:
            open_alert(conn, customer=c, device=dev, a_type="traffic_degraded",
                       severity="minor", suppressed=suppressed)


def main():
    log.info(
        "Alerter start — cek tiap %ds, %d sampel berturut-turut, "
        "perangkat gagal %dx, eskalasi %d menit",
        config.ALERT_CHECK_INTERVAL, config.CONSECUTIVE_DOWN_SAMPLES,
        config.DEVICE_FAIL_THRESHOLD, config.ESCALATION_MINUTES,
    )
    conn = db.get_conn()
    last_baseline = 0.0
    while True:
        start = time.time()
        try:
            run_check(conn)
            run_escalation(conn)
            # Baseline dihitung ulang berkala, bukan tiap siklus (mahal)
            if time.time() - last_baseline > config.BASELINE_REFRESH_HOURS * 3600:
                baseline.rebuild_baseline(conn)
                last_baseline = time.time()
        except Exception:
            log.exception("Check error")
            try:
                conn.close()
            except Exception:
                pass
            conn = db.get_conn()
        elapsed = time.time() - start
        time.sleep(max(1, config.ALERT_CHECK_INTERVAL - elapsed))


if __name__ == "__main__":
    main()

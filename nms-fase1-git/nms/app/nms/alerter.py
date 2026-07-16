"""Alert engine: deteksi DOWN dan RECOVERY dari traffic_samples.

Rule Fase 1:
- Sample dianggap "down" kalau:
    * link_up = FALSE (link fisik down / sesi PPPoE hilang), atau
    * in_bps + out_bps < threshold_bps (traffic zero).
- Customer dinyatakan DOWN kalau N sample TERAKHIR berturut-turut down
  (N = CONSECUTIVE_DOWN_SAMPLES) -> buat alert + notif Telegram.
- Customer dinyatakan RECOVERY kalau sedang ada alert open dan sample
  terakhir "up" -> resolve alert + notif recovery.
- Sample dengan bps NULL tapi link_up TRUE dianggap "unknown"
  (belum bisa dinilai) dan memutus hitungan berturut-turut, supaya
  restart collector tidak memicu false alarm.
"""
import logging
import time
from datetime import datetime, timezone

from . import config, db, notifier

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("alerter")


def classify(sample: dict, threshold: int) -> str:
    """Klasifikasi satu sample: 'down' | 'up' | 'unknown'."""
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


def fetch_recent_samples(conn, customer_id: int, n: int):
    """Ambil n sample terakhir dalam window wajar (anti data basi)."""
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


def get_open_alert(conn, customer_id: int):
    with db.dict_cursor(conn) as cur:
        cur.execute(
            """
            SELECT * FROM alerts
            WHERE customer_id = %s AND resolved_at IS NULL
            ORDER BY started_at DESC LIMIT 1
            """,
            (customer_id,),
        )
        return cur.fetchone()


def set_status(conn, customer_id: int, status: str):
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE customers
            SET status = %s, status_changed_at = now()
            WHERE id = %s AND status IS DISTINCT FROM %s
            """,
            (status, customer_id, status),
        )


def handle_down(conn, customer: dict, device: dict, latest: dict):
    """Pastikan ada alert open + notif untuk kondisi down."""
    open_alert = get_open_alert(conn, customer["id"])
    if open_alert is None:
        a_type = alert_type_for(latest, customer["monitor_type"])
        started = datetime.now(timezone.utc)
        with db.dict_cursor(conn) as cur:
            cur.execute(
                """
                INSERT INTO alerts (customer_id, alert_type, started_at)
                VALUES (%s, %s, %s) RETURNING id
                """,
                (customer["id"], a_type, started),
            )
            alert_id = cur.fetchone()["id"]
        ok = notifier.notify_down(customer, device, a_type, started)
        if ok:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE alerts SET notified = TRUE WHERE id = %s",
                    (alert_id,),
                )
        log.info("ALERT DOWN: %s (%s)", customer["name"], a_type)
    else:
        # Alert sudah ada; retry notif kalau sebelumnya gagal terkirim
        if not open_alert["notified"]:
            ok = notifier.notify_down(
                customer, device,
                open_alert["alert_type"], open_alert["started_at"],
            )
            if ok:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE alerts SET notified = TRUE WHERE id = %s",
                        (open_alert["id"],),
                    )
    set_status(conn, customer["id"], "down")


def handle_recovery(conn, customer: dict, device: dict):
    open_alert = get_open_alert(conn, customer["id"])
    if open_alert is not None:
        resolved = datetime.now(timezone.utc)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE alerts SET resolved_at = %s WHERE id = %s",
                (resolved, open_alert["id"]),
            )
        notifier.notify_recovery(
            customer, device,
            open_alert["alert_type"], open_alert["started_at"], resolved,
        )
        log.info("RECOVERY: %s", customer["name"])
    set_status(conn, customer["id"], "up")


def run_check(conn):
    n = config.CONSECUTIVE_DOWN_SAMPLES
    with db.dict_cursor(conn) as cur:
        cur.execute(
            """
            SELECT c.*, d.name AS device_name, d.ip AS device_ip
            FROM customers c
            JOIN devices d ON d.id = c.device_id
            WHERE c.enabled AND d.enabled
            """
        )
        customers = cur.fetchall()

    for c in customers:
        device = {"name": c["device_name"], "ip": c["device_ip"]}
        samples = fetch_recent_samples(conn, c["id"], n)

        if len(samples) < n:
            # Data belum cukup / basi -> jangan ambil keputusan
            continue

        states = [classify(s, c["threshold_bps"]) for s in samples]
        latest = samples[0]

        if all(s == "down" for s in states):
            handle_down(conn, c, device, latest)
        elif states[0] == "up":
            handle_recovery(conn, c, device)
        # selain itu (campuran / unknown): biarkan status apa adanya


def main():
    log.info(
        "Alerter start, check tiap %ds, threshold %d sample berturut-turut",
        config.ALERT_CHECK_INTERVAL, config.CONSECUTIVE_DOWN_SAMPLES,
    )
    conn = db.get_conn()
    while True:
        start = time.time()
        try:
            run_check(conn)
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

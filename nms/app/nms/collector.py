"""Collector: poll semua device tiap POLL_INTERVAL, hitung bps dari delta
counter, simpan ke traffic_samples.

Catatan penting:
- Sample pertama tiap customer punya bps NULL (belum ada delta).
- Delta negatif (counter reset / reboot / reconnect PPPoE) -> bps NULL,
  supaya tidak menghasilkan angka absurd.
"""
import logging
import time
from datetime import datetime, timezone

from . import config, db
from .pollers.mikrotik import poll_pppoe_customers
from .pollers.probe import probe_device
from .pollers.snmp import poll_snmp_customers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("collector")

# Cache counter terakhir: {customer_id: (epoch, in_octets, out_octets)}
_last = {}


def load_inventory(conn):
    """Ambil device enabled beserta customer enabled-nya, dikelompokkan."""
    with db.dict_cursor(conn) as cur:
        cur.execute("SELECT * FROM devices WHERE enabled")
        devices = cur.fetchall()
        cur.execute("SELECT * FROM customers WHERE enabled")
        customers = cur.fetchall()

    by_device = {}
    for c in customers:
        by_device.setdefault(c["device_id"], []).append(c)
    return devices, by_device


def compute_bps(customer_id: int, now: float, in_oct, out_oct):
    """Hitung in/out bps dari delta counter. Return (in_bps, out_bps)."""
    if in_oct is None or out_oct is None:
        _last.pop(customer_id, None)
        return None, None

    prev = _last.get(customer_id)
    _last[customer_id] = (now, in_oct, out_oct)

    if prev is None:
        return None, None

    prev_ts, prev_in, prev_out = prev
    dt = now - prev_ts
    if dt <= 0:
        return None, None

    d_in = in_oct - prev_in
    d_out = out_oct - prev_out
    # Counter reset / wrap -> skip perhitungan cycle ini
    if d_in < 0 or d_out < 0:
        return None, None

    return int(d_in * 8 / dt), int(d_out * 8 / dt)


def record_device_health(conn, device_id: int, ok: bool):
    """Catat hasil polling perangkat.

    Ini yang memungkinkan alerter membedakan 'perangkat mati' dari
    'pelanggan mati'. Tanpa ini, satu switch mati tampak seperti
    ratusan pelanggan down sekaligus.
    """
    with conn.cursor() as cur:
        if ok:
            cur.execute(
                """
                UPDATE devices
                SET last_ok_at = now(), fail_count = 0
                WHERE id = %s
                """,
                (device_id,),
            )
        else:
            cur.execute(
                "UPDATE devices SET fail_count = fail_count + 1 WHERE id = %s",
                (device_id,),
            )


def run_cycle(conn):
    devices, cust_by_device = load_inventory(conn)
    now = time.time()
    ts = datetime.now(timezone.utc)
    rows = []

    for dev in devices:
        custs = cust_by_device.get(dev["id"], [])
        snmp_custs = [c for c in custs if c["monitor_type"] == "snmp_if"]
        pppoe_custs = [c for c in custs if c["monitor_type"] == "pppoe"]

        polled = {}
        if snmp_custs and dev["poll_method"] == "snmp":
            polled.update(poll_snmp_customers(dev, snmp_custs))
        if pppoe_custs and dev["poll_method"] == "mikrotik_api":
            polled.update(poll_pppoe_customers(dev, pppoe_custs))

        # Perangkat tanpa pelanggan tetap di-probe supaya kesehatannya terpantau
        if not custs:
            reachable = probe_device(dev)
        else:
            reachable = len(polled) > 0
        record_device_health(conn, dev["id"], reachable)

        for c in custs:
            data = polled.get(c["id"])
            if data is None:
                # Perangkat tidak merespon: jangan tulis sampel palsu.
                # Alerter memperlakukan data basi sebagai 'tidak diketahui'.
                continue
            in_bps, out_bps = compute_bps(
                c["id"], now, data["in_octets"], data["out_octets"]
            )
            rows.append((
                ts, c["id"], in_bps, out_bps,
                data["in_octets"], data["out_octets"], data["link_up"],
            ))

    if rows:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO traffic_samples
                    (time, customer_id, in_bps, out_bps,
                     in_octets, out_octets, link_up)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                rows,
            )
    log.info("Cycle selesai: %d sample dari %d device", len(rows), len(devices))


def main():
    log.info("Collector start, interval %ds", config.POLL_INTERVAL)
    conn = db.get_conn()
    while True:
        start = time.time()
        try:
            run_cycle(conn)
        except Exception:
            log.exception("Cycle error")
            try:
                conn.close()
            except Exception:
                pass
            conn = db.get_conn()
        elapsed = time.time() - start
        time.sleep(max(1, config.POLL_INTERVAL - elapsed))


if __name__ == "__main__":
    main()

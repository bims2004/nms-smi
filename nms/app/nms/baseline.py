"""Baseline traffic per pelanggan, untuk mendeteksi degradasi.

Kenapa perlu: aturan Fase 1 hanya menangkap traffic yang benar-benar nol.
Pelanggan yang biasanya 300 Mbps lalu turun jadi 20 Mbps tetap dianggap
normal, padahal itu gangguan nyata (redaman naik, uplink penuh, salah
policy shaping).

Cara kerja: untuk tiap pelanggan, hitung median traffic total pada tiap
kombinasi (hari, jam) waktu lokal selama 28 hari terakhir. Traffic malam
Minggu tidak dibandingkan dengan traffic Senin pagi.
"""
import logging

from . import config

log = logging.getLogger(__name__)

# Baseline di bawah nilai ini tidak dipakai — pelanggan yang memang
# sepi tidak boleh memicu alert degradasi.
MIN_BASELINE_BPS = 1_000_000  # 1 Mbps
MIN_SAMPLES = 20              # butuh cukup riwayat sebelum dipercaya
BASELINE_DAYS = 28


def rebuild_baseline(conn, customer_id=None):
    """Hitung ulang median per (dow, hour) dan simpan ke traffic_baseline."""
    where = "AND customer_id = %s" if customer_id else ""
    params = [config.TZ_DISPLAY, config.TZ_DISPLAY, f"{BASELINE_DAYS} days"]
    if customer_id:
        params.append(customer_id)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO traffic_baseline (customer_id, dow, hour, median_bps, samples, updated_at)
            SELECT customer_id,
                   EXTRACT(DOW  FROM time AT TIME ZONE %s)::smallint AS dow,
                   EXTRACT(HOUR FROM time AT TIME ZONE %s)::smallint AS hour,
                   percentile_cont(0.5) WITHIN GROUP (
                       ORDER BY in_bps + out_bps
                   )::bigint AS median_bps,
                   count(*) AS samples,
                   now()
            FROM traffic_samples
            WHERE time > now() - %s::interval
              AND in_bps IS NOT NULL AND out_bps IS NOT NULL
              AND link_up
              {where}
            GROUP BY customer_id, dow, hour
            HAVING count(*) >= {MIN_SAMPLES}
            ON CONFLICT (customer_id, dow, hour) DO UPDATE
            SET median_bps = EXCLUDED.median_bps,
                samples    = EXCLUDED.samples,
                updated_at = EXCLUDED.updated_at
            """,
            params,
        )
        n = cur.rowcount
    log.info("Baseline diperbarui: %d entri", n)
    return n


def load_current_baselines(conn):
    """{customer_id: median_bps} untuk (hari, jam) saat ini."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT b.customer_id, b.median_bps
            FROM traffic_baseline b
            WHERE b.dow  = EXTRACT(DOW  FROM now() AT TIME ZONE %s)::smallint
              AND b.hour = EXTRACT(HOUR FROM now() AT TIME ZONE %s)::smallint
              AND b.median_bps >= %s
            """,
            (config.TZ_DISPLAY, config.TZ_DISPLAY, MIN_BASELINE_BPS),
        )
        return {r[0]: r[1] for r in cur.fetchall()}


def is_degraded(total_bps, baseline_bps, drop_pct) -> bool:
    """True kalau traffic turun lebih dari drop_pct dibanding baseline."""
    if baseline_bps is None or baseline_bps < MIN_BASELINE_BPS:
        return False
    if total_bps is None:
        return False
    threshold = baseline_bps * (1 - drop_pct / 100.0)
    return total_bps < threshold

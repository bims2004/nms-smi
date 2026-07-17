"""View dashboard NMS."""
import csv
from calendar import monthrange
from datetime import datetime as _dt
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.formats import date_format
from django.views.decorators.http import require_POST

from . import config_hint, importer
from .discovery import discover_pppoe_sessions, discover_snmp_interfaces
from .models import (ALERT_TYPE_LABEL, Alert, Customer, Device, Odp,
                     MaintenanceWindow)

SPARK_MINUTES = 60
SPARK_BUCKET = "2 minutes"


# ---------------------------------------------------------------- helpers
def fmt_bps(v):
    """1234567 -> '1.23 Mbps'."""
    if v is None:
        return "—"
    v = float(v)
    for unit, div in (("Gbps", 1e9), ("Mbps", 1e6), ("kbps", 1e3)):
        if v >= div:
            return f"{v / div:.2f} {unit}"
    return f"{int(v)} bps"


def flip_series(series):
    """Tukar kolom in/out pada hasil fetch_series.

    Dipanggil sekali, tepat setelah data diambil. Setelah ini semua yang di
    hilir — grafik, statistik, puncak — memakai sudut pandang pelanggan dan
    tidak perlu tahu-menahu soal arah port. Menukar di beberapa tempat berisiko
    tertukar dua kali, dan salah semacam itu tidak kelihatan: angkanya tetap
    masuk akal, cuma tertukar.
    """
    return [(r[0], r[2], r[1], r[4], r[3], r[5]) for r in series]


def arah_bps(customer, in_v, out_v):
    """Terjemahkan counter mentah ke sudut pandang pelanggan.

    Kembalikan (download, upload). Counter di database selalu mentah:
    in = ifInOctets = masuk ke port. Di port yang menghadap pelanggan, itu
    justru upload pelanggan — makanya perlu ditukar.

    Semua tampilan memanggil fungsi ini. Kalau penerjemahannya disebar ke
    banyak tempat, cepat atau lambat ada satu yang terlewat dan angkanya
    bertentangan antar-halaman.
    """
    return (out_v, in_v) if customer.flip_arah else (in_v, out_v)


def fmt_bytes(v):
    """Volume data dalam satuan yang enak dibaca."""
    if v is None:
        return "—"
    for unit, div in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if v >= div:
            return f"{v / div:.2f} {unit}"
    return f"{v:.0f} B"


def fmt_bps_axis(v):
    """Label sumbu grafik — ringkas supaya tidak terpotong. 399140000 -> '399M'."""
    if v is None:
        return "—"
    v = float(v)
    for unit, div in (("G", 1e9), ("M", 1e6), ("k", 1e3)):
        if v >= div:
            n = v / div
            return f"{n:.0f}{unit}" if n >= 10 else f"{n:.1f}{unit}"
    return f"{int(v)}"


def fmt_duration(delta):
    if delta is None:
        return "—"
    total = int(delta.total_seconds())
    d, rem = divmod(total, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}h {h}j"
    if h:
        return f"{h}j {m}m"
    if m:
        return f"{m}m {s}dt"
    return f"{s}dt"


def sparkline_svg(values, width=140, height=28):
    """SVG sparkline dibuat di server — tidak butuh library chart di browser."""
    pts = [v for v in values if v is not None]
    if len(pts) < 2:
        return ""
    lo, hi = min(pts), max(pts)
    span = (hi - lo) or 1
    step = width / (len(values) - 1)
    coords, prev_ok = [], False
    d = []
    for i, v in enumerate(values):
        if v is None:
            prev_ok = False
            continue
        x = i * step
        y = height - ((v - lo) / span) * (height - 4) - 2
        d.append(f"{'M' if not prev_ok else 'L'}{x:.1f},{y:.1f}")
        coords.append((x, y))
        prev_ok = True
    if not d:
        return ""
    return (
        f'<svg class="spark" viewBox="0 0 {width} {height}" '
        f'preserveAspectRatio="none" aria-hidden="true">'
        f'<path d="{" ".join(d)}" fill="none" stroke="currentColor" '
        f'stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>'
        f'</svg>'
    )


def latest_samples():
    """{customer_id: {time, in_bps, out_bps, link_up}} dari window terakhir."""
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (customer_id)
                   customer_id, time, in_bps, out_bps, link_up
            FROM traffic_samples
            WHERE time > now() - interval '15 minutes'
            ORDER BY customer_id, time DESC
            """
        )
        return {
            r[0]: {"time": r[1], "in_bps": r[2], "out_bps": r[3], "link_up": r[4]}
            for r in cur.fetchall()
        }


def spark_series():
    """{customer_id: [total_bps, ...]} 1 jam terakhir."""
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT customer_id,
                   time_bucket(%s::interval, time) AS b,
                   avg(COALESCE(in_bps, 0) + COALESCE(out_bps, 0)) AS total
            FROM traffic_samples
            WHERE time > now() - %s::interval
            GROUP BY customer_id, b
            ORDER BY customer_id, b
            """,
            (SPARK_BUCKET, f"{SPARK_MINUTES} minutes"),
        )
        out = {}
        for cid, _, total in cur.fetchall():
            out.setdefault(cid, []).append(float(total) if total is not None else None)
        return out


def open_alerts_map():
    return {
        a.customer_id: a
        for a in Alert.objects.filter(resolved_at__isnull=True)
    }


def build_rows():
    customers = list(Customer.objects.select_related("device").all())
    samples = latest_samples()
    sparks = spark_series()
    opens = open_alerts_map()
    now = timezone.now()

    rows = []
    for c in customers:
        s = samples.get(c.id)
        alert = opens.get(c.id)

        if not c.enabled:
            state = "disabled"
        elif s is None:
            state = "stale"
        elif alert is not None or c.status == "down":
            state = "down"
        elif c.status == "up":
            state = "up"
        else:
            state = "unknown"

        total = None
        if s and s["in_bps"] is not None and s["out_bps"] is not None:
            total = s["in_bps"] + s["out_bps"]

        dl, ul = arah_bps(c, s["in_bps"], s["out_bps"]) if s else (None, None)
        rows.append({
            "obj": c,
            "state": state,
            "in_bps": fmt_bps(dl) if s else "—",
            "out_bps": fmt_bps(ul) if s else "—",
            "total_raw": total,
            "last_seen": s["time"] if s else None,
            "spark": sparkline_svg(sparks.get(c.id, [])),
            "down_since": alert.started_at if alert else None,
            "down_for": fmt_duration(now - alert.started_at) if alert else None,
            "port": c.if_name or (c.pppoe_username or "—"),
        })

    order = {"down": 0, "stale": 1, "unknown": 2, "up": 3, "disabled": 4}
    rows.sort(key=lambda r: (order[r["state"]], r["obj"].name))
    return rows


# ---------------------------------------------------------------- views
@login_required
def dashboard(request):
    rows = build_rows()
    counts = {"up": 0, "down": 0, "stale": 0, "unknown": 0, "disabled": 0}
    for r in rows:
        counts[r["state"]] += 1

    return render(request, "monitor/dashboard.html", {
        "rows": rows,
        "counts": counts,
        "total": len(rows),
        "device_count": Device.objects.filter(enabled=True).count(),
        "nav": "dashboard",
    })


@login_required
def status_json(request):
    """Dipakai halaman dashboard untuk refresh tanpa reload penuh."""
    rows = build_rows()
    counts = {"up": 0, "down": 0, "stale": 0, "unknown": 0, "disabled": 0}
    for r in rows:
        counts[r["state"]] += 1

    # Daftar gangguan yang sedang terbuka. Frontend membandingkan id-nya
    # dengan yang sudah pernah dilihat untuk memutuskan kapan membunyikan
    # alarm — jadi alarm hanya berbunyi untuk gangguan BARU, bukan tiap
    # kali halaman menyegarkan diri.
    alerts = []
    for a in (Alert.objects
              .filter(resolved_at__isnull=True, suppressed=False)
              .select_related("customer", "device")
              .order_by("-started_at")[:50]):
        alerts.append({
            "id": a.id,
            "severity": a.severity,
            "type": ALERT_TYPE_LABEL.get(a.alert_type, a.alert_type),
            "subject": a.subject,
            "acked": a.ack_at is not None,
        })

    return JsonResponse({
        "counts": counts,
        "updated": timezone.localtime().strftime("%H:%M:%S"),
        "alerts": alerts,
        "rows": [
            {
                "id": r["obj"].id,
                "state": r["state"],
                "in_bps": r["in_bps"],
                "out_bps": r["out_bps"],
                "down_for": r["down_for"],
            }
            for r in rows
        ],
    })


# Rentang yang bisa dipilih: (kode, label, jumlah jam, ukuran bucket)
RANGES = [
    ("15m", "15 menit", 0.25, "1 minute"),
    ("1h",  "1 jam",    1,    "1 minute"),
    ("6h",  "6 jam",    6,    "1 minute"),
    ("24h", "24 jam",   24,   "5 minutes"),
    ("7d",  "7 hari",   168,  "30 minutes"),
    ("30d", "30 hari",  720,  "2 hours"),
    ("90d", "90 hari",  2160, "6 hours"),
]
RANGE_MAP = {r[0]: r for r in RANGES}


def parse_period(request):
    """Tentukan rentang waktu yang diminta.

    Dua mode: rentang relatif (?range=24h) atau satu tanggal penuh
    (?date=2026-07-15, 00:00-23:59 waktu lokal).
    """
    tz = timezone.get_current_timezone()
    now = timezone.now()
    date_str = request.GET.get("date", "").strip()

    if date_str:
        try:
            d = _dt.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            d = timezone.localdate()
        start = timezone.make_aware(_dt(d.year, d.month, d.day), tz)
        end = min(start + timedelta(days=1), now)
        if end <= start:
            # Tanggal di masa depan: tidak ada data, jangan bikin rentang negatif
            end = start + timedelta(days=1)
        return {
            "start": start, "end": end, "bucket": "5 minutes",
            "mode": "date", "date": d, "range": None,
            # date_format ikut locale Django (id-ID); strftime tidak — dia
            # pakai locale C dan menghasilkan nama bulan berbahasa Inggris.
            "label": date_format(timezone.localtime(start), "j F Y"),
        }

    code = request.GET.get("range", "6h")
    if code not in RANGE_MAP:
        code = "6h"
    _, label, hours, bucket = RANGE_MAP[code]
    return {
        "start": now - timedelta(hours=hours), "end": now, "bucket": bucket,
        "mode": "range", "date": None, "range": code,
        "label": f"{label} terakhir",
    }


def fetch_series(customer_id, period):
    """Ambil data traffic yang sudah di-bucket.

    Sampel mentah hanya disimpan 90 hari. Untuk rentang yang lebih panjang,
    dibaca dari rollup jam-an (traffic_hourly) yang disimpan jauh lebih lama.
    """
    span_days = (period["end"] - period["start"]).total_seconds() / 86400
    use_rollup = span_days > 60 and rollup_available()

    with connection.cursor() as cur:
        if use_rollup:
            cur.execute(
                """
                SELECT time_bucket(%s::interval, bucket) AS b,
                       avg(avg_in)::float, avg(avg_out)::float,
                       max(max_in)::float, max(max_out)::float,
                       sum(down_samples) = 0 AS link_up
                FROM traffic_hourly
                WHERE customer_id = %s AND bucket >= %s AND bucket < %s
                GROUP BY b ORDER BY b
                """,
                (period["bucket"], customer_id, period["start"], period["end"]),
            )
        else:
            cur.execute(
                """
                SELECT time_bucket(%s::interval, time) AS b,
                       avg(in_bps)::float, avg(out_bps)::float,
                       max(in_bps)::float, max(out_bps)::float,
                       bool_and(link_up) AS link_up
                FROM traffic_samples
                WHERE customer_id = %s AND time >= %s AND time < %s
                GROUP BY b ORDER BY b
                """,
                (period["bucket"], customer_id, period["start"], period["end"]),
            )
        return cur.fetchall(), use_rollup


def rollup_available():
    """traffic_hourly hanya ada kalau migrasi rollup sudah dijalankan."""
    with connection.cursor() as cur:
        cur.execute("SELECT to_regclass('public.traffic_hourly') IS NOT NULL")
        return cur.fetchone()[0]


def volume_bytes(customer_id, period, use_rollup, flip=False):
    """Volume data terpakai, dalam byte.

    Dihitung dari jarak waktu tiap sampel ke sampel sebelumnya, bukan dari
    lebar bucket. Kalau pakai lebar bucket, bucket yang cuma terisi sebagian
    (selalu terjadi di awal & akhir periode) akan dihitung penuh dan volumenya
    menggelembung.

    Jeda lebih dari 10 menit dianggap tidak ada data, bukan traffic yang
    berlanjut di kecepatan terakhir — kalau perangkat mati semalam, kita
    memang tidak tahu apa yang lewat, jadi jangan mengarang.
    """
    with connection.cursor() as cur:
        if use_rollup:
            # Rollup sudah per jam penuh; samples menyimpan jumlah sampel asli
            cur.execute(
                """
                SELECT sum(avg_in * samples * 60) / 8,
                       sum(avg_out * samples * 60) / 8
                FROM traffic_hourly
                WHERE customer_id = %s AND bucket >= %s AND bucket < %s
                """,
                (customer_id, period["start"], period["end"]),
            )
        else:
            cur.execute(
                """
                SELECT sum(in_bps * gap) / 8, sum(out_bps * gap) / 8
                FROM (
                    SELECT in_bps, out_bps,
                           EXTRACT(EPOCH FROM (
                               time - lag(time) OVER (ORDER BY time)
                           )) AS gap
                    FROM traffic_samples
                    WHERE customer_id = %s AND time >= %s AND time < %s
                      AND in_bps IS NOT NULL
                ) x
                WHERE gap IS NOT NULL AND gap <= 600
                """,
                (customer_id, period["start"], period["end"]),
            )
        row = cur.fetchone()
    vin = float(row[0]) if row and row[0] is not None else 0.0
    vout = float(row[1]) if row and row[1] is not None else 0.0
    # Query di atas membaca counter mentah, jadi penukarannya di sini.
    return (vout, vin) if flip else (vin, vout)


def usage_stats(customer_id, period, series, use_rollup, flip=False):
    """Hitung pemakaian: volume, rata-rata, puncak, dan 95th percentile."""
    ins = [r[1] for r in series if r[1] is not None]
    outs = [r[2] for r in series if r[2] is not None]
    if not ins and not outs:
        return None

    vol_in, vol_out = volume_bytes(customer_id, period, use_rollup, flip)

    # 95th percentile: cara penagihan yang lazim di ISP. Dihitung dari
    # rata-rata 5 menit, ambil nilai in/out yang lebih besar tiap interval.
    # Hanya bisa dari sampel mentah, jadi terbatas 90 hari terakhir.
    p95 = None
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY v)
            FROM (
                SELECT greatest(avg(in_bps), avg(out_bps)) AS v
                FROM traffic_samples
                WHERE customer_id = %s AND time >= %s AND time < %s
                  AND in_bps IS NOT NULL
                GROUP BY time_bucket('5 minutes'::interval, time)
            ) x
            """,
            (customer_id, period["start"], period["end"]),
        )
        row = cur.fetchone()
        if row and row[0] is not None:
            p95 = float(row[0])

    return {
        "vol_in": fmt_bytes(vol_in),
        "vol_out": fmt_bytes(vol_out),
        "vol_total": fmt_bytes(vol_in + vol_out),
        "avg_in": fmt_bps(sum(ins) / len(ins) if ins else None),
        "avg_out": fmt_bps(sum(outs) / len(outs) if outs else None),
        "peak_in": fmt_bps(max((r[3] for r in series if r[3] is not None),
                               default=None)),
        "peak_out": fmt_bps(max((r[4] for r in series if r[4] is not None),
                                default=None)),
        "p95": fmt_bps(p95) if p95 is not None else None,
        "p95_stale": use_rollup,
    }


@login_required
def customer_detail(request, pk):
    c = get_object_or_404(Customer.objects.select_related("device"), pk=pk)
    period = parse_period(request)
    series, from_rollup = fetch_series(c.id, period)
    if c.flip_arah:
        series = flip_series(series)
    stats = usage_stats(c.id, period, series, from_rollup, c.flip_arah)
    chart = build_chart(series, period)

    alerts = list(Alert.objects.filter(customer=c)[:20])
    for a in alerts:
        a.dur = fmt_duration(a.duration) if a.resolved_at else "berlangsung"
        a.label = ALERT_TYPE_LABEL.get(a.alert_type, a.alert_type)

    # Uptime dalam periode yang dipilih
    window = (period["end"] - period["start"]).total_seconds()
    down_secs = 0.0
    for a in Alert.objects.filter(customer=c, severity="major",
                                  started_at__lt=period["end"]).filter(
            Q(resolved_at__isnull=True) | Q(resolved_at__gt=period["start"])):
        s = max(a.started_at, period["start"])
        e = min(a.resolved_at or period["end"], period["end"])
        if e > s:
            down_secs += (e - s).total_seconds()
    uptime = max(0.0, 100.0 * (1 - down_secs / window)) if window else 100.0

    today = timezone.localdate()
    return render(request, "monitor/customer_detail.html", {
        "c": c,
        "chart": chart,
        "stats": stats,
        "alerts": alerts,
        "period": period,
        "ranges": RANGES,
        "from_rollup": from_rollup,
        "today": today.strftime("%Y-%m-%d"),
        "date_value": period["date"].strftime("%Y-%m-%d") if period["date"] else "",
        "prev_date": ((period["date"] or today) - timedelta(days=1)).strftime("%Y-%m-%d"),
        "next_date": ((period["date"] or today) + timedelta(days=1)).strftime("%Y-%m-%d"),
        "has_next": (period["date"] or today) < today,
        "uptime": f"{uptime:.2f}",
        "nav": "dashboard",
    })


def build_chart(series, period=None, width=900, height=220):
    """Chart traffic in/out, SVG dibuat di server."""
    if not series:
        return None
    # Label sumbu Y ditaruh DI DALAM area plot, tepat di atas garis grid.
    # Kalau ditaruh di luar, lebarnya harus ditebak dari ukuran font —
    # dan font-nya diperbesar lewat CSS di layar sempit, jadi tebakan apa pun
    # akan salah di salah satu ukuran layar. Di dalam, tidak pernah terpotong.
    pad_l, pad_b, pad_t = 10, 26, 26
    plot_w = width - pad_l - 12
    plot_h = height - pad_b - pad_t

    ins = [float(r[1]) if r[1] is not None else None for r in series]
    outs = [float(r[2]) if r[2] is not None else None for r in series]
    vals = [v for v in ins + outs if v is not None]
    if not vals:
        return None
    hi = max(vals) or 1
    hi *= 1.15
    n = len(series)
    step = plot_w / max(1, n - 1)

    def path(vals_list):
        d, started = [], False
        for i, v in enumerate(vals_list):
            if v is None:
                started = False
                continue
            x = pad_l + i * step
            y = pad_t + plot_h - (v / hi) * plot_h
            d.append(f"{'L' if started else 'M'}{x:.1f},{y:.1f}")
            started = True
        return " ".join(d)

    grid = []
    for i in range(5):
        y = pad_t + plot_h - (i / 4) * plot_h
        grid.append({
            "y": f"{y:.1f}",
            "y_label": f"{y - 6:.1f}",
            "label": fmt_bps_axis(hi * i / 4),
        })

    # Rentang lebih dari sehari butuh tanggal, bukan cuma jam
    span = (series[-1][0] - series[0][0]).total_seconds() if n > 1 else 0
    if span > 86400 * 3:
        fmt = "%d/%m"
    elif span > 86400:
        fmt = "%d/%m %H:%M"
    else:
        fmt = "%H:%M"
    n_labels = 5 if n > 40 else 3
    idxs = sorted({round(i * (n - 1) / (n_labels - 1)) for i in range(n_labels)})
    labels = []
    for i in idxs:
        if 0 <= i < n:
            # Label pertama & terakhir di-anchor ke tepi supaya tidak terpotong
            anchor = "start" if i == 0 else ("end" if i == n - 1 else "middle")
            labels.append({
                "x": f"{pad_l + i * step:.1f}",
                "anchor": anchor,
                "t": timezone.localtime(series[i][0]).strftime(fmt),
            })

    return {
        "width": width, "height": height,
        "pad_l": pad_l, "pad_t": pad_t, "plot_w": plot_w, "plot_h": plot_h,
        "in_path": path(ins), "out_path": path(outs),
        "grid": grid, "labels": labels,
        "peak_in": fmt_bps(max([v for v in ins if v is not None], default=None)),
        "peak_out": fmt_bps(max([v for v in outs if v is not None], default=None)),
    }


@login_required
def device_interfaces(request, pk):
    """Browse interface/sesi di perangkat — pengganti snmpwalk manual."""
    d = get_object_or_404(Device, pk=pk)
    rows, error, kind = [], None, d.poll_method

    if request.GET.get("scan") == "1":
        if d.poll_method == "snmp":
            rows, error = discover_snmp_interfaces(d)
        else:
            rows, error = discover_pppoe_sessions(d)

    used = set(
        Customer.objects.filter(device=d, if_index__isnull=False)
        .values_list("if_index", flat=True)
    )
    used_users = set(
        Customer.objects.filter(device=d)
        .exclude(pppoe_username__isnull=True)
        .values_list("pppoe_username", flat=True)
    )
    for r in rows:
        if kind == "snmp":
            r["used"] = r["if_index"] in used
        else:
            r["used"] = r["username"] in used_users

    return render(request, "monitor/device_interfaces.html", {
        "odps": Odp.objects.filter(enabled=True),
        "d": d, "rows": rows, "error": error, "kind": kind,
        "scanned": request.GET.get("scan") == "1",
        "nav": "devices",
    })


@login_required
def alert_list(request):
    qs = Alert.objects.select_related("customer", "device").all()
    show = request.GET.get("show", "open")
    if show == "open":
        qs = qs.filter(resolved_at__isnull=True)
    alerts = list(qs[:200])
    now = timezone.now()
    for a in alerts:
        a.dur = fmt_duration((a.resolved_at or now) - a.started_at)
        a.label = ALERT_TYPE_LABEL.get(a.alert_type, a.alert_type)
    return render(request, "monitor/alerts.html", {
        "alerts": alerts, "show": show, "nav": "alerts",
        "open_count": Alert.objects.filter(resolved_at__isnull=True).count(),
    })


@login_required
def home(request):
    return redirect("dashboard")


# ================================================================ Fase 3
def month_bounds(year, month):
    """Rentang awal-akhir bulan dalam timezone lokal, sadar zona waktu."""
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(_dt(year, month, 1), tz)
    last_day = monthrange(year, month)[1]
    end = timezone.make_aware(
        _dt(year, month, last_day, 23, 59, 59, 999999), tz
    )
    now = timezone.now()
    # Bulan berjalan dihitung sampai sekarang, bukan sampai akhir bulan —
    # kalau tidak, uptime bulan ini akan selalu terlihat bagus.
    return start, min(end, now)


def nms_blind_seconds(start, end):
    """Berapa lama NMS sendiri tidak mencatat apa pun dalam periode ini.

    Ini yang membuat laporan SLA jujur. Tanpa ini, semalam NMS mati terbaca
    sebagai "tidak ada gangguan" alias uptime 100% — bohong yang arahnya
    kebetulan menguntungkan kita sendiri, dan itu jenis bohong yang paling
    berbahaya di laporan yang dikirim ke pelanggan.
    """
    with connection.cursor() as cur:
        cur.execute("SELECT to_regclass('public.nms_heartbeat') IS NOT NULL")
        if not cur.fetchone()[0]:
            return None, 0

        # Jeda antar-detak yang lebih dari 5 menit dianggap NMS sedang mati.
        cur.execute(
            """
            SELECT coalesce(sum(gap), 0), count(*)
            FROM (
                SELECT EXTRACT(EPOCH FROM (
                           time - lag(time) OVER (ORDER BY time)
                       )) AS gap
                FROM nms_heartbeat
                WHERE component = 'collector' AND time >= %s AND time <= %s
            ) x
            WHERE gap > 300
            """,
            (start, end),
        )
        row = cur.fetchone()
        blind, gaps = float(row[0] or 0), int(row[1] or 0)

        # Detak pertama jauh setelah awal periode = NMS belum jalan
        cur.execute(
            """SELECT min(time), max(time) FROM nms_heartbeat
               WHERE component = 'collector' AND time >= %s AND time <= %s""",
            (start, end),
        )
        first, last = cur.fetchone()
        if first is None:
            return None, 0          # belum ada data detak sama sekali
        lead = (first - start).total_seconds()
        if lead > 300:
            blind += lead; gaps += 1
        tail = (end - last).total_seconds()
        if tail > 300:
            blind += tail; gaps += 1

        return blind, gaps


def sla_rows(year, month, include_maintenance):
    """Hitung downtime & uptime tiap pelanggan untuk satu bulan."""
    start, end = month_bounds(year, month)
    window = (end - start).total_seconds()
    if window <= 0:
        return [], start, end, 0

    qs = Alert.objects.filter(
        customer__isnull=False,
        severity="major",
        started_at__lt=end,
    ).filter(Q(resolved_at__isnull=True) | Q(resolved_at__gt=start))
    if not include_maintenance:
        qs = qs.exclude(suppressed=True)

    # Gabungkan interval yang tumpang tindih supaya downtime tidak dihitung
    # dua kali kalau ada beberapa gangguan bersamaan.
    per_cust = {}
    for a in qs.select_related("customer"):
        s = max(a.started_at, start)
        e = min(a.resolved_at or end, end)
        if e > s:
            per_cust.setdefault(a.customer_id, []).append((s, e, a))

    rows = []
    for c in Customer.objects.select_related("device").all():
        spans = sorted(per_cust.get(c.id, []), key=lambda x: x[0])
        merged, down = [], 0.0
        for s, e, _a in spans:
            if merged and s <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))
        for s, e in merged:
            down += (e - s).total_seconds()
        uptime = max(0.0, 100.0 * (1 - down / window))
        rows.append({
            "obj": c,
            "incidents": len(spans),
            "down_secs": down,
            "down_fmt": fmt_duration(timedelta(seconds=down)) if down else "—",
            "uptime": uptime,
            "uptime_fmt": f"{uptime:.3f}",
            "breach": uptime < 99.5,
        })
    rows.sort(key=lambda r: (r["uptime"], r["obj"].name))
    return rows, start, end, window


@login_required
def sla_report(request):
    now = timezone.localtime()
    try:
        year = int(request.GET.get("year", now.year))
        month = int(request.GET.get("month", now.month))
        if not (1 <= month <= 12) or not (2000 <= year <= 2100):
            raise ValueError
    except ValueError:
        year, month = now.year, now.month

    include_mt = request.GET.get("maintenance") == "1"
    rows, start, end, window = sla_rows(year, month, include_mt)
    blind, blind_gaps = nms_blind_seconds(start, end)

    if request.GET.get("format") == "csv":
        resp = HttpResponse(content_type="text/csv; charset=utf-8")
        resp["Content-Disposition"] = (
            f'attachment; filename="sla-{year}-{month:02d}.csv"'
        )
        resp.write("\ufeff")  # BOM supaya Excel membaca UTF-8 dengan benar
        w = csv.writer(resp)
        if blind:
            # Peringatan ini ikut ke Excel juga. Angka yang menyesatkan tidak
            # boleh lebih mudah disebar daripada peringatannya.
            w.writerow([f"PERINGATAN: NMS tidak mencatat selama "
                        f"{fmt_duration(timedelta(seconds=blind))} "
                        f"({100*blind/window:.1f}% periode). Uptime di bawah "
                        f"ini lebih tinggi dari kenyataan."])
        w.writerow(["ID layanan", "Pelanggan", "Perangkat", "Titik monitor",
                    "Jumlah gangguan", "Total down (detik)", "Total down",
                    "Uptime (%)"])
        for r in rows:
            c = r["obj"]
            w.writerow([
                c.service_id or "", c.name, c.device.name,
                c.if_name or c.pppoe_username or "",
                r["incidents"], int(r["down_secs"]), r["down_fmt"],
                f"{r['uptime']:.3f}",
            ])
        return resp

    months = [
        (i, _dt(2000, i, 1).strftime("%B")) for i in range(1, 13)
    ]
    id_months = ["Januari", "Februari", "Maret", "April", "Mei", "Juni",
                 "Juli", "Agustus", "September", "Oktober", "November",
                 "Desember"]
    months = [(i + 1, m) for i, m in enumerate(id_months)]

    return render(request, "monitor/sla.html", {
        "rows": rows,
        "year": year, "month": month,
        "month_name": id_months[month - 1],
        "months": months,
        "years": range(now.year - 2, now.year + 1),
        "start": start, "end": end,
        "window_fmt": fmt_duration(timedelta(seconds=window)),
        "include_mt": include_mt,
        "breaches": sum(1 for r in rows if r["breach"]),
        "blind_fmt": (fmt_duration(timedelta(seconds=blind))
                      if blind else None),
        "blind_pct": f"{100 * blind / window:.1f}" if blind and window else None,
        "blind_gaps": blind_gaps,
        "blind_unknown": blind is None,
        "nav": "sla",
    })


@login_required
def customer_live(request, pk):
    """Potongan grafik & statistik terbaru, untuk refresh tanpa reload.

    Sengaja memakai ulang parse_period/fetch_series/usage_stats/build_chart
    yang sama persis dengan customer_detail, lalu merender partial yang sama.
    Kalau logikanya diduplikasi di JavaScript, cepat atau lambat grafik live
    akan berbeda dari grafik hasil reload — dan selisih semacam itu susah
    dilacak.
    """
    c = get_object_or_404(Customer.objects.select_related("device"), pk=pk)
    period = parse_period(request)
    series, from_rollup = fetch_series(c.id, period)
    if c.flip_arah:
        series = flip_series(series)
    stats = usage_stats(c.id, period, series, from_rollup, c.flip_arah)
    chart = build_chart(series, period)

    ctx = {"c": c, "chart": chart, "stats": stats, "period": period,
           "from_rollup": from_rollup}
    return JsonResponse({
        "stats_html": (render_to_string("monitor/_stats.html", ctx, request)
                       if stats else ""),
        "chart_html": render_to_string("monitor/_chart.html", ctx, request),
        "has_stats": stats is not None,
        "status": c.status,
        "updated": timezone.localtime().strftime("%H:%M:%S"),
        "latest": (timezone.localtime(series[-1][0]).strftime("%H:%M:%S")
                   if series else None),
    })


@login_required
@require_POST
def alert_ack(request, pk):
    """Tandai gangguan sedang ditangani — menghentikan pengingat eskalasi."""
    a = get_object_or_404(Alert, pk=pk)
    if a.ack_at is None:
        a.ack_at = timezone.now()
        a.ack_by = request.user.get_username()
        a.save(update_fields=["ack_at", "ack_by"])
        messages.success(request, f"{a.subject} ditandai sedang ditangani.")
    return redirect(request.POST.get("next") or "alert_list")


@login_required
def device_list(request):
    devices = list(Device.objects.all())
    now = timezone.now()
    open_dev = {
        a.device_id: a
        for a in Alert.objects.filter(resolved_at__isnull=True,
                                      device__isnull=False)
    }
    rows = []
    for d in devices:
        alert = open_dev.get(d.id)
        if not d.enabled:
            state = "disabled"
        elif d.status == "down" or alert:
            state = "down"
        elif d.status == "up":
            state = "up"
        else:
            state = "unknown"
        rows.append({
            "obj": d,
            "state": state,
            "customers": d.customer_set.count(),
            "down_for": fmt_duration(now - alert.started_at) if alert else None,
            "last_ok": d.last_ok_at,
        })
    rows.sort(key=lambda r: ({"down": 0, "unknown": 1, "up": 2,
                              "disabled": 3}[r["state"]], r["obj"].name))
    return render(request, "monitor/devices.html", {
        "rows": rows, "nav": "devices",
        "active_mw": MaintenanceWindow.objects.filter(
            starts_at__lte=now, ends_at__gte=now
        ).select_related("device", "customer"),
    })


@login_required
def customer_import(request):
    """Impor pelanggan dari CSV — semua berhasil, atau tidak sama sekali."""
    if not request.user.has_perm("monitor.add_customer"):
        return redirect("dashboard")

    ctx = {"nav": "dashboard", "kolom": importer.KOLOM, "contoh": importer.CONTOH}

    if request.method != "POST":
        return render(request, "monitor/import.html", ctx)

    teks = ""
    if request.FILES.get("berkas"):
        raw = request.FILES["berkas"].read()
        for enc in ("utf-8-sig", "utf-8", "latin-1"):
            try:
                teks = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
    else:
        teks = request.POST.get("teks", "")

    if not teks.strip():
        ctx["errors"] = ["Belum ada file atau teks CSV yang diisi."]
        return render(request, "monitor/import.html", ctx)

    objek, errors = importer.parse_csv(teks)
    ctx.update({"errors": errors, "objek": objek, "teks": teks})

    if errors:
        # Satu baris rusak = tidak ada yang masuk. Impor setengah jalan
        # meninggalkan keadaan yang susah dirapikan.
        ctx["gagal_total"] = True
        return render(request, "monitor/import.html", ctx)

    if request.POST.get("aksi") != "simpan":
        ctx["pratinjau"] = True
        return render(request, "monitor/import.html", ctx)

    with transaction.atomic():
        for c in objek:
            c.save()
    messages.success(request, f"{len(objek)} pelanggan berhasil diimpor.")
    return redirect("dashboard")


@login_required
def odp_list(request):
    """Papan status ODP. Yang paling berguna di sini: lokasi."""
    odps = list(Odp.objects.select_related("device").filter(enabled=True))
    open_alerts = {
        a.odp_id: a for a in Alert.objects.filter(
            odp_id__isnull=False, resolved_at__isnull=True)
    }
    now = timezone.now()

    counts = Customer.objects.filter(enabled=True, odp__isnull=False).values(
        "odp_id", "status")
    per_odp = {}
    for row in counts:
        d = per_odp.setdefault(row["odp_id"], {"total": 0, "down": 0})
        d["total"] += 1
        if row["status"] == "down":
            d["down"] += 1

    rows = []
    for o in odps:
        c = per_odp.get(o.id, {"total": 0, "down": 0})
        a = open_alerts.get(o.id)
        # ODP dengan pelanggan terlalu sedikit tidak akan pernah terdeteksi.
        # Lebih baik dikatakan terus terang daripada diam-diam tidak dipantau.
        cukup = c["total"] >= config_hint.ODP_MIN_DOWN
        rows.append({
            "obj": o,
            "total": c["total"],
            "down": c["down"],
            "pct": round(100 * c["down"] / c["total"]) if c["total"] else 0,
            "alert": a,
            "down_for": fmt_duration(now - a.started_at) if a else None,
            "cukup": cukup,
            "state": ("down" if a else
                      "unknown" if not c["total"] else
                      "partial" if c["down"] else "up"),
        })
    rows.sort(key=lambda r: (r["state"] != "down", -r["pct"], r["obj"].name))

    return render(request, "monitor/odp_list.html", {
        "rows": rows,
        "nav": "odp",
        "min_down": config_hint.ODP_MIN_DOWN,
        "ratio_pct": int(config_hint.ODP_DOWN_RATIO * 100),
        "down_count": sum(1 for r in rows if r["state"] == "down"),
    })


@login_required
@require_POST
def bulk_register(request, pk):
    """Daftarkan banyak interface sekaligus dari halaman penemuan.

    Sama seperti impor CSV: semua berhasil atau tidak sama sekali. Separuh
    jalan meninggalkan keadaan yang susah dirapikan.
    """
    if not request.user.has_perm("monitor.add_customer"):
        return redirect("dashboard")

    dev = get_object_or_404(Device, pk=pk)
    dipilih = request.POST.getlist("pilih")
    if not dipilih:
        messages.error(request, "Belum ada interface yang dicentang.")
        return redirect("device_interfaces", pk=pk)

    odp_id = request.POST.get("odp") or None
    arah = request.POST.get("arah") or "ke_pelanggan"
    try:
        ambang = int(request.POST.get("ambang") or 0) or None
    except ValueError:
        ambang = None
    degradasi = request.POST.get("degradasi") == "1"

    objek, errors = [], []
    for token in dipilih:
        # token: "<ifIndex>|<ifName>"
        idx, _, nama_if = token.partition("|")
        try:
            idx = int(idx)
        except ValueError:
            errors.append(f"ifIndex tidak valid: {idx}")
            continue

        c = Customer(
            name=request.POST.get(f"nama_{idx}", "").strip() or nama_if,
            service_id=request.POST.get(f"sid_{idx}", "").strip() or None,
            device=dev,
            monitor_type="snmp_if",
            if_index=idx,
            if_name=nama_if or None,
            if_direction=arah,
            baseline_enabled=degradasi,
            odp_id=odp_id or None,
        )
        if ambang:
            c.threshold_bps = ambang
        try:
            c.full_clean(exclude=["status", "status_changed_at"])
        except ValidationError as e:
            for f, pesan in e.message_dict.items():
                errors.append(f"{c.name} ({nama_if}): {f} — {' '.join(pesan)}")
            continue
        objek.append(c)

    if errors:
        for e in errors[:8]:
            messages.error(request, e)
        if len(errors) > 8:
            messages.error(request, f"...dan {len(errors) - 8} masalah lain.")
        messages.error(request, "Tidak ada yang didaftarkan — perbaiki dulu.")
        return redirect("device_interfaces", pk=pk)

    with transaction.atomic():
        for c in objek:
            c.save()
    messages.success(request, f"{len(objek)} pelanggan berhasil didaftarkan.")
    return redirect("dashboard")

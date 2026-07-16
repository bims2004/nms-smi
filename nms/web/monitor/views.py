"""View dashboard NMS."""
from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.db import connection
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .discovery import discover_pppoe_sessions, discover_snmp_interfaces
from .models import ALERT_TYPE_LABEL, Alert, Customer, Device

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

        rows.append({
            "obj": c,
            "state": state,
            "in_bps": fmt_bps(s["in_bps"]) if s else "—",
            "out_bps": fmt_bps(s["out_bps"]) if s else "—",
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
    return JsonResponse({
        "counts": counts,
        "updated": timezone.localtime().strftime("%H:%M:%S"),
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


@login_required
def customer_detail(request, pk):
    c = get_object_or_404(Customer.objects.select_related("device"), pk=pk)

    try:
        hours = max(1, min(168, int(request.GET.get("hours", 6))))
    except ValueError:
        hours = 6
    bucket = "1 minute" if hours <= 6 else ("5 minutes" if hours <= 24 else "30 minutes")

    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT time_bucket(%s::interval, time) AS b,
                   avg(in_bps) AS in_bps, avg(out_bps) AS out_bps,
                   bool_and(link_up) AS link_up
            FROM traffic_samples
            WHERE customer_id = %s AND time > now() - %s::interval
            GROUP BY b ORDER BY b
            """,
            (bucket, c.id, f"{hours} hours"),
        )
        series = cur.fetchall()

    chart = build_chart(series)

    alerts = list(Alert.objects.filter(customer=c)[:20])
    for a in alerts:
        a.dur = fmt_duration(a.duration) if a.resolved_at else "berlangsung"

    # Uptime kasar dari akumulasi durasi gangguan
    since = timezone.now() - timedelta(hours=hours)
    down_secs = 0.0
    for a in Alert.objects.filter(customer=c, started_at__gte=since):
        end = a.resolved_at or timezone.now()
        down_secs += (end - a.started_at).total_seconds()
    window = hours * 3600
    uptime = max(0.0, 100.0 * (1 - down_secs / window)) if window else 100.0

    return render(request, "monitor/customer_detail.html", {
        "c": c,
        "chart": chart,
        "alerts": alerts,
        "hours": hours,
        "uptime": f"{uptime:.2f}",
        "nav": "dashboard",
    })


def build_chart(series, width=900, height=220):
    """Chart traffic in/out, SVG dibuat di server."""
    if not series:
        return None
    pad_l, pad_b, pad_t = 46, 26, 10
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
            "y_label": f"{y + 4:.1f}",
            "label": fmt_bps_axis(hi * i / 4),
        })

    labels = []
    for i in (0, n // 2, n - 1):
        if 0 <= i < n:
            # Label pertama & terakhir di-anchor ke tepi supaya tidak terpotong
            anchor = "start" if i == 0 else ("end" if i == n - 1 else "middle")
            labels.append({
                "x": f"{pad_l + i * step:.1f}",
                "anchor": anchor,
                "t": timezone.localtime(series[i][0]).strftime("%H:%M"),
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
import csv
from calendar import monthrange
from datetime import datetime as _dt

from django.contrib import messages
from django.db.models import Q
from django.http import HttpResponse
from django.views.decorators.http import require_POST

from .models import MaintenanceWindow


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

    if request.GET.get("format") == "csv":
        resp = HttpResponse(content_type="text/csv; charset=utf-8")
        resp["Content-Disposition"] = (
            f'attachment; filename="sla-{year}-{month:02d}.csv"'
        )
        resp.write("\ufeff")  # BOM supaya Excel membaca UTF-8 dengan benar
        w = csv.writer(resp)
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
        "nav": "sla",
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

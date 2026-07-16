"""Notifikasi Telegram via Bot API."""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from . import config

log = logging.getLogger(__name__)

ALERT_LABEL = {
    "link_down": "Link down",
    "session_down": "Sesi PPPoE putus",
    "traffic_zero": "Traffic nol",
    "traffic_degraded": "Traffic turun drastis",
    "device_down": "Perangkat tidak merespon",
}


def _fmt_time(dt: datetime) -> str:
    return dt.astimezone(ZoneInfo(config.TZ_DISPLAY)).strftime(
        "%d-%m-%Y %H:%M:%S WIB"
    )


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}j {m}m {s}dt"
    if m:
        return f"{m}m {s}dt"
    return f"{s}dt"


def send(text: str) -> bool:
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        log.warning("Telegram belum dikonfigurasi, notif dilewati:\n%s", text)
        return False
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            json={
                "chat_id": config.TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
            },
            timeout=10,
        )
        if r.status_code != 200:
            log.error("Telegram error %s: %s", r.status_code, r.text)
            return False
        return True
    except requests.RequestException as e:
        log.error("Telegram request gagal: %s", e)
        return False


def notify_down(customer, device, alert_type, started_at, severity="major") -> bool:
    icon = "🔴" if severity == "major" else "🟠"
    head = "DOWN" if severity == "major" else "DEGRADASI"
    text = (
        f"{icon} <b>{head}</b> — {customer['name']}\n"
        f"ID layanan : {customer.get('service_id') or '-'}\n"
        f"Perangkat  : {device['name']} ({device['ip']})\n"
        f"Jenis      : {ALERT_LABEL.get(alert_type, alert_type)}\n"
        f"Sejak      : {_fmt_time(started_at)}"
    )
    return send(text)


def notify_recovery(customer, device, alert_type, started_at, resolved_at) -> bool:
    dur = _fmt_duration((resolved_at - started_at).total_seconds())
    text = (
        f"🟢 <b>PULIH</b> — {customer['name']}\n"
        f"ID layanan : {customer.get('service_id') or '-'}\n"
        f"Perangkat  : {device['name']} ({device['ip']})\n"
        f"Jenis      : {ALERT_LABEL.get(alert_type, alert_type)}\n"
        f"Lama down  : {dur}\n"
        f"Pulih pada : {_fmt_time(resolved_at)}"
    )
    return send(text)


def notify_device_down(device, started_at) -> bool:
    """Satu perangkat mati = satu pesan, bukan satu pesan per pelanggan."""
    text = (
        f"🛑 <b>PERANGKAT TIDAK MERESPON</b>\n"
        f"Perangkat : {device['name']} ({device['ip']})\n"
        f"Sejak     : {_fmt_time(started_at)}\n\n"
        f"Status pelanggan di bawah perangkat ini tidak dapat dinilai "
        f"selama perangkat tidak merespon."
    )
    return send(text)


def notify_device_recovery(device, started_at, resolved_at) -> bool:
    dur = _fmt_duration((resolved_at - started_at).total_seconds())
    text = (
        f"✅ <b>PERANGKAT KEMBALI NORMAL</b>\n"
        f"Perangkat  : {device['name']} ({device['ip']})\n"
        f"Lama down  : {dur}\n"
        f"Pulih pada : {_fmt_time(resolved_at)}"
    )
    return send(text)


def notify_escalation(label, service_id, dev_name, dev_ip,
                      alert_type, started_at) -> bool:
    dur = _fmt_duration(
        (datetime.now(ZoneInfo("UTC")) - started_at).total_seconds()
    )
    text = (
        f"⏰ <b>BELUM DITANGANI {dur}</b>\n"
        f"{label}"
        + (f" ({service_id})" if service_id else "")
        + f"\nPerangkat : {dev_name} ({dev_ip})\n"
        f"Jenis     : {ALERT_LABEL.get(alert_type, alert_type)}\n"
        f"Sejak     : {_fmt_time(started_at)}\n\n"
        f"Tandai sudah ditangani di dashboard supaya pengingat berhenti."
    )
    return send(text)

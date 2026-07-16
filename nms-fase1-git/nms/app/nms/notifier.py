"""Notifikasi Telegram via Bot API."""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from . import config

log = logging.getLogger(__name__)

ALERT_LABEL = {
    "link_down": "Link Down",
    "session_down": "PPPoE Session Down",
    "traffic_zero": "Traffic Zero",
}


def _fmt_time(dt: datetime) -> str:
    tz = ZoneInfo(config.TZ_DISPLAY)
    return dt.astimezone(tz).strftime("%d-%m-%Y %H:%M:%S WIB")


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}j {m}m {s}d"
    if m:
        return f"{m}m {s}d"
    return f"{s}d"


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


def notify_down(customer: dict, device: dict, alert_type: str,
                started_at: datetime) -> bool:
    label = ALERT_LABEL.get(alert_type, alert_type)
    text = (
        f"🔴 <b>DOWN</b> — {customer['name']}\n"
        f"Service ID : {customer.get('service_id') or '-'}\n"
        f"Device     : {device['name']} ({device['ip']})\n"
        f"Tipe       : {label}\n"
        f"Sejak      : {_fmt_time(started_at)}"
    )
    return send(text)


def notify_recovery(customer: dict, device: dict, alert_type: str,
                    started_at: datetime, resolved_at: datetime) -> bool:
    label = ALERT_LABEL.get(alert_type, alert_type)
    dur = _fmt_duration((resolved_at - started_at).total_seconds())
    text = (
        f"🟢 <b>RECOVERY</b> — {customer['name']}\n"
        f"Service ID : {customer.get('service_id') or '-'}\n"
        f"Device     : {device['name']} ({device['ip']})\n"
        f"Tipe       : {label}\n"
        f"Durasi down: {dur}\n"
        f"Pulih pada : {_fmt_time(resolved_at)}"
    )
    return send(text)

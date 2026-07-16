"""Konfigurasi terpusat, dibaca dari environment variables."""
import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# Database
DB_HOST = os.environ.get("DB_HOST", "db")
DB_PORT = _int("DB_PORT", 5432)
DB_NAME = os.environ.get("POSTGRES_DB", "nms")
DB_USER = os.environ.get("POSTGRES_USER", "nms")
DB_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "nmspass")

# Collector
POLL_INTERVAL = _int("POLL_INTERVAL", 60)
SNMP_TIMEOUT = _int("SNMP_TIMEOUT", 5)
SNMP_RETRIES = _int("SNMP_RETRIES", 1)

# Alert engine
ALERT_CHECK_INTERVAL = _int("ALERT_CHECK_INTERVAL", 60)
CONSECUTIVE_DOWN_SAMPLES = _int("CONSECUTIVE_DOWN_SAMPLES", 3)

# Telegram
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Timezone tampilan notifikasi
TZ_DISPLAY = os.environ.get("TZ_DISPLAY", "Asia/Jakarta")

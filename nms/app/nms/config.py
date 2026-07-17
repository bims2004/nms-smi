"""Konfigurasi terpusat, dibaca dari environment variables."""
import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
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

# Timezone tampilan notifikasi & perhitungan baseline
TZ_DISPLAY = os.environ.get("TZ_DISPLAY", "Asia/Jakarta")

# Fase 3
# Berapa kali polling gagal berturut-turut sebelum perangkat dianggap mati
DEVICE_FAIL_THRESHOLD = _int("DEVICE_FAIL_THRESHOLD", 3)
# Kirim pengingat kalau gangguan major belum di-ack setelah sekian menit.
# 0 = matikan eskalasi.
ESCALATION_MINUTES = _int("ESCALATION_MINUTES", 30)
# Seberapa sering baseline dihitung ulang (jam)
BASELINE_REFRESH_HOURS = _int("BASELINE_REFRESH_HOURS", 6)

# Berapa perangkat di-poll bersamaan. Perangkat mati memblokir selama
# SNMP_TIMEOUT x (retries+1); tanpa paralel, beberapa perangkat mati sekaligus
# membuat siklus meleset dari POLL_INTERVAL. Naikkan kalau perangkatnya banyak.
POLL_WORKERS = _int("POLL_WORKERS", 8)

# --- Korelasi ODP ---
# ODP tidak punya perangkat aktif di dalamnya — cuma splitter pasif. Tidak
# bisa di-ping. Satu-satunya bukti ODP bermasalah adalah pelanggan di
# bawahnya mati BERSAMAAN.
#
# Berapa pelanggan minimal harus mati sebelum ODP dituduh. Di bawah ini
# buktinya terlalu lemah: 2 dari 2 pelanggan mati bisa saja kebetulan.
ODP_MIN_DOWN = _int("ODP_MIN_DOWN", 3)

# Berapa bagian pelanggan ODP yang harus mati. 0.75 berarti 3 dari 4.
# Dinaikkan = lebih jarang salah tuduh, tapi ODP yang setengah mati terlewat.
# Diturunkan = lebih peka, tapi beberapa pelanggan yang kebetulan mati
# bersamaan bisa memicu tuduhan palsu dan mengirim teknisi ke lapangan
# untuk sesuatu yang tidak ada.
ODP_DOWN_RATIO = _float("ODP_DOWN_RATIO", 0.75)

# URL yang di-ping tiap siklus sebagai bukti NMS masih hidup (dead man's
# switch). Kalau ping berhenti, layanan di seberang yang memberi tahu — karena
# NMS yang mati tidak bisa memberi tahu tentang dirinya sendiri.
# Kosongkan untuk mematikan. Cocok dengan healthchecks.io, Uptime Kuma, dsb.
HEARTBEAT_URL = os.environ.get("HEARTBEAT_URL", "").strip()

"""Koneksi PostgreSQL/TimescaleDB dengan retry sederhana."""
import logging
import time

import psycopg2
import psycopg2.extras

from . import config

log = logging.getLogger(__name__)


def get_conn(max_wait: int = 60):
    """Buat koneksi baru, retry sampai max_wait detik (untuk startup race)."""
    deadline = time.time() + max_wait
    last_err = None
    while time.time() < deadline:
        try:
            conn = psycopg2.connect(
                host=config.DB_HOST,
                port=config.DB_PORT,
                dbname=config.DB_NAME,
                user=config.DB_USER,
                password=config.DB_PASSWORD,
                connect_timeout=5,
            )
            conn.autocommit = True
            return conn
        except psycopg2.OperationalError as e:
            last_err = e
            log.warning("DB belum siap, retry... (%s)", e)
            time.sleep(3)
    raise RuntimeError(f"Gagal konek DB: {last_err}")


def dict_cursor(conn):
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

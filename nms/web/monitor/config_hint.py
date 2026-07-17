"""Ambang yang dipakai alerter, dibaca juga oleh dashboard.

Nilainya harus sama dengan app/nms/config.py. Dua container terpisah tidak
bisa saling impor, jadi keduanya membaca environment variable yang sama.
"""
import os


def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


ODP_MIN_DOWN = _int("ODP_MIN_DOWN", 3)
ODP_DOWN_RATIO = _float("ODP_DOWN_RATIO", 0.75)

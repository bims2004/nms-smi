"""Enkripsi kredensial perangkat saat disimpan.

Ancaman yang ditangani, dengan jujur:

MELINDUNGI dari — backup database yang bocor. Dump `.sql.gz` sering disalin ke
NAS, dikirim lewat chat, atau menumpuk di folder yang izinnya longgar. Tanpa
ini, satu file backup = password semua perangkat jaringan.

TIDAK melindungi dari — orang yang sudah masuk ke servernya. Kuncinya ada di
`.env` di server yang sama. Siapa pun yang bisa membaca `.env` bisa membuka
ciphertext-nya. Ini bukan kelemahan yang bisa ditambal dengan menaruh kunci di
tempat lain di server yang sama — itu cuma memindahkan masalah sambil terlihat
lebih aman.

Yang tetap perlu dilakukan, dan tidak tergantikan oleh enkripsi ini: pakai user
read-only di perangkat, bukan admin.

    /user group add name=nms-ro policy=read,api
    /user add name=nms group=nms-ro password=...

Kalau kredensial ini bocor, yang didapat penyerang hanyalah kemampuan membaca
counter interface — bukan mengubah konfigurasi.
"""
import base64
import hashlib
import logging
import os

log = logging.getLogger(__name__)

PREFIX = "enc:v1:"


def _key():
    """Turunkan kunci Fernet dari NMS_SECRET_KEY."""
    raw = os.environ.get("NMS_SECRET_KEY", "").strip()
    if not raw:
        return None
    # Fernet minta 32 byte ter-base64. Kunci apa pun dari .env diturunkan
    # lewat hash supaya panjangnya selalu pas, tanpa memaksa orang membuat
    # kunci berformat khusus.
    digest = hashlib.sha256(raw.encode()).digest()
    return base64.urlsafe_b64encode(digest)


def tersedia() -> bool:
    if _key() is None:
        return False
    try:
        import cryptography  # noqa: F401
        return True
    except ImportError:
        return False


def enkripsi(teks):
    """Kembalikan ciphertext berprefiks. Kalau tidak bisa, kembalikan apa adanya."""
    if not teks or teks.startswith(PREFIX):
        return teks
    k = _key()
    if k is None:
        return teks
    try:
        from cryptography.fernet import Fernet
        return PREFIX + Fernet(k).encrypt(teks.encode()).decode()
    except Exception:
        log.exception("Enkripsi gagal — nilai disimpan apa adanya")
        return teks


def dekripsi(teks):
    """Kebalikan enkripsi. Nilai tanpa prefiks dianggap belum terenkripsi."""
    if not teks or not teks.startswith(PREFIX):
        return teks
    k = _key()
    if k is None:
        # Ini terjadi kalau NMS_SECRET_KEY hilang atau berubah. Diam-diam
        # mengembalikan ciphertext akan membuat polling gagal dengan pesan
        # "password salah" yang menyesatkan berjam-jam.
        raise RuntimeError(
            "Ada kredensial terenkripsi di database tapi NMS_SECRET_KEY "
            "kosong. Kembalikan kunci lamanya ke .env, atau isi ulang "
            "password perangkat lewat dashboard."
        )
    try:
        from cryptography.fernet import Fernet
        return Fernet(k).decrypt(teks[len(PREFIX):].encode()).decode()
    except Exception as e:
        raise RuntimeError(
            "Kredensial tidak bisa didekripsi — NMS_SECRET_KEY kemungkinan "
            f"sudah berubah dari saat password disimpan. ({e})"
        ) from e

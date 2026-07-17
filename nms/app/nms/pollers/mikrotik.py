"""Poller Mikrotik API (librouteros) untuk pelanggan PPPoE.

Kenapa API dan bukan SNMP untuk PPPoE:
- ifIndex interface PPPoE dinamis, berubah setiap reconnect.
- Lewat API kita tracking berdasarkan username sesi (/ppp/active) dan
  nama interface dinamis (default: "<pppoe-username>").

rx-byte  = diterima router dari pelanggan (upload pelanggan)
tx-byte  = dikirim router ke pelanggan (download pelanggan)
"""
import logging

from librouteros import connect

from .. import crypto

log = logging.getLogger(__name__)


def norm_user(v) -> str:
    """Samakan bentuk username dari API dan dari database.

    librouteros mengubah setiap nilai yang tampak seperti angka menjadi int:

        =name=12345         -> 12345        (int)
        =name=081234567890  -> 81234567890  (int, NOL DI DEPAN HILANG)

    Akibatnya `"12345" in {12345}` bernilai False, dan pelanggan dengan
    username angka akan selamanya terbaca down tanpa pesan error apa pun —
    kegagalan yang diam, jenis yang paling lama tidak ketahuan.

    Nol di depan sudah dihancurkan di dalam librouteros dan tidak bisa
    dikembalikan. Supaya pencocokan tetap jalan, nilai dari database
    dilewatkan transformasi yang sama persis, jadi keduanya "rusak" dengan
    cara yang identik dan tetap bertemu.

    Kompromi yang perlu diketahui: username "01" dan "1" jadi dianggap sama.
    Kalau di jaringanmu itu dua pelanggan berbeda, jangan pakai username
    angka murni.
    """
    s = str(v).strip()
    try:
        return str(int(s))
    except (ValueError, TypeError):
        return s


def poll_pppoe_customers(device: dict, customers: list) -> dict:
    """Poll semua customer pppoe pada satu router Mikrotik.

    Returns: {customer_id: {"in_octets", "out_octets", "link_up"}}
    - link_up False = sesi PPPoE tidak ada di /ppp/active.
    - Kalau sesi tidak aktif, counter di-set None.
    """
    results = {}
    api = None
    try:
        api = connect(
            host=str(device["ip"]),
            username=device["api_username"],
            # Didekripsi di sini, sedekat mungkin dengan pemakaian —
            # supaya plaintext-nya tidak berkeliaran di struktur data.
            password=crypto.dekripsi(device["api_password"]) or "",
            port=int(device["api_port"] or 8728),
            timeout=10,
        )

        # Set username yang sedang aktif. Dinormalkan karena librouteros
        # mengubah username angka jadi int — lihat norm_user().
        active_users = set()
        for row in api.path("ppp", "active"):
            name = row.get("name")
            if name is not None and str(name).strip():
                active_users.add(norm_user(name))

        # Ambil counter semua interface sekali jalan, lalu match di memory
        iface_by_name = {}
        for row in api.path("interface"):
            iface_by_name[str(row.get("name", ""))] = row

        for c in customers:
            user = c["pppoe_username"] or ""
            session_up = norm_user(user) in active_users

            # Nama interface dinamis default Mikrotik: <pppoe-username>.
            # Nama itu mengandung "<" jadi tidak pernah jadi int, tapi
            # if_name yang diisi manual bisa saja angka murni.
            iface = (
                iface_by_name.get(f"<pppoe-{user}>")
                or iface_by_name.get(str(user))
                or iface_by_name.get(str(c.get("if_name") or ""))
            )

            if not session_up or iface is None:
                results[c["id"]] = {
                    "in_octets": None,
                    "out_octets": None,
                    "link_up": False,
                }
                continue

            try:
                rx = int(iface.get("rx-byte", 0))
                tx = int(iface.get("tx-byte", 0))
            except (TypeError, ValueError):
                rx, tx = 0, 0

            results[c["id"]] = {
                # in = ke arah pelanggan (download) = tx router
                "in_octets": tx,
                "out_octets": rx,
                "link_up": True,
            }

    except Exception as e:
        log.error("Mikrotik API %s gagal: %s", device["ip"], e)
    finally:
        if api is not None:
            try:
                api.close()
            except Exception:
                pass

    return results

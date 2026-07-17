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

log = logging.getLogger(__name__)


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
            port=device["api_port"] or 8728,
            timeout=10,
        )

        # Set username yang sedang aktif
        active_users = set()
        for row in api.path("ppp", "active"):
            name = row.get("name")
            if name:
                active_users.add(name)

        # Ambil counter semua interface sekali jalan, lalu match di memory
        iface_by_name = {}
        for row in api.path("interface"):
            iface_by_name[row.get("name", "")] = row

        for c in customers:
            user = c["pppoe_username"]
            session_up = user in active_users

            # Nama interface dinamis default Mikrotik: <pppoe-username>
            iface = (
                iface_by_name.get(f"<pppoe-{user}>")
                or iface_by_name.get(user)
                or iface_by_name.get(c.get("if_name") or "")
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

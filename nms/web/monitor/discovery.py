"""Penemuan interface dari perangkat, dipakai oleh halaman 'Cari interface'
supaya NOC tidak perlu snmpwalk manual untuk mencari ifIndex.
"""
import logging

from . import crypto

log = logging.getLogger(__name__)

OID_IF_NAME = "1.3.6.1.2.1.31.1.1.1.1"   # ifName
OID_IF_ALIAS = "1.3.6.1.2.1.31.1.1.1.18"  # ifAlias (deskripsi di device)
OID_IF_OPER = "1.3.6.1.2.1.2.2.1.8"       # ifOperStatus


def discover_snmp_interfaces(device, timeout=5, retries=1):
    """Walk ifName/ifAlias/ifOperStatus. Return (list, error_message)."""
    from pysnmp.hlapi import (
        CommunityData, ContextData, ObjectIdentity, ObjectType,
        SnmpEngine, UdpTransportTarget, nextCmd,
    )

    def walk(oid):
        out = {}
        engine = SnmpEngine()
        iterator = nextCmd(
            engine,
            CommunityData(device.snmp_community or "public", mpModel=1),
            UdpTransportTarget(
                (str(device.ip), device.snmp_port or 161),
                timeout=timeout, retries=retries,
            ),
            ContextData(),
            ObjectType(ObjectIdentity(oid)),
            lexicographicMode=False,
        )
        for error_indication, error_status, _, var_binds in iterator:
            if error_indication:
                raise RuntimeError(str(error_indication))
            if error_status:
                raise RuntimeError(error_status.prettyPrint())
            for name, val in var_binds:
                idx = str(name).rsplit(".", 1)[-1]
                out[idx] = val.prettyPrint()
        return out

    try:
        names = walk(OID_IF_NAME)
    except Exception as e:
        return [], (
            f"Tidak bisa membaca interface dari {device.ip}: {e}. "
            "Periksa community string, ACL SNMP di perangkat, dan firewall UDP/161."
        )

    if not names:
        return [], (
            f"{device.ip} menjawab tapi tidak mengirim daftar interface. "
            "Perangkat mungkin tidak mendukung IF-MIB ifName."
        )

    try:
        aliases = walk(OID_IF_ALIAS)
    except Exception:
        aliases = {}
    try:
        opers = walk(OID_IF_OPER)
    except Exception:
        opers = {}

    rows = []
    for idx, name in names.items():
        rows.append({
            "if_index": int(idx),
            "if_name": name,
            "alias": aliases.get(idx, ""),
            "oper_up": opers.get(idx) == "1",
        })
    rows.sort(key=lambda r: r["if_index"])
    return rows, None


def teks(v) -> str:
    """Paksa nilai dari librouteros menjadi teks.

    librouteros mengubah setiap nilai yang tampak seperti angka menjadi int:
    username "12345" jadi int, uptime "45" jadi int. Mencampur int dan str
    lalu mengurutkannya melempar TypeError:

        '<' not supported between instances of 'int' and 'str'

    Ini bukan masalah jaringan, tapi sempat terbaca begitu karena tertangkap
    oleh except yang sama dengan kegagalan koneksi.
    """
    if v is None:
        return ""
    if isinstance(v, bool):          # librouteros memetakan yes/no ke bool
        return "ya" if v else "tidak"
    return str(v)


def discover_pppoe_sessions(device):
    """Ambil daftar sesi PPPoE aktif dari Mikrotik. Return (list, error)."""
    from librouteros import connect

    api = None
    try:
        api = connect(
            host=str(device.ip),
            username=device.api_username or "",
            # Password tersimpan terenkripsi. Tanpa dekripsi di sini,
            # yang terkirim ke Mikrotik adalah ciphertext-nya.
            password=crypto.dekripsi(device.api_password) or "",
            port=int(device.api_port or 8728),
            timeout=10,
        )
        rows = []
        for row in api.path("ppp", "active"):
            rows.append({
                "username": teks(row.get("name")),
                "address": teks(row.get("address")),
                "uptime": teks(row.get("uptime")),
                "caller_id": teks(row.get("caller-id")),
            })
        rows.sort(key=lambda r: r["username"])
        return rows, None
    except Exception as e:
        return [], (
            f"Tidak bisa terhubung ke API {device.ip}: {e}. "
            "Periksa user/password API, '/ip service set api disabled=no', "
            "dan address list yang mengizinkan IP server ini."
        )
    finally:
        if api is not None:
            try:
                api.close()
            except Exception:
                pass

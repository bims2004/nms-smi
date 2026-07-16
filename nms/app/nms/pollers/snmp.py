"""Poller SNMP v2c untuk pelanggan dedicated (interface fisik switch/router).

Menggunakan counter 64-bit (ifHCInOctets / ifHCOutOctets) supaya tidak
wrap di link gigabit ke atas.
"""
import logging

from pysnmp.hlapi import (
    CommunityData,
    ContextData,
    ObjectIdentity,
    ObjectType,
    SnmpEngine,
    UdpTransportTarget,
    getCmd,
)

from .. import config

log = logging.getLogger(__name__)

OID_IF_HC_IN = "1.3.6.1.2.1.31.1.1.1.6."    # ifHCInOctets
OID_IF_HC_OUT = "1.3.6.1.2.1.31.1.1.1.10."  # ifHCOutOctets
OID_IF_OPER = "1.3.6.1.2.1.2.2.1.8."        # ifOperStatus (1=up)

# Batas varbind per request supaya PDU tidak terlalu besar
CHUNK = 10

_engine = SnmpEngine()


def poll_snmp_customers(device: dict, customers: list) -> dict:
    """Poll semua customer snmp_if pada satu device.

    Returns: {customer_id: {"in_octets", "out_octets", "link_up"}}
    Customer yang gagal di-poll tidak ada di hasil.
    """
    results = {}
    community = CommunityData(device["snmp_community"] or "public", mpModel=1)
    target = UdpTransportTarget(
        (str(device["ip"]), device["snmp_port"] or 161),
        timeout=config.SNMP_TIMEOUT,
        retries=config.SNMP_RETRIES,
    )

    for i in range(0, len(customers), CHUNK):
        chunk = customers[i:i + CHUNK]
        varbinds = []
        for c in chunk:
            idx = c["if_index"]
            varbinds.append(ObjectType(ObjectIdentity(OID_IF_HC_IN + str(idx))))
            varbinds.append(ObjectType(ObjectIdentity(OID_IF_HC_OUT + str(idx))))
            varbinds.append(ObjectType(ObjectIdentity(OID_IF_OPER + str(idx))))

        error_indication, error_status, error_index, var_binds = next(
            getCmd(_engine, community, target, ContextData(), *varbinds)
        )

        if error_indication:
            log.error("SNMP %s: %s", device["ip"], error_indication)
            continue
        if error_status:
            log.error(
                "SNMP %s error status: %s at %s",
                device["ip"], error_status.prettyPrint(), error_index,
            )
            continue

        # var_binds berurutan 3 per customer sesuai urutan request
        for pos, c in enumerate(chunk):
            base = pos * 3
            try:
                in_oct = int(var_binds[base][1])
                out_oct = int(var_binds[base + 1][1])
                oper = int(var_binds[base + 2][1])
            except (ValueError, TypeError, IndexError):
                log.warning(
                    "SNMP %s ifIndex %s: nilai tidak valid (cek ifIndex?)",
                    device["ip"], c["if_index"],
                )
                continue
            results[c["id"]] = {
                "in_octets": in_oct,
                "out_octets": out_oct,
                "link_up": oper == 1,
            }

    return results


OID_IF_NAME = "1.3.6.1.2.1.31.1.1.1.1"  # ifName


def walk_if_names(device: dict) -> dict:
    """Ambil {ifIndex: ifName} dari perangkat.

    Dipakai diagnosa untuk memastikan ifIndex yang terdaftar memang ada.
    ifIndex bisa bergeser setelah reboot atau perubahan modul, dan itu
    penyebab paling sering pelanggan SNMP tiba-tiba kehilangan data.
    """
    from pysnmp.hlapi import (
        CommunityData, ContextData, ObjectIdentity, ObjectType,
        SnmpEngine, UdpTransportTarget, nextCmd,
    )

    result = {}
    it = nextCmd(
        SnmpEngine(),
        CommunityData(device["snmp_community"] or "public", mpModel=1),
        UdpTransportTarget(
            (str(device["ip"]), device["snmp_port"] or 161),
            timeout=config.SNMP_TIMEOUT, retries=config.SNMP_RETRIES,
        ),
        ContextData(),
        ObjectType(ObjectIdentity(OID_IF_NAME)),
        lexicographicMode=False,
    )
    for error_indication, error_status, _, var_binds in it:
        if error_indication:
            raise RuntimeError(str(error_indication))
        if error_status:
            raise RuntimeError(error_status.prettyPrint())
        for oid, val in var_binds:
            idx = int(str(oid).rsplit(".", 1)[1])
            result[idx] = str(val)
    return result

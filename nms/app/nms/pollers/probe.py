"""Probe ringan untuk memastikan perangkat masih merespon.

Dipakai untuk perangkat yang belum punya pelanggan terdaftar, supaya
kesehatannya tetap terpantau.
"""
import logging
import socket

from .. import config

log = logging.getLogger(__name__)

OID_SYS_UPTIME = "1.3.6.1.2.1.1.3.0"


def probe_snmp(device) -> bool:
    from pysnmp.hlapi import (
        CommunityData, ContextData, ObjectIdentity, ObjectType,
        SnmpEngine, UdpTransportTarget, getCmd,
    )
    try:
        error_indication, error_status, _, _ = next(
            getCmd(
                SnmpEngine(),
                CommunityData(device["snmp_community"] or "public", mpModel=1),
                UdpTransportTarget(
                    (str(device["ip"]), device["snmp_port"] or 161),
                    timeout=config.SNMP_TIMEOUT, retries=config.SNMP_RETRIES,
                ),
                ContextData(),
                ObjectType(ObjectIdentity(OID_SYS_UPTIME)),
            )
        )
        return not error_indication and not error_status
    except Exception as e:
        log.debug("probe_snmp %s: %s", device["ip"], e)
        return False


def probe_tcp(host: str, port: int, timeout: int = 5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def probe_device(device) -> bool:
    """True kalau perangkat merespon."""
    if device["poll_method"] == "snmp":
        return probe_snmp(device)
    return probe_tcp(str(device["ip"]), device["api_port"] or 8728,
                     timeout=config.SNMP_TIMEOUT)

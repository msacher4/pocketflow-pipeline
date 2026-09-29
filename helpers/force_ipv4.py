"""Force IPv4-only DNS resolution for the entire process.

Monkey-patches socket.getaddrinfo so that all DNS lookups return only
IPv4 addresses. This avoids ConnectTimeout when the system resolves
AAAA records (IPv6) that are unreachable from this network.

Import this module at the very top of main.py, before any httpx usage.
"""
import socket

_original_getaddrinfo = socket.getaddrinfo


def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _original_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)


socket.getaddrinfo = _ipv4_only_getaddrinfo

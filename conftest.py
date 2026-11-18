"""Refuse network access beyond this machine for every Python test.

A test that reaches the internet is slow, fails when someone else's service does, and may contact a
real provider. Loopback servers and Unix sockets stay available. Code under test often treats a
connection error as an ordinary source failure, so a refused attempt also fails the test itself.
Subprocesses are not covered.
"""
import ipaddress
import socket

import pytest


class NetworkRefused(OSError):
    pass


_attempts = []
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_getaddrinfo = socket.getaddrinfo


def _local(host):
    if isinstance(host, bytes):
        host = host.decode()
    if host in (None, '', 'localhost'):
        return True
    try:
        address = ipaddress.ip_address(host.split('%')[0])
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified


def _check(sock, address):
    if sock.family in (socket.AF_INET, socket.AF_INET6) and not _local(address[0]):
        _attempts.append(address[0])
        raise NetworkRefused(f'tests may not reach {address[0]}')


def _connect(sock, address):
    _check(sock, address)
    return _real_connect(sock, address)


def _connect_ex(sock, address):
    _check(sock, address)
    return _real_connect_ex(sock, address)


def _getaddrinfo(host, *args, **kwargs):
    if not _local(host):
        _attempts.append(host)
        raise NetworkRefused(f'tests may not resolve {host}')
    return _real_getaddrinfo(host, *args, **kwargs)


socket.socket.connect = _connect
socket.socket.connect_ex = _connect_ex
socket.getaddrinfo = _getaddrinfo


@pytest.fixture(autouse=True)
def _no_network():
    _attempts.clear()
    yield
    if _attempts:
        pytest.fail(f'test tried to reach the network: {", ".join(sorted(set(map(str, _attempts))))}')

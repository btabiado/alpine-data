"""Shared pytest fixtures for the alpine-data test suite."""
from __future__ import annotations

import ipaddress
import os
import socket
import sys
from pathlib import Path

import pytest

# Make the project root importable so `import app`, `import server`, etc. work.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Network guard
# ---------------------------------------------------------------------------
# Unit tests must not reach the internet. A live call makes the suite slow and
# flaky, and several fetchers write what they download straight into tracked
# files: until this guard existed, every local run rewrote data-travel.json
# from travel.state.gov.
#
# Two layers, on for every test unless it is marked ``network``:
#   1. socket.connect / connect_ex / getaddrinfo / gethostbyname refuse any
#      non-loopback address. The attempt is also recorded, and the test FAILS
#      at teardown even when the code under test swallowed the error, so a
#      silent "offline fallback" cannot hide a live call.
#   2. The proxy variables point at a closed loopback port, so anything that
#      honours them (requests, urllib, libcurl, and child processes a test
#      starts) fails fast instead of going out. Loopback hosts bypass it.
#
# A test that truly needs the internet opts in with ``@pytest.mark.network``.
# Those run locally only; they are skipped when ``CI`` is set (GitHub sets it
# on every runner).

# tcpmux: reserved, nothing listens on it, so a proxied request is refused
# at once. Connecting to it counts as a blocked attempt (layer 1).
_DEAD_PROXY_PORT = 1
_DEAD_PROXY = f"http://127.0.0.1:{_DEAD_PROXY_PORT}"
_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
               "http_proxy", "https_proxy", "all_proxy")
_NO_PROXY_VARS = ("NO_PROXY", "no_proxy")
_LOCAL_NAMES = {"", "localhost", "localhost.localdomain", "ip6-localhost"}

_REAL_ENV = {k: os.environ.get(k) for k in _PROXY_VARS + _NO_PROXY_VARS}
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_getaddrinfo = socket.getaddrinfo
_real_gethostbyname = socket.gethostbyname
_real_gethostbyname_ex = socket.gethostbyname_ex

_network_allowed = False
_blocked_attempts: list[str] = []


class NetworkBlockedError(OSError):
    """A test tried to open a real (non-loopback) connection."""


def _is_local_host(host) -> bool:
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    host = str(host).strip().strip("[]").lower()
    if host in _LOCAL_NAMES or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    return ip.is_loopback or ip.is_unspecified


def _block(target: str, how: str):
    _blocked_attempts.append(target)
    raise NetworkBlockedError(
        f"network access is blocked in tests ({how} {target}); mock the call, "
        f"or mark the test @pytest.mark.network if it must go live")


def _check_address(sock, address) -> None:
    if _network_allowed or sock.family == getattr(socket, "AF_UNIX", None):
        return
    if isinstance(address, tuple) and address:
        host = address[0]
        port = address[1] if len(address) > 1 else None
        if port == _DEAD_PROXY_PORT and _is_local_host(host):
            _block("an outbound request", "via the proxy variables:")
        if _is_local_host(host):
            return
        _block(f"{host}:{port}", "connect to")
    _block(repr(address), "connect to")


def _guarded_connect(self, address):
    _check_address(self, address)
    return _real_connect(self, address)


def _guarded_connect_ex(self, address):
    _check_address(self, address)
    return _real_connect_ex(self, address)


def _guarded_getaddrinfo(host, *args, **kwargs):
    if not _network_allowed and not _is_local_host(host):
        _blocked_attempts.append(str(host))
        raise socket.gaierror(
            socket.EAI_NONAME,
            f"DNS lookup of {host!r} blocked in tests; mock the call, or mark "
            f"the test @pytest.mark.network if it must go live")
    return _real_getaddrinfo(host, *args, **kwargs)


def _guarded_gethostbyname(host):
    _guarded_getaddrinfo(host, None)
    return _real_gethostbyname(host)


def _guarded_gethostbyname_ex(host):
    _guarded_getaddrinfo(host, None)
    return _real_gethostbyname_ex(host)


def _block_proxy_env() -> None:
    for var in _PROXY_VARS:
        os.environ[var] = _DEAD_PROXY
    for var in _NO_PROXY_VARS:
        os.environ[var] = "localhost,127.0.0.1,::1"


# Installed at import, before collection, so module-level code in a test file
# (a skipif that probes a host, say) is covered too.
socket.socket.connect = _guarded_connect
socket.socket.connect_ex = _guarded_connect_ex
socket.getaddrinfo = _guarded_getaddrinfo
socket.gethostbyname = _guarded_gethostbyname
socket.gethostbyname_ex = _guarded_gethostbyname_ex
_block_proxy_env()


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "network: needs the real internet; the network guard is lifted for "
        "this test, and it is skipped in CI",
    )


def pytest_collection_modifyitems(config, items):
    if not os.environ.get("CI"):
        return
    skip = pytest.mark.skip(reason="needs the internet; network tests do not run in CI")
    for item in items:
        if item.get_closest_marker("network"):
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _network_guard(request, monkeypatch):
    global _network_allowed
    _blocked_attempts.clear()
    if request.node.get_closest_marker("network"):
        _network_allowed = True
        for var, value in _REAL_ENV.items():
            if value is None:
                monkeypatch.delenv(var, raising=False)
            else:
                monkeypatch.setenv(var, value)
        try:
            yield
        finally:
            _network_allowed = False
        return
    yield
    if _blocked_attempts:
        attempts = ", ".join(sorted(set(_blocked_attempts)))
        _blocked_attempts.clear()
        pytest.fail(
            f"test tried to reach the network ({attempts}). Mock the call, or "
            f"mark the test @pytest.mark.network if it must go live.",
            pytrace=False,
        )


# ---------------------------------------------------------------------------
# Files the code under test would otherwise write inside the repo
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_insights_history(tmp_path, monkeypatch):
    """Redirect the rolling insights-history file to a tmp path for every
    test so calling ``insights.build_insights`` doesn't write to the real
    ``data/insights_history.json`` or read stale rows left by a prior run.

    Autouse so individual tests don't need to remember the fixture; the
    cost is one ``import insights`` per test (cheap, already cached after
    the first hit). Tests that *want* to seed prior days can write to the
    same path with ``insights._HISTORY_PATH.write_text(...)``.
    """
    try:
        import insights
    except Exception:
        # Some tests don't import insights at all — skip rebinding rather
        # than failing the test collection phase.
        return
    monkeypatch.setattr(insights, "_HISTORY_PATH", tmp_path / "insights_history.json")


@pytest.fixture(autouse=True)
def _isolate_fetch_market_stale_cache(tmp_path, monkeypatch):
    """Point fetch_market's stale-keep cache (``data/.stale/``) at tmp_path.

    The fetchers save their last good payload there and some tests delete it
    to start clean, so without this a test run wrote and removed files next
    to the committed NUFORC cache. Tests that seed their own stale files
    still set ``_STALE_DIR`` themselves; this is the default underneath.
    """
    try:
        import fetch_market
    except Exception:
        return
    monkeypatch.setattr(fetch_market, "_STALE_DIR", tmp_path / ".stale")

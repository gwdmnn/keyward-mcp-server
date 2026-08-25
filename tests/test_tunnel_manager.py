"""Tests for the SSH tunnel pool (milestone M5 acceptance).

Uses an injected fake forwarder factory — no real SSH is opened.
"""

from __future__ import annotations

import pytest

from db_mcp_server.errors import DbMcpError, ErrorCode
from db_mcp_server.models import TunnelProfile
from db_mcp_server.tunnel_manager import TunnelManager


PROFILE = TunnelProfile(
    jump_host="jump.example", jump_user="tun", pem_key="k.pem", db_host="db.example"
)
KEY = ("prefmaringa", "live")


class FakeForwarder:
    def __init__(self, port: int):
        self.local_bind_port = port
        self.is_active = False
        self.starts = 0
        self.stops = 0

    def start(self):
        self.starts += 1
        self.is_active = True

    def stop(self):
        self.stops += 1
        self.is_active = False


def _counting_factory():
    created: list[FakeForwarder] = []

    def factory(profile):
        fwd = FakeForwarder(port=15000 + len(created))
        created.append(fwd)
        return fwd

    return factory, created


def test_get_opens_and_returns_local_endpoint():
    factory, created = _counting_factory()
    tm = TunnelManager("keys", forwarder_factory=factory)

    host, port = tm.get(KEY, PROFILE)
    assert host == "127.0.0.1"
    assert port == 15000
    assert len(created) == 1 and created[0].starts == 1


def test_reuse_does_not_open_second_tunnel():
    factory, created = _counting_factory()
    tm = TunnelManager("keys", forwarder_factory=factory)

    a = tm.get(KEY, PROFILE)
    b = tm.get(KEY, PROFILE)
    assert a == b
    assert len(created) == 1  # same (customer, env) -> one tunnel


def test_distinct_keys_open_distinct_tunnels():
    factory, created = _counting_factory()
    tm = TunnelManager("keys", forwarder_factory=factory)

    tm.get(("prefmaringa", "live"), PROFILE)
    tm.get(("planserv", "live"), PROFILE)
    assert len(created) == 2


def test_stale_tunnel_is_reopened():
    factory, created = _counting_factory()
    tm = TunnelManager("keys", forwarder_factory=factory)

    tm.get(KEY, PROFILE)
    created[0].is_active = False  # simulate a dropped tunnel

    tm.get(KEY, PROFILE)
    assert len(created) == 2  # reopened
    assert created[0].stops == 1  # old one was torn down


def test_open_failure_maps_to_tunnel_failed():
    def failing_factory(profile):
        fwd = FakeForwarder(port=1)

        def boom():
            raise RuntimeError("handshake refused")

        fwd.start = boom
        return fwd

    tm = TunnelManager("keys", forwarder_factory=failing_factory)
    with pytest.raises(DbMcpError) as ei:
        tm.get(KEY, PROFILE)
    assert ei.value.code is ErrorCode.TUNNEL_FAILED


def test_close_all_stops_everything():
    factory, created = _counting_factory()
    tm = TunnelManager("keys", forwarder_factory=factory)
    tm.get(("a", "live"), PROFILE)
    tm.get(("b", "live"), PROFILE)

    tm.close_all()
    assert all(f.stops == 1 for f in created)
    # pool is empty -> next get opens fresh
    tm.get(("a", "live"), PROFILE)
    assert len(created) == 3


def test_close_idle_respects_timeout():
    now = [1000.0]
    factory, created = _counting_factory()
    tm = TunnelManager("keys", idle_timeout_s=300, forwarder_factory=factory, clock=lambda: now[0])

    tm.get(KEY, PROFILE)          # last_used = 1000
    now[0] = 1200.0               # 200s elapsed -> within timeout
    tm.close_idle()
    assert created[0].stops == 0

    now[0] = 1400.0               # 400s since last use -> beyond timeout
    tm.close_idle()
    assert created[0].stops == 1
    # reopens after idle close
    tm.get(KEY, PROFILE)
    assert len(created) == 2

"""连接自愈：空闲后探活、探测失败即摘掉、后台巡检线程。"""

from __future__ import annotations

import textwrap
import time
from pathlib import Path

import anyio
import pytest

from ssh_mcp.config import load_config
from ssh_mcp.pool import SSHPool, _Chan


class _FakeChan:
    def __init__(self, *, exit_ready: bool = True, closed: bool = False) -> None:
        self._exit_ready = exit_ready
        self.closed = closed
        self.timeout = None
        self.executed: str | None = None
        self.close_calls = 0

    def settimeout(self, v):
        self.timeout = v

    def exec_command(self, cmd):
        self.executed = cmd

    def exit_status_ready(self):
        return self._exit_ready

    def recv_ready(self):
        return False

    def recv_stderr_ready(self):
        return False

    def close(self):
        self.close_calls += 1
        self.closed = True


class _FakeTransport:
    def __init__(self, chan: _FakeChan, active: bool = True) -> None:
        self.chan = chan
        self.active = active

    def is_active(self):
        return self.active

    def open_session(self, timeout=None):
        return self.chan


class _FakeClient:
    def __init__(self, transport: _FakeTransport) -> None:
        self._t = transport

    def get_transport(self):
        return self._t

    def close(self):
        pass


def _conn(chan: _FakeChan, *, idle: float = 0.0, alias: str = "box") -> _Chan:
    host = type("H", (), {"alias": alias, "host": "10.0.0.9", "port": 22, "user": "ops"})()
    conn = _Chan(client=_FakeClient(_FakeTransport(chan)), host=host)
    conn.last_used = time.time() - idle
    return conn


@pytest.fixture()
def pool(tmp_path: Path) -> SSHPool:
    cfg = tmp_path / "servers.yaml"
    cfg.write_text(
        textwrap.dedent(
            """
            name: t
            policy:
              idle_probe_after: 60
              heal_interval: 0
              probe_timeout: 2
            hosts:
              box:
                host: 10.0.0.9
                user: ops
                auth:
                  method: agent
            """
        ),
        encoding="utf-8",
    )
    return SSHPool(load_config(str(cfg)))


# --------------------------------------------------------------------------- #
# _should_probe 的前置条件
# --------------------------------------------------------------------------- #
def test_no_probe_when_recently_used(pool: SSHPool):
    assert pool._should_probe(_conn(_FakeChan(), idle=5)) is False


def test_probe_when_idle(pool: SSHPool):
    assert pool._should_probe(_conn(_FakeChan(), idle=120)) is True


def test_no_probe_when_busy(pool: SSHPool):
    conn = _conn(_FakeChan(), idle=120)
    conn.enter()
    assert pool._should_probe(conn) is False


def test_no_probe_when_disabled(tmp_path: Path):
    cfg = tmp_path / "s.yaml"
    cfg.write_text(
        "name: t\npolicy:\n  idle_probe_after: 0\nhosts:\n  box:\n    host: 10.0.0.9\n    auth:\n      method: agent\n",
        encoding="utf-8",
    )
    pool = SSHPool(load_config(str(cfg)))
    assert pool._should_probe(_conn(_FakeChan(), idle=99999)) is False


# --------------------------------------------------------------------------- #
# _probe
# --------------------------------------------------------------------------- #
def test_probe_alive(pool: SSHPool):
    chan = _FakeChan(exit_ready=True)
    assert pool._probe(_conn(chan)) is True
    assert chan.executed == "true"
    assert chan.timeout == 2.0          # 用的是 policy.probe_timeout
    assert chan.close_calls >= 1


def test_probe_dead_when_no_exit_status(pool: SSHPool):
    chan = _FakeChan(exit_ready=False)
    started = time.monotonic()
    assert pool._probe(_conn(chan)) is False
    assert time.monotonic() - started < 3.0   # 有界，不会卡住
    assert chan.close_calls >= 1


def test_probe_dead_when_transport_inactive(pool: SSHPool):
    chan = _FakeChan()
    conn = _conn(chan)
    conn.client._t.active = False
    assert pool._probe(conn) is False


# --------------------------------------------------------------------------- #
# heal_once
# --------------------------------------------------------------------------- #
def test_heal_drops_dead_idle_connection(pool: SSHPool):
    dead = _conn(_FakeChan(exit_ready=False), idle=600)
    pool._conns["box"] = dead
    dropped = pool.heal_once()
    assert dropped == ["box"]
    assert "box" not in pool._conns
    assert dead.probe_failures == 1


def test_heal_keeps_healthy_connection(pool: SSHPool):
    good = _conn(_FakeChan(exit_ready=True), idle=600)
    pool._conns["box"] = good
    assert pool.heal_once() == []
    assert "box" in pool._conns
    assert good.probe_failures == 0
    assert good.idle_seconds < 5      # 探活成功会把 last_used 刷新，避免每轮都探


def test_heal_skips_fresh_and_busy(pool: SSHPool):
    fresh = _conn(_FakeChan(exit_ready=False), idle=1)
    busy = _conn(_FakeChan(exit_ready=False), idle=600)
    busy.enter()
    pool._conns["fresh"] = fresh
    pool._conns["busy"] = busy
    assert pool.heal_once() == []
    assert set(pool._conns) == {"fresh", "busy"}


# --------------------------------------------------------------------------- #
# get_conn 里的「使用前自愈」
# --------------------------------------------------------------------------- #
def test_get_conn_reconnects_when_probe_fails(pool: SSHPool, monkeypatch):
    dead = _conn(_FakeChan(exit_ready=False), idle=600)
    pool._conns["box"] = dead
    calls = {"reconnect": 0, "dropped": 0}

    def fake_connect(host, depth=0):     # _connect_sync 是同步函数，走 to_thread
        calls["reconnect"] += 1
        return _FakeClient(_FakeTransport(_FakeChan())), None

    async def fake_drop(alias, conn):
        calls["dropped"] += 1
        pool._conns.pop(alias, None)

    monkeypatch.setattr(pool, "_connect_sync", fake_connect)
    monkeypatch.setattr(pool, "_drop_conn", fake_drop)

    conn = anyio.run(lambda: pool.get_conn("box"))
    assert calls["dropped"] == 1        # 先摘掉死的
    assert calls["reconnect"] == 1      # 再建新的
    assert pool._conns["box"] is conn


def test_get_conn_keeps_connection_when_probe_ok(pool: SSHPool, monkeypatch):
    good = _conn(_FakeChan(exit_ready=True), idle=600)
    pool._conns["box"] = good

    async def boom(*a, **k):
        raise AssertionError("不应该重连")

    monkeypatch.setattr(pool, "_connect_sync", boom)
    conn = anyio.run(lambda: pool.get_conn("box"))
    assert conn is good


# --------------------------------------------------------------------------- #
# 后台巡检线程
# --------------------------------------------------------------------------- #
def test_healer_thread_runs_and_stops(tmp_path: Path):
    cfg = tmp_path / "s.yaml"
    cfg.write_text(
        "name: t\npolicy:\n  idle_probe_after: 1\n  heal_interval: 1\n  probe_timeout: 0.5\n"
        "hosts:\n  box:\n    host: 10.0.0.9\n    auth:\n      method: agent\n",
        encoding="utf-8",
    )
    pool = SSHPool(load_config(str(cfg)))
    pool._conns["box"] = _conn(_FakeChan(exit_ready=False), idle=60)

    pool.start_healer()
    try:
        deadline = time.time() + 6
        while time.time() < deadline and "box" in pool._conns:
            time.sleep(0.2)
        assert "box" not in pool._conns, "巡检线程应该已经摘掉死连接"
    finally:
        pool.stop_healer()
    assert pool._healer is None

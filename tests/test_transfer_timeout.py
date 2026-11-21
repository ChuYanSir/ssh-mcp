"""文件传输超时：之前 sftp.put 没有超时，连接断了会永久阻塞并把整个服务卡死。"""

from __future__ import annotations

import textwrap
from pathlib import Path

import anyio
import pytest

from ssh_mcp.config import load_config
from ssh_mcp.pool import SSHExecError, SSHPool


class _FakeChannel:
    def __init__(self) -> None:
        self.timeout = None

    def settimeout(self, value: float) -> None:
        self.timeout = value


class _FakeSFTP:
    def __init__(self) -> None:
        self.chan = _FakeChannel()
        self.closed = False

    def get_channel(self) -> _FakeChannel:
        return self.chan

    def close(self) -> None:
        self.closed = True


class _FakeClient:
    def __init__(self, sftp: _FakeSFTP) -> None:
        self._sftp = sftp

    def open_sftp(self) -> _FakeSFTP:
        return self._sftp


class _FakeConn:
    def __init__(self, sftp: _FakeSFTP) -> None:
        self.host = None
        self.client = _FakeClient(sftp)
        self.inflight = 0

    def enter(self) -> None:
        self.inflight += 1

    def leave(self) -> None:
        self.inflight = max(0, self.inflight - 1)


@pytest.fixture()
def pool(tmp_path: Path) -> SSHPool:
    cfg_file = tmp_path / "servers.yaml"
    cfg_file.write_text(
        textwrap.dedent(
            """
            name: t
            defaults:
              command_timeout: 120
            policy:
              max_transfer_timeout: 600
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
    return SSHPool(load_config(str(cfg_file)))


def test_transfer_timeout_defaults_and_cap(pool: SSHPool):
    host = pool.cfg.get("box")
    assert pool._transfer_timeout(host, None) == 240.0      # command_timeout(120) * 2
    assert pool._transfer_timeout(host, 30) == 30.0          # 显式值优先
    assert pool._transfer_timeout(host, 99999) == 600.0      # 被 policy 上限截断
    assert pool._transfer_timeout(host, 0.1) == 5.0          # 下限 5 秒


def test_sftp_op_sets_channel_timeout(pool: SSHPool):
    sftp = _FakeSFTP()
    conn = _FakeConn(sftp)
    seen = {}

    def op(handle):
        seen["channel_timeout"] = handle.get_channel().timeout
        return "done"

    assert pool._sftp_op(conn, op, timeout=42) == "done"
    assert seen["channel_timeout"] == 42
    assert sftp.closed is True  # 用完一定关掉


def test_sftp_op_maps_socket_timeout(pool: SSHPool):
    conn = _FakeConn(_FakeSFTP())

    def boom(_handle):
        raise TimeoutError("timed out")

    with pytest.raises(SSHExecError) as exc:
        pool._sftp_op(conn, boom, timeout=7)
    assert "传输超时" in str(exc.value) and "7s" in str(exc.value)


def test_sftp_op_keeps_other_oserrors(pool: SSHPool):
    conn = _FakeConn(_FakeSFTP())

    def boom(_handle):
        raise PermissionError("denied")

    with pytest.raises(PermissionError):
        pool._sftp_op(conn, boom, timeout=7)


def test_sftp_call_drops_connection_on_failure(pool: SSHPool, monkeypatch):
    wall = []
    conn = _FakeConn(_FakeSFTP())

    async def fake_get_conn(alias, reconnect=False):
        return conn

    async def fake_close(alias=None):
        wall.append(alias)
        return [alias or "*"]

    monkeypatch.setattr(pool, "get_conn", fake_get_conn)
    monkeypatch.setattr(pool, "close", fake_close)

    def boom(_handle):
        raise TimeoutError("timed out")

    with pytest.raises(SSHExecError):
        anyio.run(lambda: pool._sftp_call("box", boom, timeout=5))
    assert wall == ["box"]  # 失败后丢弃这条连接，下次重连


def test_sftp_call_passes_timeout(pool: SSHPool, monkeypatch):
    conn = _FakeConn(_FakeSFTP())
    captured = {}

    async def fake_get_conn(alias, reconnect=False):
        return conn

    def fake_op(_conn, fn, *, timeout=None):
        captured["timeout"] = timeout
        return "ok"

    monkeypatch.setattr(pool, "get_conn", fake_get_conn)
    monkeypatch.setattr(pool, "_sftp_op", fake_op)

    assert anyio.run(lambda: pool._sftp_call("box", lambda s: s, timeout=11)) == "ok"
    assert captured["timeout"] == 11.0


def test_upload_tool_forwards_timeout(monkeypatch, tmp_path: Path):
    """工具层要把 timeout 透传下去，而不是吞掉。"""
    from ssh_mcp import server as srv

    captured = {}

    class FakePool:
        async def upload(self, alias, local_path, remote_path, *, timeout=None):
            captured.update(alias=alias, timeout=timeout)
            return {"alias": alias, "ok": True, "remote_size": 1}

    class FakeRT:
        pool = FakePool()

    monkeypatch.setattr(srv, "runtime", lambda *a, **k: FakeRT())
    monkeypatch.setattr(srv, "_audit", lambda *a, **k: None)

    local = tmp_path / "a.txt"
    local.write_text("x", encoding="utf-8")
    out = anyio.run(lambda: srv.ssh_upload("box", str(local), "/tmp/a.txt", timeout=33))
    assert out["ok"] is True
    assert captured["timeout"] == 33

"""exec 的「永久阻塞」防护。

背景：paramiko 的 `Channel.recv_exit_status()` 内部是 `status_event.wait()`，
没有任何超时；连接在执行途中断掉时永远等不到 exit-status，会把线程永久挂住。
实测现场：一次成功的 exec 之后进程再无审计记录，2.5 小时内所有调用排队超时。
"""

from __future__ import annotations

import textwrap
import time
from pathlib import Path

import pytest

from ssh_mcp.config import load_config
from ssh_mcp.pool import SSHExecError, SSHPool


class _FakeChan:
    """可以摆出各种「半死连接」状态的假 channel。"""

    def __init__(
        self,
        *,
        closed: bool = False,
        exit_ready: bool = False,
        exit_status: int = 0,
        recv_data: bytes = b"",
    ) -> None:
        self.closed = closed
        self._exit_ready = exit_ready
        self._exit_status = exit_status
        self._recv = recv_data
        self.timeout = None
        self.close_calls = 0
        self.recv_exit_status_calls = 0

    # ---- 我们代码用到的接口 ----
    def settimeout(self, value):
        self.timeout = value

    def exit_status_ready(self):
        return self._exit_ready

    def recv_exit_status(self):
        self.recv_exit_status_calls += 1
        if not self._exit_ready:
            raise AssertionError("recv_exit_status 不该在未就绪时被调用")
        return self._exit_status

    def recv_ready(self):
        return bool(self._recv)

    def recv(self, n):
        data, self._recv = self._recv[:n], self._recv[n:]
        return data

    def recv_stderr_ready(self):
        return False

    def recv_stderr(self, n):
        return b""

    def close(self):
        self.close_calls += 1
        self.closed = True

    def exec_command(self, command):
        self.executed = command

    def sendall(self, data):
        self.sent = data


class _FakeTransport:
    def __init__(self, chan: _FakeChan) -> None:
        self.chan = chan
        self.active = True

    def is_active(self):
        return self.active

    def open_session(self, timeout=None):
        return self.chan


class _FakeClient:
    def __init__(self, chan: _FakeChan) -> None:
        self._transport = _FakeTransport(chan)

    def get_transport(self):
        return self._transport


class _FakeConn:
    def __init__(self, chan: _FakeChan, alias: str = "box"):
        self.host = type("H", (), {"alias": alias, "encoding": "utf-8"})()
        self.client = _FakeClient(chan)
        self.inflight = 0

    def enter(self) -> None:
        self.inflight += 1

    def leave(self) -> None:
        self.inflight = max(0, self.inflight - 1)


@pytest.fixture()
def pool(tmp_path: Path) -> SSHPool:
    cfg = tmp_path / "servers.yaml"
    cfg.write_text(
        textwrap.dedent(
            """
            name: t
            defaults:
              command_timeout: 30
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
# _safe_exit_status
# --------------------------------------------------------------------------- #
def test_exit_status_ready_returns_code(pool: SSHPool):
    chan = _FakeChan(exit_ready=True, exit_status=7)
    assert pool._safe_exit_status(chan, 2.0) == 7
    assert chan.recv_exit_status_calls == 1


def test_exit_status_never_ready_does_not_hang(pool: SSHPool):
    """核心用例：状态永远不就绪时必须在 wait 内返回，而且不能调用会永久阻塞的接口。"""
    chan = _FakeChan(exit_ready=False, closed=False)
    started = time.monotonic()
    result = pool._safe_exit_status(chan, 0.2)
    elapsed = time.monotonic() - started

    assert result is None
    assert elapsed < 1.0, f"耗时 {elapsed:.2f}s，应该立刻返回"
    assert chan.recv_exit_status_calls == 0       # 绝不能在未就绪时调它
    assert chan.close_calls == 1                  # 主动关掉 channel 让后续解锁


def test_exit_status_closed_channel_returns_fast(pool: SSHPool):
    chan = _FakeChan(exit_ready=False, closed=True)
    started = time.monotonic()
    assert pool._safe_exit_status(chan, 5.0) is None
    assert time.monotonic() - started < 0.5


# --------------------------------------------------------------------------- #
# _open_chan / _exec_sync
# --------------------------------------------------------------------------- #
def test_open_chan_uses_finite_timeout(pool: SSHPool):
    chan = _FakeChan()
    pool._open_chan(_FakeConn(chan), "echo hi", timeout=45)
    assert chan.timeout == 45      # 不是 None，否则半死连接会一直等


def test_exec_raises_when_channel_closed_without_status(pool: SSHPool):
    """连接执行途中断开（channel 关了但没 exit-status）→ 抛错让上层重连重试。"""
    chan = _FakeChan(closed=True, exit_ready=False)
    conn = _FakeConn(chan)

    with pytest.raises(SSHExecError) as exc:
        pool._exec_sync(
            conn, "echo hi", timeout=5, max_bytes=1024, sudo_password=None, encoding="utf-8"
        )
    assert "断开" in str(exc.value)


def test_exec_closed_channel_returns_quickly(pool: SSHPool):
    """连接断掉时不要死等到 deadline，要立刻判定失败。"""
    chan = _FakeChan(closed=True, exit_ready=False)
    started = time.monotonic()
    with pytest.raises(SSHExecError):
        pool._exec_sync(
            _FakeConn(chan), "echo hi", timeout=30, max_bytes=1024, sudo_password=None, encoding="utf-8"
        )
    assert time.monotonic() - started < 2.0


def test_exec_success_still_works(pool: SSHPool):
    chan = _FakeChan(exit_ready=True, exit_status=0, recv_data=b"hello\n")
    code, out, err, truncated, timed_out = pool._exec_sync(
        _FakeConn(chan), "echo hello", timeout=5, max_bytes=1024, sudo_password=None, encoding="utf-8"
    )
    assert code == 0 and out == b"hello\n" and err == b""
    assert timed_out is False and truncated is False


def test_exec_timeout_returns_flagged(pool: SSHPool):
    """命令超时（不是连接断）应返回 timed_out=True，而不是抛错。"""
    chan = _FakeChan(exit_ready=False, closed=False)
    code, out, err, truncated, timed_out = pool._exec_sync(
        _FakeConn(chan), "sleep 100", timeout=0.2, max_bytes=1024, sudo_password=None, encoding="utf-8"
    )
    assert timed_out is True
    assert code == -1

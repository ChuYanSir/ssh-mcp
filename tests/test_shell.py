"""交互式 shell（PTY）测试：走假 SSH 服务器的 shell 通道。

真 PTY 的行为（top / vi）在真实主机上人工验证过；这里锁住协议链路：
开 / 发 / 读 / 改尺寸 / 关、会话上限、策略开关、ANSI 清洗。
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import anyio
import pytest

from fake_ssh_server import FakeSSHServer

from ssh_mcp import server as srv
from ssh_mcp.pool import strip_ansi


@pytest.fixture()
def shell_rt(tmp_path: Path, monkeypatch):
    fake = FakeSSHServer().start()
    shell_kind = "cmd" if os.name == "nt" else "posix"
    cfg = tmp_path / "servers.yaml"
    cfg.write_text(
        textwrap.dedent(
            f"""
            name: ssh-mcp-shell
            defaults:
              host_key_policy: ignore
              keepalive: 0
              shell_kind: {shell_kind}
              command_timeout: 30
            policy:
              allow_shell: true
              max_shell_sessions: 2
              shell_idle_timeout: 60
            audit:
              enabled: false
              path: {(tmp_path / 'audit.jsonl').as_posix()}
            hosts:
              local:
                host: 127.0.0.1
                port: {fake.port}
                user: testuser
                auth:
                  method: password
                  password: testpass
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SSH_MCP_CONFIG", str(cfg))
    monkeypatch.setenv("SSH_MCP_CRED_DIR", str(tmp_path / "creds"))
    rt = srv.init_runtime(str(cfg))
    try:
        yield rt
    finally:
        rt.pool.close_all_shells()
        fake.stop()


def test_open_shell_returns_pty_size(shell_rt):
    out = anyio.run(lambda: shell_rt.pool.open_shell("local", cols=100, rows=24))
    try:
        assert out["session_id"]
        assert out["info"]["size"] == "100x24"
        assert out["info"]["term"] == "xterm-256color"
        # 假服务器首屏会报出它收到的 PTY 尺寸
        assert "fake-pty 100x24" in out["plain"]
    finally:
        anyio.run(lambda: shell_rt.pool.shell_close(out["session_id"]))


def test_send_and_read_echo(shell_rt):
    opened = anyio.run(lambda: shell_rt.pool.open_shell("local"))
    sid = opened["session_id"]
    try:
        sent = anyio.run(lambda: shell_rt.pool.shell_send(sid, "hello-pty\r", wait=0.3))
        assert "hello-pty" in sent["plain"]
        assert sent["closed"] is False

        # stty size 走 PTY，能拿到真实行列
        sized = anyio.run(lambda: shell_rt.pool.shell_send(sid, "stty size\r", wait=0.3))
        assert "32 120" in sized["plain"] or "32 100" in sized["plain"]

        read = anyio.run(lambda: shell_rt.pool.shell_read(sid, timeout=0.2))
        assert read["session_id"] == sid
    finally:
        anyio.run(lambda: shell_rt.pool.shell_close(sid))


def test_resize_changes_pty(shell_rt):
    opened = anyio.run(lambda: shell_rt.pool.open_shell("local", cols=80, rows=20))
    sid = opened["session_id"]
    try:
        resized = anyio.run(lambda: shell_rt.pool.shell_resize(sid, 140, 44))
        assert resized["size"] == "140x44"
        out = anyio.run(lambda: shell_rt.pool.shell_send(sid, "stty size\r", wait=0.3))
        assert "44 140" in out["plain"]
    finally:
        anyio.run(lambda: shell_rt.pool.shell_close(sid))


def test_close_is_idempotent(shell_rt):
    opened = anyio.run(lambda: shell_rt.pool.open_shell("local"))
    sid = opened["session_id"]
    first = anyio.run(lambda: shell_rt.pool.shell_close(sid))
    second = anyio.run(lambda: shell_rt.pool.shell_close(sid))
    assert first["closed"] is True and first["existed"] is True
    assert second["closed"] is True and second["existed"] is False
    assert shell_rt.pool.shell_list() == []


def test_session_cap(shell_rt):
    a = anyio.run(lambda: shell_rt.pool.open_shell("local"))
    b = anyio.run(lambda: shell_rt.pool.open_shell("local"))
    try:
        with pytest.raises(Exception) as exc:
            anyio.run(lambda: shell_rt.pool.open_shell("local"))
        assert "上限" in str(exc.value)
    finally:
        anyio.run(lambda: shell_rt.pool.shell_close(a["session_id"]))
        anyio.run(lambda: shell_rt.pool.shell_close(b["session_id"]))


def test_unknown_session(shell_rt):
    with pytest.raises(Exception) as exc:
        anyio.run(lambda: shell_rt.pool.shell_read("nope", timeout=0.1))
    assert "没有会话" in str(exc.value)


def test_shell_disabled_by_policy(shell_rt):
    shell_rt.cfg.policy.allow_shell = False
    shell_rt.pool.cfg.policy.allow_shell = False
    with pytest.raises(PermissionError):
        anyio.run(lambda: shell_rt.pool.open_shell("local"))


def test_close_connection_also_closes_shells(shell_rt):
    opened = anyio.run(lambda: shell_rt.pool.open_shell("local"))
    assert opened["session_id"]
    anyio.run(lambda: shell_rt.pool.close("local"))
    assert shell_rt.pool.shell_list() == []


def test_strip_ansi():
    raw = "\x1b[1;32mOK\x1b[0m \x1b(Bplain\r\nline2\rline3"
    assert strip_ansi(raw) == "OK plain\nline2\nline3"


def test_tool_layer_open_send_close(shell_rt):
    """走 MCP 工具层（ssh_shell_* 的返回结构和错误兜底）。"""
    opened = anyio.run(lambda: srv.ssh_shell_open("local", cols=90, rows=22))
    assert opened["ok"] is True
    sid = opened["session_id"]

    sent = anyio.run(lambda: srv.ssh_shell_send(sid, "echo hi\r", wait_ms=300))
    assert sent["ok"] is True and "echo hi" in sent["plain"]

    resized = anyio.run(lambda: srv.ssh_shell_resize(sid, 100, 30))
    assert resized["ok"] is True and resized["size"] == "100x30"

    closed = anyio.run(lambda: srv.ssh_shell_close(sid))
    assert closed["ok"] is True

    bad = anyio.run(lambda: srv.ssh_shell_send("bogus", "x"))
    assert bad["ok"] is False and "没有会话" in bad["error"]

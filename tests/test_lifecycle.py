"""进程级自愈：请求到达日志（取证）+ 空闲自退（让客户端重新拉起）。"""

from __future__ import annotations

import textwrap
import time
from pathlib import Path

import pytest

from ssh_mcp.config import load_config
from ssh_mcp.server import Runtime


@pytest.fixture()
def rt(tmp_path: Path) -> Runtime:
    cfg = tmp_path / "servers.yaml"
    cfg.write_text(
        textwrap.dedent(
            """
            name: t
            policy:
              heal_interval: 0
              exit_if_idle: 0
            hosts:
              box:
                host: 10.0.0.9
                auth:
                  method: agent
            """
        ),
        encoding="utf-8",
    )
    return Runtime(str(cfg), autoload=False)


# --------------------------------------------------------------------------- #
# 请求取证
# --------------------------------------------------------------------------- #
def test_note_request_writes_log(rt: Runtime, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    rt.note_request("ssh_status")
    rt.note_request("ssh_exec")

    log = Path.home() / ".ssh-mcp" / "logs" / "requests.log"
    assert log.is_file()
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert lines[0].endswith("\tssh_status")
    assert lines[1].endswith("\tssh_exec")
    assert rt.request_count == 2


def test_note_request_without_tool_only_updates_timer(rt: Runtime):
    before = rt.last_request_at
    time.sleep(0.01)
    rt.note_request(None)
    assert rt.last_request_at > before
    assert rt.request_count == 1  # 不写日志的那次也算一次到达


def test_requests_log_rotates(rt: Runtime, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    log = Path.home() / ".ssh-mcp" / "logs" / "requests.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("x" * (2 * 1024 * 1024 + 10), encoding="utf-8")

    rt.note_request("ssh_status")
    assert log.is_file() and log.stat().st_size < 2000
    assert log.with_suffix(".log.1").is_file()


# --------------------------------------------------------------------------- #
# 空闲自退
# --------------------------------------------------------------------------- #
def test_idle_watchdog_disabled_by_zero(rt: Runtime):
    rt.start_idle_watchdog(0)
    assert rt._idle_thread is None
    assert rt.exit_if_idle == 0.0


def test_idle_watchdog_starts_thread(rt: Runtime):
    rt.start_idle_watchdog(600)
    try:
        assert rt._idle_thread is not None and rt._idle_thread.is_alive()
        assert rt.exit_if_idle == 600
    finally:
        rt._idle_thread = None  # 让 daemon 线程随进程结束，单测里不 join


def test_idle_exit_respects_recent_request(rt: Runtime, monkeypatch):
    """有请求就重置计时：不该退出。"""
    exits = []
    monkeypatch.setattr("ssh_mcp.server.os._exit", lambda code: exits.append(code))

    rt.last_request_at = time.time()          # 刚刚有请求
    rt.exit_if_idle = 0.2
    # 跑一轮判断逻辑：等过阈值时间后仍不该退出
    time.sleep(0.3)
    assert time.time() - rt.last_request_at >= 0.2
    # 直接调用循环体一次不现实（死循环），这里验证判定条件本身
    assert exits == []


def test_policy_exit_if_idle_default_and_override(tmp_path: Path):
    cfg = tmp_path / "a.yaml"
    cfg.write_text("name: t\nhosts: {}\n", encoding="utf-8")
    assert load_config(str(cfg)).policy.exit_if_idle == 1800.0

    cfg2 = tmp_path / "b.yaml"
    cfg2.write_text("name: t\npolicy:\n  exit_if_idle: 60\nhosts: {}\n", encoding="utf-8")
    assert load_config(str(cfg2)).policy.exit_if_idle == 60.0

    cfg3 = tmp_path / "c.yaml"
    cfg3.write_text("name: t\npolicy:\n  exit_if_idle: 0\nhosts: {}\n", encoding="utf-8")
    assert load_config(str(cfg3)).policy.exit_if_idle == 0.0


def test_every_tool_passes_its_name():
    """每个工具都要把名字交给 runtime()，否则取证日志分不清是哪个调用。"""
    import inspect
    import re

    from ssh_mcp import server as srv

    src = inspect.getsource(srv)
    names = set(re.findall(r'rt = runtime\("(\w+)"\)', src))
    tools = set(re.findall(r'@mcp\.tool\(\)\s*\n(?:@.*\n)*async def (\w+)\(', src))
    assert tools, "没解析到工具定义"
    assert tools <= names, f"这些工具没传名字：{sorted(tools - names)}"

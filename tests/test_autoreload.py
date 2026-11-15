"""配置热加载测试：改 servers.yaml 后不重启、不显式调用 reload，工具也能看到新配置。"""

from __future__ import annotations

import os
import time
from pathlib import Path

import anyio
import pytest

from fake_ssh_server import FakeSSHServer

from ssh_mcp import server as srv
from ssh_mcp.config import load_config


def _write(cfg: Path, port: int, extra: str = "") -> None:
    cfg.write_text(
        f"""name: ssh-mcp-autoload
defaults:
  host_key_policy: ignore
  keepalive: 0
  shell_kind: cmd
  command_timeout: 30
audit:
  enabled: true
  path: {(cfg.parent / 'audit.jsonl').as_posix()}
hosts:
  local:
    host: 127.0.0.1
    port: {port}
    user: testuser
    description: 本地假 SSH
    auth:
      method: password
      password: testpass
{extra}""",
        encoding="utf-8",
    )


def _touch_newer(path: Path) -> None:
    """确保 mtime 一定变化（Windows 上同毫秒内连续写可能不刷新）。"""
    future = time.time() + 2
    os.utime(path, (future, future))


def test_autoload_picks_up_new_host(tmp_path: Path, monkeypatch):
    fake = FakeSSHServer().start()
    try:
        cfg_path = tmp_path / "servers.yaml"
        _write(cfg_path, fake.port)
        monkeypatch.setenv("SSH_MCP_CONFIG", str(cfg_path))
        monkeypatch.setenv("SSH_MCP_CRED_DIR", str(tmp_path / "creds"))

        rt = srv.init_runtime(str(cfg_path))
        assert sorted(rt.cfg.hosts) == ["local"]

        # 模拟桌面端新增一台主机
        _write(
            cfg_path,
            fake.port,
            f"""  added:
    host: 127.0.0.1
    port: {fake.port}
    user: testuser
    description: 后加的
    auth:
      method: password
      password: testpass
""",
        )
        _touch_newer(cfg_path)

        payload = srv.ssh_list_hosts()
        assert [h["alias"] for h in payload["hosts"]] == ["added", "local"]
        assert rt.reload_count == 1
        assert rt.last_reload_error is None
    finally:
        fake.stop()


def test_autoload_noop_when_file_unchanged(tmp_path: Path, monkeypatch):
    fake = FakeSSHServer().start()
    try:
        cfg_path = tmp_path / "servers.yaml"
        _write(cfg_path, fake.port)
        rt = srv.init_runtime(str(cfg_path))
        for _ in range(3):
            srv.ssh_list_hosts()
        assert rt.reload_count == 0
    finally:
        fake.stop()


def test_autoload_keeps_old_config_on_broken_file(tmp_path: Path, monkeypatch):
    fake = FakeSSHServer().start()
    try:
        cfg_path = tmp_path / "servers.yaml"
        _write(cfg_path, fake.port)
        rt = srv.init_runtime(str(cfg_path))

        cfg_path.write_text("hosts: [this, is, not, a, map]\n", encoding="utf-8")
        _touch_newer(cfg_path)

        # 坏配置不该把服务打挂，旧的仍然可用
        payload = srv.ssh_list_hosts()
        assert [h["alias"] for h in payload["hosts"]] == ["local"]
        assert rt.last_reload_error is not None
        assert rt.reload_count == 0

        # 修好后自动恢复
        _write(cfg_path, fake.port)
        _touch_newer(cfg_path)
        payload2 = srv.ssh_list_hosts()
        assert [h["alias"] for h in payload2["hosts"]] == ["local"]
        assert rt.last_reload_error is None
        assert rt.reload_count == 1
    finally:
        fake.stop()


def test_autoload_disabled(tmp_path: Path, monkeypatch):
    fake = FakeSSHServer().start()
    try:
        cfg_path = tmp_path / "servers.yaml"
        _write(cfg_path, fake.port)
        rt = srv.init_runtime(str(cfg_path), autoload=False)
        cfg_path.write_text(cfg_path.read_text(encoding="utf-8").replace("ssh-mcp-autoload", "renamed"), encoding="utf-8")
        _touch_newer(cfg_path)

        srv.ssh_list_hosts()
        assert rt.reload_count == 0
        assert rt.cfg.name == "ssh-mcp-autoload"
    finally:
        fake.stop()


def test_autoload_keeps_working_after_reload(tmp_path: Path, monkeypatch):
    """重载后新连接池要能真的执行命令，旧连接在后台被关掉。"""
    fake = FakeSSHServer().start()
    try:
        cfg_path = tmp_path / "servers.yaml"
        _write(cfg_path, fake.port)
        rt = srv.init_runtime(str(cfg_path))

        first = anyio.run(lambda: rt.pool.exec("local", "echo before"))
        assert first.exit_code == 0

        _write(cfg_path, fake.port, "  second:\n    host: 127.0.0.1\n    port: %d\n    user: testuser\n    auth:\n      method: password\n      password: testpass\n" % fake.port)
        _touch_newer(cfg_path)
        srv.ssh_list_hosts()  # 触发自动重载

        assert rt.reload_count == 1
        second = anyio.run(lambda: rt.pool.exec("second", "echo after"))
        assert second.exit_code == 0
        assert "after" in second.stdout

        # 等后台清理线程跑完，旧池应该已经空了
        deadline = time.time() + 5
        while time.time() < deadline and rt.pool.status():
            time.sleep(0.1)
        assert load_config(str(cfg_path)).hosts.keys() == rt.cfg.hosts.keys()
    finally:
        fake.stop()

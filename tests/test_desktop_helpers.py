"""桌面端非 GUI 逻辑的回归测试。

重点覆盖「pythonw 下点启动 MCP 服务没反应」那个 bug：
pythonw 的 sys.stderr 是 None，uvicorn.Config 默认会调 sys.stderr.isatty() 崩掉，
所以必须显式传 use_colors=False。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("tkinter", reason="桌面端需要 tkinter；CI 的 Linux 镜像可能没装")

from ssh_mcp import desktop  # noqa: E402


def test_ensure_stdio_restores_missing_streams(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(desktop, "LOG_PATH", tmp_path / "logs" / "desktop.log")
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    desktop.ensure_stdio()

    assert sys.stdout is not None
    assert sys.stderr is not None
    sys.stdout.write("ok\n")


def test_log_line_writes_file(monkeypatch, tmp_path: Path):
    log = tmp_path / "desktop.log"
    monkeypatch.setattr(desktop, "LOG_PATH", log)
    desktop.log_line("hello 日志")
    assert "hello 日志" in log.read_text(encoding="utf-8")


def test_uvicorn_config_survives_missing_stdio(monkeypatch):
    """pythonw 下 sys.stdout/sys.stderr 都是 None：

    uvicorn 默认 formatter 会调 sys.stdout.isatty() → AttributeError → 整个 Config 构造失败，
    tkinter 回调里没人接住，表现就是「点了启动 MCP 服务一点反应都没有」。
    必须显式 use_colors=False（ensure_stdio 是第二道保险）。
    """
    import uvicorn
    from starlette.applications import Starlette

    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    with pytest.raises(ValueError):
        uvicorn.Config(Starlette(), host="127.0.0.1", port=0, log_level="warning")

    cfg = uvicorn.Config(Starlette(), host="127.0.0.1", port=0, log_level="warning", use_colors=False)
    assert cfg.use_colors is False
    assert cfg.host == "127.0.0.1"


def test_resolve_config_prefers_argument(monkeypatch, tmp_path: Path):
    target = tmp_path / "custom.yaml"
    target.write_text("name: x\nhosts: {}\n", encoding="utf-8")
    assert desktop.resolve_config(str(target)) == target

    monkeypatch.setenv("SSH_MCP_CONFIG", str(target))
    assert desktop.resolve_config(None) == target


def _tk_available() -> bool:
    try:
        import tkinter as tk

        root = tk.Tk()
        root.destroy()
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _tk_available(), reason="没有可用的图形环境")
def test_mcp_config_json_builders(tmp_path: Path, monkeypatch):
    import json

    cfg = tmp_path / "servers.yaml"
    cfg.write_text("name: t\nhosts: {}\n", encoding="utf-8")
    monkeypatch.setenv("SSH_MCP_CRED_DIR", str(tmp_path / "creds"))

    app = desktop.App(cfg)
    app.withdraw()
    try:
        app.var_port.set("9123")
        app.var_token.set("tok-123")

        http_cfg = json.loads(app.build_http_config())
        entry = http_cfg["mcpServers"]["ssh"]
        assert entry["type"] == "streamable-http"
        assert entry["url"] == "http://127.0.0.1:9123/mcp"
        assert entry["headers"]["Authorization"] == "Bearer tok-123"

        app.var_token.set("")
        no_auth = json.loads(app.build_http_config())["mcpServers"]["ssh"]
        assert "headers" not in no_auth

        stdio_cfg = json.loads(app.build_stdio_config())["mcpServers"]["ssh"]
        assert stdio_cfg["args"][:2] == ["-m", "ssh_mcp"]
        assert "--transport" in stdio_cfg["args"] and "stdio" in stdio_cfg["args"]
        assert str(cfg) in stdio_cfg["args"]
        # stdio 必须用带控制台的 python.exe（pythonw 的 stdout 是 None）
        assert not stdio_cfg["command"].lower().endswith("pythonw.exe")
    finally:
        app.destroy()

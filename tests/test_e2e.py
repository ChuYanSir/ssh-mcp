"""端到端测试：真起一个本地 SSH 服务，验证「会话端只传别名就能执行命令」。

覆盖：exec / exit code / stderr / 截断 / 策略拒绝 / 未知别名 / SFTP 往返 /
      MCP stdio 协议全链路（tools/list + tools/call）。
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path

import anyio
import pytest

from fake_ssh_server import FakeSSHServer
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from ssh_mcp import server as srv
from ssh_mcp.config import load_config


def test_config_parses_and_hides_secrets(cfg_path: Path):
    cfg = load_config(str(cfg_path))
    host = cfg.hosts["local"]
    desc = host.describe()
    assert desc["host"] == "127.0.0.1"
    assert desc["auth"]["password_source"] == "literal"
    # describe() 不能出现密码本体
    assert "testpass" not in json.dumps(desc, ensure_ascii=False)


def test_exec_basic(rt):
    res = anyio.run(lambda: rt.pool.exec("local", "echo hello-ssh-mcp"))
    assert res.exit_code == 0, res.stderr
    assert "hello-ssh-mcp" in res.stdout
    assert res.timed_out is False


def test_exec_stderr_and_exit_code(rt):
    res = anyio.run(lambda: rt.pool.exec("local", "echo oops 1>&2"))
    assert "oops" in res.stderr

    bad = anyio.run(lambda: rt.pool.exec("local", "exit 3"))
    assert bad.exit_code == 3
    assert bad.to_dict()["ok"] is False


def test_exec_truncation(rt):
    # 跨平台：两条命令都只用到各自 shell 的内建/常见命令，避免嵌套引号
    big = (
        "for /l %i in (1,1,200) do @echo aaaaaaaaaaaaaaaaaaaa"
        if os.name == "nt"
        else "seq 1 400"
    )
    res = anyio.run(lambda: rt.pool.exec("local", big, max_output_bytes=64))
    assert res.truncated is True
    assert len(res.stdout) <= 64


def test_exec_unknown_alias(rt):
    with pytest.raises(KeyError):
        anyio.run(lambda: rt.pool.exec("nope", "echo x"))


def test_policy_denies_command(rt):
    with pytest.raises(PermissionError):
        anyio.run(lambda: rt.pool.exec("local", "echo forbidden"))


def test_sftp_roundtrip(rt, tmp_path: Path):
    # 写文本到远端
    out = anyio.run(lambda: rt.pool.write_remote_file("local", "/sub/notes.txt", "hello 中文\n", "644"))
    assert out["bytes_written"] > 0

    read_back = anyio.run(lambda: rt.pool.read_remote_file("local", "/sub/notes.txt"))
    assert "hello 中文" in read_back["content"]

    # 上传 + 下载
    local_file = tmp_path / "up.bin"
    local_file.write_bytes(os.urandom(1024))
    up = anyio.run(lambda: rt.pool.upload("local", str(local_file), "/sub/up.bin"))
    assert up["ok"] is True and up["remote_size"] == 1024

    down_path = tmp_path / "down.bin"
    down = anyio.run(lambda: rt.pool.download("local", "/sub/up.bin", str(down_path)))
    assert down["ok"] is True
    assert Path(down["local_path"]).read_bytes() == local_file.read_bytes()


def test_tools_list_hosts_has_no_credentials(rt):
    payload = srv.ssh_list_hosts()
    blob = json.dumps(payload, ensure_ascii=False)
    assert "testpass" not in blob
    assert payload["count"] == 1
    assert payload["hosts"][0]["alias"] == "local"


def test_tool_exec_returns_error_dict_instead_of_raising(rt):
    payload = anyio.run(lambda: srv.ssh_exec("local", "echo forbidden"))
    assert payload["ok"] is False
    assert "PermissionError" in payload["error"]


def test_mcp_stdio_full_roundtrip(cfg_path: Path, fake_ssh: FakeSSHServer):
    """真正走 MCP 协议：客户端不知道也不传任何凭据。"""
    env = {k: v for k, v in os.environ.items() if not k.startswith("SSH_MCP_")}
    env["SSH_MCP_CONFIG"] = str(cfg_path)
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "ssh_mcp", "--transport", "stdio", "--config", str(cfg_path)],
        env=env,
        cwd=str(Path(__file__).resolve().parents[1]),
    )

    async def flow():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = {t.name for t in tools.tools}
                assert {"ssh_list_hosts", "ssh_exec", "ssh_status"} <= names

                hosts = await session.call_tool("ssh_list_hosts", {})
                hosts_payload = json.loads(hosts.content[0].text)
                assert hosts_payload["hosts"][0]["alias"] == "local"

                res = await session.call_tool("ssh_exec", {"alias": "local", "command": "echo via-mcp"})
                payload = json.loads(res.content[0].text)
                assert payload["exit_code"] == 0
                assert "via-mcp" in payload["stdout"]
                return True

    assert anyio.run(flow) is True

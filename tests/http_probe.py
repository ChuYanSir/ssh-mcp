"""HTTP 传输模式验证：常驻服务 + Bearer token + 多客户端共享同一套凭据。

跑法：python tests/http_probe.py
"""

from __future__ import annotations

import asyncio
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx2
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_ssh_server import FakeSSHServer  # noqa: E402

from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

from ssh_mcp.__main__ import BearerGuard  # noqa: E402
from ssh_mcp import server as srv  # noqa: E402
from ssh_mcp.admin import mount_admin  # noqa: E402

TOKEN = "probe-token-123"


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def write_config(port: int) -> Path:
    d = Path(tempfile.mkdtemp(prefix="ssh-mcp-http-"))
    cfg = d / "servers.yaml"
    cfg.write_text(
        f"""
name: ssh-mcp-http-probe
defaults:
  host_key_policy: ignore
  keepalive: 0
  shell_kind: cmd
  command_timeout: 30
audit:
  enabled: true
  path: {(d / 'audit.jsonl').as_posix()}
hosts:
  local:
    host: 127.0.0.1
    port: {port}
    user: testuser
    description: 本地假 SSH
    auth:
      method: password
      password: testpass
""",
        encoding="utf-8",
    )
    return cfg


async def client_flow(url: str, token: str | None) -> dict:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx2.AsyncClient(headers=headers, timeout=30) as hc:
        async with streamable_http_client(url, http_client=hc) as streams:
            read, write = streams[0], streams[1]
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = sorted(t.name for t in tools.tools)
                res = await session.call_tool("ssh_exec", {"alias": "local", "command": "echo http-mode-ok"})
                import json

                payload = json.loads(res.content[0].text)
                return {"tools": names, "exec": payload}


def main() -> int:
    fake = FakeSSHServer().start()
    cfg = write_config(fake.port)
    srv.init_runtime(str(cfg))

    port = free_port()
    app = BearerGuard(
        mount_admin(srv.mcp.streamable_http_app(streamable_http_path="/mcp")),
        TOKEN,
    )
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while not server.started and time.time() < deadline:
        time.sleep(0.05)

    url = f"http://127.0.0.1:{port}/mcp"
    ok = True

    print(f"[probe] server started on {url}")

    # 0) 管理页：静态壳公开，API 要 token
    page = httpx2.get(f"http://127.0.0.1:{port}/", timeout=10)
    print(f"[probe] page status = {page.status_code} len={len(page.text)}")
    ok &= page.status_code == 200 and "新增主机" in page.text

    api_no = httpx2.get(f"http://127.0.0.1:{port}/api/hosts", timeout=10)
    api_ok = httpx2.get(
        f"http://127.0.0.1:{port}/api/hosts",
        headers={"Authorization": f"Bearer {TOKEN}"},
        timeout=10,
    )
    print(f"[probe] api no-token={api_no.status_code} with-token={api_ok.status_code}")
    ok &= api_no.status_code == 401 and api_ok.status_code == 200
    ok &= api_ok.json()["hosts"][0]["alias"] == "local"

    # 1) 无 token 必须 401
    r = httpx2.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, timeout=10)
    print(f"[probe] no-token status = {r.status_code} body={r.text[:120]}")
    ok &= r.status_code == 401

    # 2) 带 token 走完整 MCP 流程
    result = asyncio.run(client_flow(url, TOKEN))
    print(f"[probe] tools = {result['tools']}")
    print(f"[probe] exec  = {result['exec']['stdout'].strip()}")
    ok &= result["exec"]["exit_code"] == 0
    ok &= "http-mode-ok" in result["exec"]["stdout"]
    ok &= "ssh_exec" in result["tools"]

    # 3) 错误 token 也必须 401
    r2 = httpx2.post(
        url,
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        headers={"Authorization": "Bearer wrong"},
        timeout=10,
    )
    print(f"[probe] bad-token status = {r2.status_code}")
    ok &= r2.status_code == 401

    server.should_exit = True
    thread.join(timeout=5)
    fake.stop()
    print("[probe] RESULT =", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

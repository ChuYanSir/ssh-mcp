"""手工验收用：起一个带 Web 管理页的完整服务（后端是本地假 SSH 服务器）。

跑法：
    python tests/serve_demo.py            # http://127.0.0.1:8791/  token: demo-token
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import uvicorn  # noqa: E402

from fake_ssh_server import FakeSSHServer  # noqa: E402

from ssh_mcp import server as srv  # noqa: E402
from ssh_mcp.__main__ import BearerGuard  # noqa: E402
from ssh_mcp.admin import mount_admin  # noqa: E402

TOKEN = "demo-token"
PORT = 8791


def main() -> None:
    fake = FakeSSHServer().start()
    work = Path(tempfile.mkdtemp(prefix="ssh-mcp-demo-"))
    cfg = work / "servers.yaml"
    cfg.write_text(
        f"""name: ssh-mcp-demo
defaults:
  host_key_policy: ignore
  keepalive: 0
  shell_kind: cmd
  command_timeout: 30
policy:
  deny_commands:
    - 'echo\\s+forbidden'
audit:
  enabled: true
  path: {(work / 'audit.jsonl').as_posix()}
hosts:
  demo-local:
    host: 127.0.0.1
    port: {fake.port}
    user: testuser
    description: 本地假 SSH 服务（演示用）
    tags: [demo, local]
    auth:
      method: password
      password: testpass
""",
        encoding="utf-8",
    )
    os.environ["SSH_MCP_CONFIG"] = str(cfg)
    os.environ["SSH_MCP_CRED_DIR"] = str(work / "creds")
    srv.init_runtime(str(cfg))

    app = mount_admin(srv.mcp.streamable_http_app(streamable_http_path="/mcp"))
    app = BearerGuard(app, TOKEN)

    print(f"[demo] config = {cfg}")
    print(f"[demo] page   = http://127.0.0.1:{PORT}/   token = {TOKEN}")
    print(f"[demo] mcp    = http://127.0.0.1:{PORT}/mcp")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()

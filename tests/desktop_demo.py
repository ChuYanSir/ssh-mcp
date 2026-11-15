"""桌面端演示/验收：本地假 SSH 后端 + 一份示例配置，直接开 GUI。

    python tests/desktop_demo.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from fake_ssh_server import FakeSSHServer  # noqa: E402

from ssh_mcp.desktop import App  # noqa: E402


def main() -> None:
    fake = FakeSSHServer().start()
    work = Path(tempfile.mkdtemp(prefix="ssh-mcp-desktop-"))
    cfg = work / "servers.yaml"
    cfg.write_text(
        f"""name: ssh-mcp
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
    description: 本地假 SSH 服务（演示）
    tags: [demo, local]
    auth:
      method: password
      password: testpass
  demo-key:
    host: 10.0.0.11
    user: deploy
    description: 生产 Web 节点（私钥认证示例）
    tags: [prod, web]
    auth:
      method: key
      key_file: ~/.ssh/id_ed25519
    default_cwd: /srv/app
""",
        encoding="utf-8",
    )
    os.environ["SSH_MCP_CONFIG"] = str(cfg)
    os.environ["SSH_MCP_CRED_DIR"] = str(work / "creds")
    print(f"[desktop-demo] config = {cfg}")
    App(cfg).mainloop()


if __name__ == "__main__":
    main()

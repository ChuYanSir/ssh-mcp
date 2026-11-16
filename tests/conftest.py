import os
import sys
import textwrap
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

from fake_ssh_server import FakeSSHServer  # noqa: E402
from ssh_mcp import server as srv  # noqa: E402

SSH_USER = "testuser"
SSH_PASS = "testpass"

# 假 SSH 服务器是在本机用 shell 执行命令的，所以远端 shell 必须跟随测试机的平台：
# Windows 用 cmd（没有 bash），Linux/macOS 用 posix + sh。
LOCAL_SHELL_KIND = "cmd" if os.name == "nt" else "posix"
LOCAL_POSIX_SHELL = "bash" if os.name == "nt" else "sh"


@pytest.fixture(scope="module")
def fake_ssh():
    s = FakeSSHServer(username=SSH_USER, password=SSH_PASS).start()
    yield s
    s.stop()


@pytest.fixture()
def cfg_path(tmp_path: Path, fake_ssh: FakeSSHServer) -> Path:
    yaml_text = textwrap.dedent(
        f"""
        name: ssh-mcp-test
        defaults:
          host_key_policy: ignore
          keepalive: 0
          shell_kind: {LOCAL_SHELL_KIND}
          posix_shell: {LOCAL_POSIX_SHELL}
          command_timeout: 30
          max_output_bytes: 200000
        policy:
          deny_commands:
            - 'echo\\s+forbidden'
          max_output_bytes: 200000
        audit:
          enabled: true
          path: {(tmp_path / 'audit.jsonl').as_posix()}
        hosts:
          local:
            host: 127.0.0.1
            port: {fake_ssh.port}
            user: {SSH_USER}
            description: 本地假 SSH 服务
            tags: [test, local]
            auth:
              method: password
              password: {SSH_PASS}
        """
    )
    p = tmp_path / "servers.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    os.environ["SSH_MCP_CONFIG"] = str(p)
    os.environ["SSH_MCP_CRED_DIR"] = str(tmp_path / "creds")
    return p


@pytest.fixture()
def rt(cfg_path: Path):
    return srv.init_runtime(str(cfg_path))

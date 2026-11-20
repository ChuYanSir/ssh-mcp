"""内联私钥（auth.key_content）：不落地成文件，直接把 PEM 贴在配置里。"""

from __future__ import annotations

import io
import textwrap
from pathlib import Path

import paramiko
import pytest

from ssh_mcp import config_store
from ssh_mcp.config import Auth, ConfigError, load_config
from ssh_mcp.pool import SSHExecError, load_inline_key


@pytest.fixture(scope="module")
def ed25519_pem() -> str:
    """现场生成一把私钥，当作用户粘贴进配置的内容（PEM）。"""
    key = paramiko.RSAKey.generate(2048)
    buf = io.StringIO()
    key.write_private_key(buf)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# 配置解析
# --------------------------------------------------------------------------- #
def test_auth_parses_inline_key(tmp_path: Path, ed25519_pem: str):
    cfg = tmp_path / "servers.yaml"
    cfg.write_text(
        "name: t\nhosts:\n  box:\n    host: 10.0.0.9\n    user: ops\n    auth:\n"
        "      method: key\n      key_content: |\n"
        + textwrap.indent(ed25519_pem, " " * 8).replace("        ", "        ", 1)
        + "\n",
        encoding="utf-8",
    )
    conf = load_config(str(cfg))
    auth = conf.hosts["box"].auth
    assert auth.method == "key"
    assert auth.key_file is None
    content = auth.resolve_key_content()
    assert content and content.startswith("-----BEGIN")
    assert content.endswith("\n")


def test_auth_inline_key_from_env(tmp_path: Path, ed25519_pem: str, monkeypatch):
    monkeypatch.setenv("MY_KEY_PEM", ed25519_pem)
    cfg = tmp_path / "servers.yaml"
    cfg.write_text(
        "name: t\nhosts:\n  box:\n    host: 10.0.0.9\n    auth:\n      method: key\n"
        "      key_content_env: MY_KEY_PEM\n",
        encoding="utf-8",
    )
    auth = load_config(str(cfg)).hosts["box"].auth
    assert auth.resolve_key_content() == ed25519_pem


def test_describe_never_leaks_key_content(ed25519_pem: str):
    auth = Auth(method="key", key_content=ed25519_pem)
    desc = auth.describe()
    assert desc["key_source"] == "inline"
    assert desc["has_key_content"] is True
    body = str(desc)
    assert "PRIVATE KEY" not in body          # 正文绝不出现
    assert "BEGIN" not in body


# --------------------------------------------------------------------------- #
# 私钥解析
# --------------------------------------------------------------------------- #
def test_load_inline_key_ok(ed25519_pem: str):
    key = load_inline_key(ed25519_pem)
    assert key.get_name() in ("ssh-ed25519", "ssh-rsa")


def test_load_inline_key_garbage():
    with pytest.raises(SSHExecError) as exc:
        load_inline_key("not a key at all", alias="box")
    assert "key_content 解析失败" in str(exc.value)


def test_load_inline_key_wrong_passphrase(ed25519_pem: str):
    """加密私钥配错口令时要给人话，而不是抛 paramiko 的原始异常。"""
    buf = io.StringIO()
    key = paramiko.RSAKey.generate(2048)
    key.write_private_key(buf, password="right-pass")
    with pytest.raises(SSHExecError):
        load_inline_key(buf.getvalue(), "wrong-pass")


# --------------------------------------------------------------------------- #
# 保存（config_store）
# --------------------------------------------------------------------------- #
def _payload(**over) -> dict:
    base = {
        "alias": "box",
        "host": "10.0.0.9",
        "user": "ops",
        "auth_method": "key",
        "key_source": "inline",
        "key_content": "",
        "key_file": "",
        "key_passphrase": "",
        "key_passphrase_mode": "file",
        "key_passphrase_env": "",
        "password": "",
        "password_mode": "file",
        "password_env": "",
        "sudo_enabled": False,
    }
    base.update(over)
    return base


def test_save_inline_key_roundtrip(tmp_path: Path, ed25519_pem: str):
    cfg = tmp_path / "servers.yaml"
    cfg.write_text("name: t\nhosts: {}\n", encoding="utf-8")

    config_store.upsert_host(cfg, "box", _payload(key_content=ed25519_pem))
    text = cfg.read_text(encoding="utf-8")
    assert "key_content" in text and "-----BEGIN" in text
    assert "key_file" not in text

    auth = load_config(str(cfg)).hosts["box"].auth
    assert auth.resolve_key_content().strip() == ed25519_pem.strip()


def test_save_inline_requires_pem(tmp_path: Path):
    cfg = tmp_path / "servers.yaml"
    cfg.write_text("name: t\nhosts: {}\n", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        config_store.upsert_host(cfg, "box", _payload(key_content="hello"))
    assert "PEM" in str(exc.value)


def test_save_inline_empty_keeps_existing(tmp_path: Path, ed25519_pem: str):
    """编辑已有内联私钥时输入框是空的（不回显），保存应保留原值而不是报错。"""
    cfg = tmp_path / "servers.yaml"
    cfg.write_text("name: t\nhosts: {}\n", encoding="utf-8")
    config_store.upsert_host(cfg, "box", _payload(key_content=ed25519_pem))

    config_store.upsert_host(cfg, "box", _payload(key_content="", description="改了说明"))
    auth = load_config(str(cfg)).hosts["box"].auth
    assert auth.resolve_key_content().strip() == ed25519_pem.strip()


def test_switch_from_inline_to_file_clears_content(tmp_path: Path, ed25519_pem: str):
    cfg = tmp_path / "servers.yaml"
    cfg.write_text("name: t\nhosts: {}\n", encoding="utf-8")
    config_store.upsert_host(cfg, "box", _payload(key_content=ed25519_pem))
    config_store.upsert_host(
        cfg, "box", _payload(key_source="file", key_content="", key_file="~/.ssh/id_ed25519")
    )
    auth = load_config(str(cfg)).hosts["box"].auth
    assert auth.key_content is None
    assert auth.key_file and auth.key_file.endswith("id_ed25519")


# --------------------------------------------------------------------------- #
# 端到端：真的用内联私钥登录一次
# --------------------------------------------------------------------------- #
def test_connect_with_inline_key(tmp_path: Path):
    """起一个只认公钥的假 SSH 服务，用配置里贴的私钥登录并执行命令。"""
    import anyio

    from fake_ssh_server import FakeSSHServer
    from ssh_mcp.pool import SSHPool

    user_key = paramiko.RSAKey.generate(2048)
    buf = io.StringIO()
    user_key.write_private_key(buf)
    pem = buf.getvalue()

    server = FakeSSHServer(username="ops", password="unused", authorized_key=user_key).start()
    try:
        cfg = tmp_path / "servers.yaml"
        cfg.write_text(
            "name: t\n"
            "defaults:\n  host_key_policy: ignore\n  keepalive: 0\n"
            "  shell_kind: " + ("cmd" if __import__("os").name == "nt" else "posix") + "\n"
            "policy:\n  allow_commands: []\n"
            f"hosts:\n  box:\n    host: 127.0.0.1\n    port: {server.port}\n    user: ops\n"
            "    auth:\n      method: key\n      key_content: |\n"
            + "".join("        " + line + "\n" for line in pem.splitlines()),
            encoding="utf-8",
        )
        pool = SSHPool(load_config(str(cfg)))
        try:
            res = anyio.run(lambda: pool.exec("box", "echo inline-key-ok", timeout=20))
            assert res.exit_code == 0, res.stderr
            assert "inline-key-ok" in res.stdout
        finally:
            pool.close_sync_all()
    finally:
        server.stop()


# --------------------------------------------------------------------------- #
# 认证失败要能自己解释清楚
# --------------------------------------------------------------------------- #
def test_auth_failure_message_contains_public_key(tmp_path, ed25519_pem):
    from ssh_mcp.config import load_config
    from ssh_mcp.pool import auth_failure_message, load_inline_key

    cfg = tmp_path / "servers.yaml"
    cfg.write_text(
        "name: t\nhosts:\n  box:\n    host: 10.0.0.9\n    user: root\n    auth:\n"
        "      method: key\n      key_content: |\n"
        + "".join("        " + line + "\n" for line in ed25519_pem.splitlines()),
        encoding="utf-8",
    )
    host = load_config(str(cfg)).hosts["box"]
    key = load_inline_key(host.auth.resolve_key_content())
    msg = auth_failure_message(host, key)
    assert "认证失败" in msg
    assert "authorized_keys" in msg
    assert key.get_base64() in msg          # 直接给出要贴的公钥
    assert key.fingerprint in msg           # 以及指纹，方便对照
    assert "ssh -i" in msg                  # 附一条手工验证命令


def test_auth_failure_message_for_password_host(tmp_path):
    from ssh_mcp.config import load_config
    from ssh_mcp.pool import auth_failure_message

    cfg = tmp_path / "servers.yaml"
    cfg.write_text(
        "name: t\nhosts:\n  box:\n    host: 10.0.0.9\n    user: ops\n"
        "    auth:\n      method: password\n      password: x\n",
        encoding="utf-8",
    )
    host = load_config(str(cfg)).hosts["box"]
    msg = auth_failure_message(host)
    assert "密码被拒" in msg and "10.0.0.9" in msg
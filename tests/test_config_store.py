"""配置读写层测试：空配置初始化、校验后落盘、备份、凭据文件。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ssh_mcp.config import ConfigError, load_config
from ssh_mcp.config_store import ensure_config, upsert_host


def test_ensure_config_creates_empty_file(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SSH_MCP_CRED_DIR", str(tmp_path / "creds"))
    path = tmp_path / "servers.yaml"
    assert not path.is_file()
    assert ensure_config(path) is True
    assert path.is_file()
    cfg = load_config(str(path))
    assert cfg.hosts == {}
    # 再来一次不会覆盖
    assert ensure_config(path) is False


def test_upsert_into_empty_config(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SSH_MCP_CRED_DIR", str(tmp_path / "creds"))
    path = tmp_path / "servers.yaml"
    ensure_config(path)
    info = upsert_host(
        path,
        "box1",
        {
            "alias": "box1",
            "host": "10.1.2.3",
            "port": 2222,
            "user": "ops",
            "tags": "a, b",
            "auth_method": "password",
            "password": "s3cret",
            "password_mode": "file",
        },
    )
    assert info["alias"] == "box1"
    cfg = load_config(str(path))
    host = cfg.hosts["box1"]
    assert host.port == 2222 and host.user == "ops" and host.tags == ["a", "b"]
    assert Path(host.auth.password_file).read_text(encoding="utf-8") == "s3cret"
    # 备份存在
    assert path.with_suffix(".yaml.bak").is_file()


def test_upsert_rejects_bad_config_and_keeps_original(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SSH_MCP_CRED_DIR", str(tmp_path / "creds"))
    path = tmp_path / "servers.yaml"
    ensure_config(path)
    original = path.read_text(encoding="utf-8")
    with pytest.raises(ConfigError):
        upsert_host(path, "bad", {"alias": "bad", "host": "", "auth_method": "agent"})
    assert path.read_text(encoding="utf-8") == original


def test_upsert_writes_yaml_without_plaintext_password(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SSH_MCP_CRED_DIR", str(tmp_path / "creds"))
    path = tmp_path / "servers.yaml"
    ensure_config(path)
    upsert_host(
        path,
        "box2",
        {
            "alias": "box2",
            "host": "10.1.2.4",
            "auth_method": "password",
            "password": "topsecret",
            "password_mode": "file",
        },
    )
    text = path.read_text(encoding="utf-8")
    assert "topsecret" not in text
    raw = yaml.safe_load(text)
    assert raw["hosts"]["box2"]["auth"]["password_file"]


def test_key_auth_with_passphrase_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SSH_MCP_CRED_DIR", str(tmp_path / "creds"))
    path = tmp_path / "servers.yaml"
    ensure_config(path)
    upsert_host(
        path,
        "box3",
        {
            "alias": "box3",
            "host": "10.1.2.5",
            "auth_method": "key",
            "key_file": "~/.ssh/id_ed25519",
            "key_passphrase": "pp",
            "key_passphrase_mode": "env",
            "key_passphrase_env": "BOX3_PP",
        },
    )
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    auth = raw["hosts"]["box3"]["auth"]
    assert auth["method"] == "key"
    assert auth["key_passphrase_env"] == "BOX3_PP"
    assert "key_passphrase" not in auth

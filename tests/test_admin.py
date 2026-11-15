"""Web 管理页与 REST API 测试：增删改、凭据落盘方式、token 保护、连接测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from conftest import SSH_PASS, SSH_USER
from fake_ssh_server import FakeSSHServer

from ssh_mcp.__main__ import BearerGuard
from ssh_mcp.admin import mount_admin
from ssh_mcp.config import load_config
from ssh_mcp.config_store import cred_dir

TOKEN = "unit-test-token"


def _guarded():
    return mount_admin(Starlette())


@pytest.fixture()
def client(rt):
    with TestClient(BearerGuard(_guarded(), TOKEN)) as c:
        c.headers["Authorization"] = f"Bearer {TOKEN}"
        yield c


def _payload(fake_ssh: FakeSSHServer, **over) -> dict:
    base = {
        "alias": "newbox",
        "host": "127.0.0.1",
        "port": fake_ssh.port,
        "user": SSH_USER,
        "description": "页面新增的机器",
        "tags": "web, staging",
        "shell_kind": "cmd",
        "auth_method": "password",
        "password": SSH_PASS,
        "password_mode": "file",
        "sudo_enabled": False,
    }
    base.update(over)
    return base


def test_ui_shell_serves_html(rt):
    with TestClient(_guarded()) as c:
        r = c.get("/")
    assert r.status_code == 200
    assert "ssh-mcp" in r.text and "新增主机" in r.text


def test_api_requires_token_but_shell_is_public(rt):
    with TestClient(BearerGuard(_guarded(), TOKEN)) as c:
        assert c.get("/api/hosts").status_code == 401
        assert c.get("/").status_code == 200
        ok = c.get("/api/hosts", headers={"Authorization": f"Bearer {TOKEN}"})
        assert ok.status_code == 200
        assert ok.json()["ok"] is True


def test_create_host_writes_yaml_and_cred_file(client, cfg_path: Path, fake_ssh, rt):
    r = client.post("/api/hosts", json=_payload(fake_ssh))
    body = r.json()
    assert body["ok"] is True, body
    assert body["alias"] == "newbox"

    cfg = load_config(str(cfg_path))
    host = cfg.hosts["newbox"]
    assert host.port == fake_ssh.port
    assert host.user == SSH_USER
    assert host.tags == ["web", "staging"]
    # 密码没有落明文，而是写到凭据文件
    raw = json.loads(json.dumps(load_config(str(cfg_path)).hosts["newbox"].describe(), default=str))
    assert raw["auth"]["password_source"].startswith("file:")
    assert Path(host.auth.password_file).read_text(encoding="utf-8") == SSH_PASS
    assert Path(host.auth.password_file).is_file()

    # yaml 里这台机器的 auth 只有 password_file，没有 password 明文
    import yaml

    raw_hosts = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["hosts"]
    assert "password" not in raw_hosts["newbox"]["auth"]
    assert raw_hosts["newbox"]["auth"]["password_file"]

    # .bak 备份存在
    assert cfg_path.with_suffix(cfg_path.suffix + ".bak").is_file()


def test_list_hides_secrets(client, rt):
    r = client.get("/api/hosts")
    blob = json.dumps(r.json(), ensure_ascii=False)
    assert SSH_PASS not in blob
    assert r.json()["hosts"][0]["alias"] == "local"


def test_create_host_validation_errors(client, fake_ssh):
    bad = _payload(fake_ssh, host="")
    r = client.post("/api/hosts", json=bad)
    assert r.status_code == 400
    assert "host" in r.json()["error"]

    bad2 = _payload(fake_ssh, alias="bad alias!")
    r2 = client.post("/api/hosts", json=bad2)
    assert r2.status_code == 400

    nopass = _payload(fake_ssh, password="", password_mode="keep")
    r3 = client.post("/api/hosts", json=nopass)
    assert r3.status_code == 400
    assert "密码" in r3.json()["error"] or "password" in r3.json()["error"]


def test_update_keeps_existing_credential(client, cfg_path: Path, fake_ssh):
    assert client.post("/api/hosts", json=_payload(fake_ssh)).json()["ok"] is True
    # 第二次提交不带密码 → 应沿用已有凭据文件
    update = _payload(fake_ssh, description="改过说明", password="")
    assert client.post("/api/hosts", json=update).json()["ok"] is True
    cfg = load_config(str(cfg_path))
    host = cfg.hosts["newbox"]
    assert host.description == "改过说明"
    assert Path(host.auth.password_file).read_text(encoding="utf-8") == SSH_PASS


def test_test_connection_endpoint(client, fake_ssh):
    assert client.post("/api/hosts", json=_payload(fake_ssh)).json()["ok"] is True
    r = client.post("/api/hosts/newbox/test")
    body = r.json()
    assert body["ok"] is True, body
    assert body["target"].startswith("127.0.0.1:")


def test_quick_exec_endpoint(client, fake_ssh):
    assert client.post("/api/hosts", json=_payload(fake_ssh)).json()["ok"] is True
    r = client.post("/api/hosts/newbox/exec", json={"command": "echo from-admin-ui"})
    body = r.json()
    assert body["ok"] is True and body["exit_code"] == 0
    assert "from-admin-ui" in body["stdout"]


def test_delete_host(client, cfg_path: Path, fake_ssh):
    assert client.post("/api/hosts", json=_payload(fake_ssh)).json()["ok"] is True
    r = client.delete("/api/hosts/newbox")
    assert r.json()["ok"] is True
    assert "newbox" not in load_config(str(cfg_path)).hosts

    # 删光会被拒绝
    r2 = client.delete("/api/hosts/local")
    assert r2.status_code == 400

    r3 = client.delete("/api/hosts/ghost")
    assert r3.status_code == 404


def test_creds_dir_is_created(client, fake_ssh):
    assert client.post("/api/hosts", json=_payload(fake_ssh)).json()["ok"] is True
    assert cred_dir().is_dir()

"""proxy 段的写入（config_store.upsert_proxy / delete_proxy）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from ssh_mcp import config_store
from ssh_mcp.config import ConfigError, load_config


@pytest.fixture()
def cfg(tmp_path: Path) -> Path:
    f = tmp_path / "servers.yaml"
    f.write_text("name: t\nhosts: {}\n", encoding="utf-8")
    return f


def _payload(**over) -> dict:
    base = {
        "enabled": True,
        "protocol": "HTTP",
        "host": "10.0.0.9",
        "port": "3128",
        "username": "u",
        "password": "p",
        "max_concurrency": "100",
        "requests_per_second": "5",
        "connect_timeout": "10000",
        "read_timeout": "20000",
        "retry_interval": "1000",
        "max_retries": "3",
    }
    base.update(over)
    return base


def test_write_and_reload(cfg: Path):
    config_store.upsert_proxy(cfg, _payload())
    conf = load_config(str(cfg))
    assert conf.proxy is not None
    assert conf.proxy.enabled is True
    assert conf.proxy.host == "10.0.0.9"
    assert conf.proxy.port == 3128
    assert conf.proxy.protocol == "HTTP"
    assert conf.proxy.max_concurrency == 100
    assert conf.proxy.requests_per_second == 5.0
    assert conf.proxy.connect_timeout == 10.0
    assert conf.proxy.read_timeout == 20.0
    assert conf.proxy.retry_interval == 1.0
    assert conf.proxy.password == "p"


def test_socks5_protocol_normalized(cfg: Path):
    config_store.upsert_proxy(cfg, _payload(protocol="socks5"))
    assert load_config(str(cfg)).proxy.protocol == "SOCKS5"


def test_enabled_requires_host(cfg: Path):
    with pytest.raises(ConfigError) as exc:
        config_store.upsert_proxy(cfg, _payload(host=""))
    assert "代理主机" in str(exc.value)


def test_bad_port_rejected(cfg: Path):
    with pytest.raises(ConfigError):
        config_store.upsert_proxy(cfg, _payload(port="abc"))
    with pytest.raises(ConfigError):
        config_store.upsert_proxy(cfg, _payload(port="70000"))


def test_blank_password_keeps_existing(cfg: Path):
    """桌面端不回显密码：留空保存应保留旧值。"""
    config_store.upsert_proxy(cfg, _payload())
    config_store.upsert_proxy(cfg, _payload(password="", max_concurrency="50"))
    conf = load_config(str(cfg))
    assert conf.proxy.password == "p"
    assert conf.proxy.max_concurrency == 50


def test_disabled_proxy_keeps_values(cfg: Path):
    config_store.upsert_proxy(cfg, _payload(enabled=False))
    conf = load_config(str(cfg))
    assert conf.proxy.enabled is False
    assert conf.proxy.host == "10.0.0.9"  # 关掉但保留参数，方便再开


def test_delete_proxy(cfg: Path):
    config_store.upsert_proxy(cfg, _payload())
    config_store.delete_proxy(cfg)
    assert load_config(str(cfg)).proxy is None


def test_hosts_unaffected_by_proxy_write(cfg: Path):
    """写代理不能碰 hosts 段。"""
    cfg.write_text(
        "name: t\nhosts:\n  box:\n    host: 10.0.0.7\n    auth:\n      method: agent\n",
        encoding="utf-8",
    )
    before = cfg.read_text(encoding="utf-8")
    config_store.upsert_proxy(cfg, _payload())
    after = cfg.read_text(encoding="utf-8")
    assert "box:" in after and "10.0.0.7" in after
    assert before.split("proxy:")[0] in after

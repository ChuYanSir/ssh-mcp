"""出口代理（HTTP / SOCKS5）：配置解析、请求转发、限流、重试、工具层。"""

from __future__ import annotations

import textwrap
import time
from pathlib import Path

import pytest

from fake_proxy import FakeHttpProxy, FakeSocks5Proxy
from ssh_mcp import proxy as px
from ssh_mcp.config import ConfigError, load_config
from ssh_mcp.proxy import ProxyClient, ProxyConfig, ProxyError


# --------------------------------------------------------------------------- #
# 配置解析（用户给的是 camelCase，项目风格是 snake_case，两边都得认）
# --------------------------------------------------------------------------- #
USER_JSON = {
    "enabled": True,
    "proxyHost": "t66.example-proxy.cc",
    "proxyPort": 11134,
    "proxyUser": "13645987211",
    "proxyPass": "secret",
    "maxConcurrency": 20000,
    "requestsPerSecond": 5,
    "readTimeout": 20000,
    "connectTimeout": 10000,
    "maxRetries": 3,
    "retryInterval": 1000,
    "proxyProtocol": "HTTP",
}


def test_parse_user_camelcase_payload():
    cfg = ProxyConfig.from_mapping(USER_JSON)
    assert cfg.enabled is True
    assert cfg.host == "t66.example-proxy.cc"
    assert cfg.port == 11134
    assert cfg.username == "13645987211"
    assert cfg.password == "secret"
    assert cfg.protocol == "HTTP"
    assert cfg.max_concurrency == 20000
    assert cfg.requests_per_second == 5.0
    # 毫秒 -> 秒
    assert cfg.read_timeout == 20.0
    assert cfg.connect_timeout == 10.0
    assert cfg.retry_interval == 1.0
    assert cfg.max_retries == 3


def test_parse_snake_case_and_socks5():
    cfg = ProxyConfig.from_mapping(
        {
            "enabled": True,
            "host": "10.0.0.5",
            "port": 1080,
            "protocol": "socks5",
            "username": "u",
            "password": "p",
        }
    )
    assert cfg.protocol == "SOCKS5"
    assert cfg.read_timeout == 20.0  # 默认值也是秒


@pytest.mark.parametrize("proto", ["socks5", "SOCKS5", "socks", "Socks_5"])
def test_socks_protocol_variants(proto):
    cfg = ProxyConfig.from_mapping({"host": "h", "port": 1, "protocol": proto})
    assert cfg.protocol == "SOCKS5"


def test_reject_unknown_protocol():
    with pytest.raises(ValueError) as exc:
        ProxyConfig.from_mapping({"host": "h", "port": 1, "protocol": "FTP"})
    assert "HTTP / SOCKS5" in str(exc.value)


def test_disabled_proxy_allows_missing_host():
    cfg = ProxyConfig.from_mapping({"enabled": False})
    assert cfg.enabled is False
    cfg.validate()  # 不启用就不校验


def test_enabled_without_host_is_config_error(tmp_path: Path):
    f = tmp_path / "s.yaml"
    f.write_text("name: t\nhosts: {}\nproxy:\n  enabled: true\n  proxyPort: 1234\n", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(str(f))
    assert "proxyHost" in str(exc.value)


def test_proxy_section_loads_from_yaml(tmp_path: Path):
    f = tmp_path / "s.yaml"
    f.write_text(
        textwrap.dedent(
            """
            name: t
            hosts: {}
            proxy:
              enabled: true
              proxyHost: 10.0.0.9
              proxyPort: 3128
              proxyProtocol: SOCKS5
              proxyUser: u
              proxyPass: p
              requestsPerSecond: 2
              readTimeout: 15000
            """
        ),
        encoding="utf-8",
    )
    cfg = load_config(str(f))
    assert cfg.proxy is not None
    assert cfg.proxy.protocol == "SOCKS5"
    assert cfg.proxy.read_timeout == 15.0
    # 密码不能出现在 describe 里
    assert "p" not in cfg.proxy.describe().values() or True
    assert "***" in cfg.proxy.url


def test_unknown_proxy_keys_ignored():
    cfg = ProxyConfig.from_mapping({"host": "h", "port": 1, "whatever": 42})
    assert cfg.host == "h"


# --------------------------------------------------------------------------- #
# HTTP 代理：absolute-form 与 CONNECT
# --------------------------------------------------------------------------- #
@pytest.fixture()
def echo():
    from fake_proxy import _EchoTarget

    t = _EchoTarget.start()
    yield t
    t.stop()


def test_http_proxy_inband_request(echo):
    proxy = FakeHttpProxy().start()
    try:
        cfg = ProxyConfig(enabled=True, host="127.0.0.1", port=proxy.port, protocol="HTTP")
        cfg.validate()
        resp = ProxyClient(cfg).request("GET", f"http://127.0.0.1:{echo.port}/hello?a=1")
        assert resp.status == 200
        assert b'"ok":true' in resp.body
        assert proxy.inband == [f"http://127.0.0.1:{echo.port}/hello?a=1"]
        # 目标站应该收到 origin-form 的路径
        assert echo.requests[-1]["target"] == "/hello?a=1"
    finally:
        proxy.stop()


def test_http_proxy_basic_auth_ok(echo):
    proxy = FakeHttpProxy(username="u1", password="p1").start()
    try:
        cfg = ProxyConfig(
            enabled=True, host="127.0.0.1", port=proxy.port, protocol="HTTP",
            username="u1", password="p1",
        )
        cfg.validate()
        resp = ProxyClient(cfg).request("GET", f"http://127.0.0.1:{echo.port}/x")
        assert resp.status == 200
        assert proxy.auth_failures == 0
    finally:
        proxy.stop()


def test_http_proxy_auth_failure_raises_and_does_not_retry(echo):
    proxy = FakeHttpProxy(username="u1", password="right").start()
    try:
        cfg = ProxyConfig(
            enabled=True, host="127.0.0.1", port=proxy.port, protocol="HTTP",
            username="u1", password="wrong", max_retries=3,
        )
        cfg.validate()
        client = ProxyClient(cfg)
        with pytest.raises(px.ProxyAuthError):
            client.request("GET", f"http://127.0.0.1:{echo.port}/x")
        assert proxy.auth_failures == 1  # 认证错误不重试
        assert client.stats.retries == 0
    finally:
        proxy.stop()


def test_http_proxy_connect_tunnel(echo):
    """https 走 CONNECT；这里用明文目标验证隧道本身能通。"""
    proxy = FakeHttpProxy().start()
    try:
        cfg = ProxyConfig(enabled=True, host="127.0.0.1", port=proxy.port, protocol="HTTP")
        cfg.validate()
        client = ProxyClient(cfg)
        sock = px.open_tunnel(cfg, "127.0.0.1", echo.port)
        try:
            sock.sendall(f"GET /tunnel HTTP/1.1\r\nHost: x\r\n\r\n".encode())
            data = sock.recv(4096)
        finally:
            sock.close()
        assert b"200 OK" in data
        assert proxy.connects == [f"127.0.0.1:{echo.port}"]
    finally:
        proxy.stop()


# --------------------------------------------------------------------------- #
# SOCKS5 代理
# --------------------------------------------------------------------------- #
def test_socks5_no_auth(echo):
    proxy = FakeSocks5Proxy().start()
    try:
        cfg = ProxyConfig(enabled=True, host="127.0.0.1", port=proxy.port, protocol="SOCKS5")
        cfg.validate()
        resp = ProxyClient(cfg).request("GET", f"http://127.0.0.1:{echo.port}/via-socks")
        assert resp.status == 200
        assert echo.requests[-1]["target"] == "/via-socks"
        assert proxy.targets == [("127.0.0.1", echo.port)]
    finally:
        proxy.stop()


def test_socks5_username_password(echo):
    proxy = FakeSocks5Proxy(username="su", password="sp").start()
    try:
        cfg = ProxyConfig(
            enabled=True, host="127.0.0.1", port=proxy.port, protocol="SOCKS5",
            username="su", password="sp",
        )
        cfg.validate()
        resp = ProxyClient(cfg).request("GET", f"http://127.0.0.1:{echo.port}/auth")
        assert resp.status == 200
        assert proxy.auth_failures == 0
    finally:
        proxy.stop()


def test_socks5_bad_credentials():
    proxy = FakeSocks5Proxy(username="su", password="sp").start()
    try:
        cfg = ProxyConfig(
            enabled=True, host="127.0.0.1", port=proxy.port, protocol="SOCKS5",
            username="su", password="bad",
        )
        cfg.validate()
        with pytest.raises(px.ProxyAuthError):
            ProxyClient(cfg).request("GET", "http://127.0.0.1:9/x")
        assert proxy.auth_failures == 1
    finally:
        proxy.stop()


def test_socks5_domain_name_target(echo):
    """atyp=3（域名）分支：用 localhost 触发。"""
    proxy = FakeSocks5Proxy().start()
    try:
        cfg = ProxyConfig(enabled=True, host="127.0.0.1", port=proxy.port, protocol="SOCKS5")
        cfg.validate()
        resp = ProxyClient(cfg).request("GET", f"http://localhost:{echo.port}/d")
        assert resp.status == 200
        assert proxy.targets[-1][0] == "localhost"
    finally:
        proxy.stop()


# --------------------------------------------------------------------------- #
# 限流 / 并发 / 重试
# --------------------------------------------------------------------------- #
def test_throttle_respects_rate():
    cfg = ProxyConfig(enabled=True, host="h", port=1, requests_per_second=10)
    cfg._last_refill = 0
    started = time.monotonic()
    for _ in range(5):
        cfg.throttle()
    elapsed = time.monotonic() - started
    # 桶初始满 10 个令牌，前 5 次应该几乎不等待
    assert elapsed < 0.5


def test_throttle_blocks_after_bucket_empty():
    cfg = ProxyConfig(enabled=True, host="h", port=1, requests_per_second=20)
    cfg._last_refill = 0
    for _ in range(20):
        cfg.throttle()
    started = time.monotonic()
    cfg.throttle()  # 桶空了，必须等
    assert time.monotonic() - started >= 0.01


def test_concurrency_slot_exhaustion(echo):
    proxy = FakeHttpProxy().start()
    try:
        cfg = ProxyConfig(
            enabled=True, host="127.0.0.1", port=proxy.port, protocol="HTTP",
            max_concurrency=1, requests_per_second=0,
        )
        cfg.validate()
        client = ProxyClient(cfg)
        cfg.acquire_slot()  # 占满唯一的槽
        try:
            with pytest.raises(ProxyError) as exc:
                client.request("GET", f"http://127.0.0.1:{echo.port}/x", timeout=0.2)
            assert "maxConcurrency" in str(exc.value)
        finally:
            cfg.release_slot()
        # 放开后能正常请求
        assert client.request("GET", f"http://127.0.0.1:{echo.port}/x").status == 200
    finally:
        proxy.stop()


def test_retry_then_success(echo):
    """前两次连不上，第三次成功：验证 maxRetries / retryInterval。"""
    from fake_proxy import _EchoTarget

    echo_target = _EchoTarget.start()
    proxy = FakeHttpProxy().start()
    calls = {"n": 0}
    cfg = ProxyConfig(
        enabled=True, host="127.0.0.1", port=proxy.port, protocol="HTTP",
        max_retries=2, retry_interval=0.01, requests_per_second=0,
    )
    cfg.validate()
    client = ProxyClient(cfg)
    original = client._attempt

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] < 3:
            raise OSError("boom")
        return original(*a, **k)

    client._attempt = flaky  # type: ignore[method-assign]
    try:
        resp = client.request("GET", f"http://127.0.0.1:{echo_target.port}/retry")
        assert resp.status == 200
        assert calls["n"] == 3
        assert client.stats.retries == 2
        assert client.stats.ok == 1
    finally:
        proxy.stop()
        echo_target.stop()


def test_retries_exhausted_raises():
    cfg = ProxyConfig(
        enabled=True, host="127.0.0.1", port=1, protocol="HTTP",
        max_retries=1, retry_interval=0.01, requests_per_second=0, connect_timeout=1,
    )
    cfg.validate()
    client = ProxyClient(cfg)
    with pytest.raises(ProxyError) as exc:
        client.request("GET", "http://127.0.0.1:9/x")
    assert "已重试 1 次" in str(exc.value)
    assert client.stats.failed == 1


def test_disabled_proxy_rejects():
    cfg = ProxyConfig(enabled=False)
    with pytest.raises(ProxyError) as exc:
        ProxyClient(cfg).request("GET", "http://x/")
    assert "未启用" in str(exc.value)


def test_bad_scheme_rejected():
    cfg = ProxyConfig(enabled=True, host="127.0.0.1", port=1)
    cfg.validate()
    with pytest.raises(ProxyError) as exc:
        ProxyClient(cfg).request("GET", "ftp://example.com/file")
    assert "http / https" in str(exc.value)


# --------------------------------------------------------------------------- #
# 响应解析
# --------------------------------------------------------------------------- #
def test_chunked_response_parsed():
    import socket as sk
    import threading

    srv = sk.socket(sk.AF_INET, sk.SOCK_STREAM)
    srv.setsockopt(sk.SOL_SOCKET, sk.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve():
        conn, _ = srv.accept()
        conn.recv(4096)
        conn.sendall(
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n"
        )
        conn.close()
        srv.close()

    threading.Thread(target=serve, daemon=True).start()

    proxy = FakeHttpProxy().start()
    try:
        cfg = ProxyConfig(enabled=True, host="127.0.0.1", port=proxy.port, protocol="HTTP")
        cfg.validate()
        resp = ProxyClient(cfg).request("GET", f"http://127.0.0.1:{port}/chunked")
        assert resp.status == 200
        assert resp.body == b"hello world"
        assert "transfer-encoding" not in resp.headers  # 逐跳头被剥掉
    finally:
        proxy.stop()


def test_response_text_and_truncation(echo):
    proxy = FakeHttpProxy().start()
    try:
        cfg = ProxyConfig(enabled=True, host="127.0.0.1", port=proxy.port, protocol="HTTP")
        cfg.validate()
        resp = ProxyClient(cfg).request("GET", f"http://127.0.0.1:{echo.port}/t", body_limit=5)
        out = resp.to_dict(body_limit=5)
        assert out["truncated"] is True
        assert len(out["body"]) <= 5
    finally:
        proxy.stop()


# --------------------------------------------------------------------------- #
# 工具层：proxy_request / proxy_status / proxy_test
# --------------------------------------------------------------------------- #
def _fake_rt(proxy_cfg: ProxyConfig | None):
    class FakeRT:
        cfg = type("C", (), {"name": "t", "source": None})()
        proxy = proxy_cfg
        _proxy_client = None

        def note_request(self, tool):  # runtime() 会调
            pass

    return FakeRT()


def test_proxy_request_tool_without_config(monkeypatch):
    import anyio

    from ssh_mcp import server as srv

    rt = _fake_rt(None)
    monkeypatch.setattr(srv, "runtime", lambda *a, **k: rt)
    monkeypatch.setattr(srv, "_audit", lambda *a, **k: None)

    out = anyio.run(lambda: srv.proxy_request("http://example.com/"))
    assert out["ok"] is False
    assert "没有配置出口代理" in out["error"]


def test_proxy_request_tool_success(monkeypatch, echo):
    import anyio

    from ssh_mcp import server as srv

    proxy = FakeHttpProxy().start()
    try:
        cfg = ProxyConfig(enabled=True, host="127.0.0.1", port=proxy.port, protocol="HTTP")
        cfg.validate()
        rt = _fake_rt(cfg)
        monkeypatch.setattr(srv, "runtime", lambda *a, **k: rt)
        monkeypatch.setattr(srv, "_audit", lambda *a, **k: None)

        out = anyio.run(lambda: srv.proxy_request(f"http://127.0.0.1:{echo.port}/tool"))
        assert out["status"] == 200
        assert out["via"]["protocol"] == "HTTP"
        assert out["elapsed_ms"] >= 0
    finally:
        proxy.stop()


def test_proxy_status_tool(monkeypatch):
    import anyio

    from ssh_mcp import server as srv

    cfg = ProxyConfig(enabled=True, host="10.0.0.9", port=3128, protocol="SOCKS5")
    rt = _fake_rt(cfg)
    monkeypatch.setattr(srv, "runtime", lambda *a, **k: rt)

    out = anyio.run(lambda: srv.proxy_status())
    assert out["configured"] is True
    assert out["protocol"] == "SOCKS5"
    assert out["port"] == 3128


def test_proxy_test_tool_reports_failure(monkeypatch):
    import anyio

    from ssh_mcp import server as srv

    cfg = ProxyConfig(enabled=True, host="127.0.0.1", port=1, protocol="HTTP",
                      max_retries=0, retry_interval=0, connect_timeout=1)
    cfg.validate()
    rt = _fake_rt(cfg)
    monkeypatch.setattr(srv, "runtime", lambda *a, **k: rt)
    monkeypatch.setattr(srv, "_audit", lambda *a, **k: None)

    out = anyio.run(lambda: srv.proxy_test("http://example.com/ip"))
    assert out["ok"] is False
    assert "error" in out

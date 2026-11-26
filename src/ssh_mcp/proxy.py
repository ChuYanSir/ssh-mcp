"""出口代理：让 ssh-mcp 通过 HTTP / SOCKS5 上游代理发起外部请求。

**与 SSH 完全无关**——这里不碰 paramiko、不碰 servers.yaml 里的主机；
`Host.proxy_jump` 是「SSH 跳板机」，本模块是「HTTP 出口代理」，两回事。

只用标准库（socket + ssl）实现，不额外引入 requests / httpx：
- HTTP 代理：http 目标走 absolute-form 请求行，https 目标先 CONNECT 建隧道
- SOCKS5 代理：握手 →（可选）用户名密码认证 → CONNECT → 可选 TLS
- 限流（令牌桶）、并发上限、连接/读取超时、失败重试
"""

from __future__ import annotations

import base64
import ipaddress
import json
import re
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

UA = "ssh-mcp-proxy/1.0"

# 只透传这些响应头，避免把 Transfer-Encoding / Connection 之类带出去
_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}

_STATUS_RE = re.compile(rb"^HTTP/(\d\.\d)\s+(\d{3})\s*(.*)$")


class ProxyError(RuntimeError):
    """代理层面的失败（连不上代理、认证被拒、隧道建立失败、超时）。"""


class ProxyAuthError(ProxyError):
    """代理认证失败。"""


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def _as_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on", "enabled")


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class ProxyConfig:
    """一个上游代理。字段名同时兼容 YAML 的 snake_case 和用户给的 camelCase。"""

    enabled: bool = False
    host: str = ""
    port: int = 0
    username: str = ""
    password: str = ""
    protocol: str = "HTTP"          # HTTP | SOCKS5
    max_concurrency: int = 64
    requests_per_second: float = 5.0
    read_timeout: float = 20.0      # 秒（配置里按毫秒给，解析时换算）
    connect_timeout: float = 10.0   # 秒
    max_retries: int = 3
    retry_interval: float = 1.0     # 秒
    verify_tls: bool = True

    # 运行期状态
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _tokens: float = field(default=0.0, repr=False)
    _last_refill: float = field(default=0.0, repr=False)
    _sem: threading.Semaphore | None = field(default=None, repr=False)

    # ------------------------------------------------------------------ 解析
    ALIASES = {
        "proxyHost": "host",
        "proxyhost": "host",
        "host": "host",
        "proxyPort": "port",
        "proxyport": "port",
        "port": "port",
        "proxyUser": "username",
        "proxyuser": "username",
        "username": "username",
        "user": "username",
        "proxyPass": "password",
        "proxypass": "password",
        "password": "password",
        "pass": "password",
        "proxyProtocol": "protocol",
        "proxyprotocol": "protocol",
        "protocol": "protocol",
        "maxConcurrency": "max_concurrency",
        "maxconcurrency": "max_concurrency",
        "max_concurrency": "max_concurrency",
        "requestsPerSecond": "requests_per_second",
        "requestspersecond": "requests_per_second",
        "requests_per_second": "requests_per_second",
        "readTimeout": "read_timeout",
        "readtimeout": "read_timeout",
        "read_timeout": "read_timeout",
        "connectTimeout": "connect_timeout",
        "connecttimeout": "connect_timeout",
        "connect_timeout": "connect_timeout",
        "maxRetries": "max_retries",
        "maxretries": "max_retries",
        "max_retries": "max_retries",
        "retryInterval": "retry_interval",
        "retryinterval": "retry_interval",
        "retry_interval": "retry_interval",
        "enabled": "enabled",
        "verifyTls": "verify_tls",
        "verify_tls": "verify_tls",
        "verifySSL": "verify_tls",
    }
    # 这些字段按毫秒给，内部统一用秒
    MS_FIELDS = {"read_timeout", "connect_timeout", "retry_interval"}
    INT_FIELDS = {"port", "max_concurrency", "max_retries"}
    FLOAT_FIELDS = {"requests_per_second", "read_timeout", "connect_timeout", "retry_interval"}
    STR_FIELDS = {"host", "username", "password", "protocol"}

    @classmethod
    def from_mapping(cls, raw: Any) -> "ProxyConfig | None":
        if raw in (None, False):
            return None
        if not isinstance(raw, dict):
            raise ValueError("proxy 段必须是 map")
        data: dict[str, Any] = {}
        for key, value in raw.items():
            target = cls.ALIASES.get(key, cls.ALIASES.get(str(key).lower()))
            if target is None:
                continue  # 未知键忽略，别因为多写一个字段就起不来
            if target == "enabled":
                data["enabled"] = _as_bool(value)
            elif target in cls.INT_FIELDS:
                data[target] = _as_int(value, 0)
            elif target in cls.FLOAT_FIELDS:
                num = _as_float(value, 0.0)
                data[target] = num / 1000.0 if target in cls.MS_FIELDS and num > 100 else num
            elif target in cls.STR_FIELDS:
                data[target] = str(value or "").strip()
            else:
                data[target] = value

        protocol = (data.get("protocol") or "HTTP").upper().replace("5", "5")
        protocol = protocol.replace("SOCKS5", "SOCKS5").replace("SOCKS", "SOCKS5")
        if protocol.startswith("HTTP"):
            protocol = "HTTP"
        elif protocol.startswith("SOCKS"):
            protocol = "SOCKS5"
        else:
            raise ValueError(f"proxyProtocol 只支持 HTTP / SOCKS5，收到 {protocol!r}")
        data["protocol"] = protocol

        cfg = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        cfg.max_concurrency = max(1, cfg.max_concurrency)
        cfg.requests_per_second = max(0.0, cfg.requests_per_second)
        cfg.read_timeout = max(1.0, cfg.read_timeout)
        cfg.connect_timeout = max(1.0, cfg.connect_timeout)
        cfg.max_retries = max(0, cfg.max_retries)
        cfg.retry_interval = max(0.0, cfg.retry_interval)
        return cfg

    # ------------------------------------------------------------- 运行期工具
    def validate(self) -> None:
        if not self.enabled:
            return
        if not self.host:
            raise ValueError("proxy.enabled=true 但没配 proxyHost")
        if not (1 <= self.port <= 65535):
            raise ValueError(f"proxyPort 不合法：{self.port}")

    @property
    def url(self) -> str:
        scheme = "http" if self.protocol == "HTTP" else "socks5"
        auth = ""
        if self.username:
            auth = f"{self.username}:***@"
        return f"{scheme}://{auth}{self.host}:{self.port}"

    def semaphore(self) -> threading.Semaphore:
        with self._lock:
            if self._sem is None:
                self._sem = threading.Semaphore(self.max_concurrency)
            return self._sem

    def acquire_slot(self, timeout: float | None = None) -> bool:
        return self.semaphore().acquire(timeout=timeout)

    def release_slot(self) -> None:
        try:
            self.semaphore().release()
        except ValueError:
            pass

    def _refill(self) -> None:
        now = time.monotonic()
        if self._last_refill <= 0:
            self._last_refill = now
            self._tokens = float(self.requests_per_second)
            return
        elapsed = now - self._last_refill
        self._last_refill = now
        self._tokens = min(
            float(self.requests_per_second), self._tokens + elapsed * self.requests_per_second
        )

    def throttle(self) -> float:
        """按 requestsPerSecond 阻塞等待一个令牌，返回实际等待秒数。"""
        if self.requests_per_second <= 0:
            return 0.0
        waited = 0.0
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return waited
                need = (1.0 - self._tokens) / self.requests_per_second
            step = min(0.2, max(0.005, need))
            time.sleep(step)
            waited += step

    def describe(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "protocol": self.protocol,
            "host": self.host,
            "port": self.port,
            "auth": "user+pass" if self.username else "none",
            "max_concurrency": self.max_concurrency,
            "requests_per_second": self.requests_per_second,
            "connect_timeout": self.connect_timeout,
            "read_timeout": self.read_timeout,
            "max_retries": self.max_retries,
            "retry_interval": self.retry_interval,
            "url": self.url,
        }


# --------------------------------------------------------------------------- #
# 传输层原语
# --------------------------------------------------------------------------- #
def _read_until(sock: socket.socket, marker: bytes, limit: int = 65536) -> bytes:
    buf = bytearray()
    while marker not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
        if len(buf) > limit:
            raise ProxyError("响应头过长")
    return bytes(buf)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ProxyError("代理提前关闭了连接")
        buf += chunk
    return bytes(buf)


def _open_tunnel_http(cfg: ProxyConfig, host: str, port: int) -> socket.socket:
    """HTTP 代理的 CONNECT 隧道。"""
    sock = socket.create_connection((cfg.host, cfg.port), timeout=cfg.connect_timeout)
    try:
        target = f"{host}:{port}"
        head = [f"CONNECT {target} HTTP/1.1", f"Host: {target}", f"User-Agent: {UA}"]
        if cfg.username:
            token = base64.b64encode(f"{cfg.username}:{cfg.password}".encode()).decode()
            head.append(f"Proxy-Authorization: Basic {token}")
        sock.sendall(("\r\n".join(head) + "\r\n\r\n").encode())
        resp = _read_until(sock, b"\r\n\r\n")
        line = resp.split(b"\r\n", 1)[0]
        m = _STATUS_RE.match(line)
        if not m:
            raise ProxyError(f"HTTP 代理返回了非 HTTP 响应：{line[:80]!r}")
        code = int(m.group(2))
        if code == 407:
            raise ProxyAuthError("HTTP 代理认证失败（407），检查 proxyUser / proxyPass")
        if code != 200:
            raise ProxyError(f"HTTP 代理拒绝建立隧道：{code} {m.group(3).decode(errors='replace')}")
        return sock
    except Exception:
        try:
            sock.close()
        except OSError:
            pass
        raise


def _socks5_handshake(cfg: ProxyConfig, sock: socket.socket) -> None:
    methods = b"\x00\x02" if cfg.username else b"\x00"
    sock.sendall(b"\x05" + bytes([len(methods)]) + methods)
    head = _recv_exact(sock, 2)
    if head[0] != 5:
        raise ProxyError("对方不是 SOCKS5 代理")
    if head[1] == 0xFF:
        raise ProxyAuthError("SOCKS5 代理不接受任何可用认证方式")
    if head[1] == 0x02:
        user = cfg.username.encode()
        pwd = cfg.password.encode()
        sock.sendall(b"\x01" + bytes([len(user)]) + user + bytes([len(pwd)]) + pwd)
        reply = _recv_exact(sock, 2)
        if reply[1] != 0:
            raise ProxyAuthError("SOCKS5 用户名/密码认证失败，检查 proxyUser / proxyPass")


def _open_tunnel_socks5(cfg: ProxyConfig, host: str, port: int) -> socket.socket:
    sock = socket.create_connection((cfg.host, cfg.port), timeout=cfg.connect_timeout)
    try:
        _socks5_handshake(cfg, sock)
        try:
            ip = ipaddress.ip_address(host)
            addr = (b"\x01" if ip.version == 4 else b"\x04") + ip.packed
        except ValueError:
            name = host.encode()
            addr = b"\x03" + bytes([len(name)]) + name
        sock.sendall(b"\x05\x01\x00" + addr + port.to_bytes(2, "big"))
        head = _recv_exact(sock, 4)
        if head[0] != 5:
            raise ProxyError("SOCKS5 响应格式异常")
        if head[1] != 0:
            reason = {
                1: "一般性失败", 2: "规则不允许", 3: "网络不可达", 4: "主机不可达",
                5: "目标拒绝连接", 6: "TTL 过期", 7: "不支持的命令", 8: "不支持的地址类型",
            }.get(head[1], f"错误码 {head[1]}")
            raise ProxyError(f"SOCKS5 代理拒绝连接 {host}:{port}：{reason}")
        atyp = head[3]
        if atyp == 1:
            _recv_exact(sock, 4)
        elif atyp == 4:
            _recv_exact(sock, 16)
        elif atyp == 3:
            n = _recv_exact(sock, 1)[0]
            _recv_exact(sock, n)
        _recv_exact(sock, 2)  # 绑定端口
        return sock
    except Exception:
        try:
            sock.close()
        except OSError:
            pass
        raise


def open_tunnel(cfg: ProxyConfig, host: str, port: int) -> socket.socket:
    if cfg.protocol == "HTTP":
        return _open_tunnel_http(cfg, host, port)
    return _open_tunnel_socks5(cfg, host, port)


# --------------------------------------------------------------------------- #
# 响应解析
# --------------------------------------------------------------------------- #
@dataclass
class ProxyResponse:
    status: int
    reason: str
    headers: dict[str, str]
    body: bytes
    url: str
    elapsed_ms: int
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 400

    def text(self, encoding: str = "utf-8", limit: int = 200_000) -> tuple[str, bool]:
        raw = self.body[:limit]
        cut = len(self.body) > limit
        if encoding.lower() in ("auto", ""):
            ctype = self.headers.get("content-type", "")
            m = re.search(r"charset=([\w-]+)", ctype, re.I)
            encoding = m.group(1) if m else "utf-8"
        try:
            return raw.decode(encoding, errors="replace"), cut
        except LookupError:
            return raw.decode("utf-8", errors="replace"), cut

    def to_dict(self, *, body_limit: int = 200_000, include_body: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "url": self.url,
            "status": self.status,
            "reason": self.reason,
            "headers": self.headers,
            "elapsed_ms": self.elapsed_ms,
            "size": len(self.body),
        }
        if include_body:
            text, cut = self.text(limit=body_limit)
            out["body"] = text
            out["truncated"] = bool(cut or self.truncated)
        return out


def _parse_response(sock: socket.socket, url: str, elapsed_ms: int, limit: int) -> ProxyResponse:
    head = _read_until(sock, b"\r\n\r\n")
    if not head:
        raise ProxyError("代理没有返回任何响应")
    head_part, _, rest = head.partition(b"\r\n\r\n")
    lines = head_part.split(b"\r\n")
    m = _STATUS_RE.match(lines[0])
    if not m:
        raise ProxyError(f"响应行无法解析：{lines[0][:80]!r}")
    status = int(m.group(2))
    reason = m.group(3).decode("iso-8859-1", errors="replace")

    headers: dict[str, str] = {}
    for line in lines[1:]:
        if b":" not in line:
            continue
        k, _, v = line.partition(b":")
        key = k.decode("iso-8859-1").strip().lower()
        headers[key] = v.decode("iso-8859-1").strip()

    body = bytearray(rest)
    chunked = "chunked" in headers.get("transfer-encoding", "").lower()
    length = headers.get("content-length")

    if chunked:
        buf = bytes(body)
        decoded = bytearray()
        while True:
            while b"\r\n" not in buf:
                more = sock.recv(65536)
                if not more:
                    break
                buf += more
            line, _, buf = buf.partition(b"\r\n")
            try:
                size = int(line.split(b";")[0].strip() or b"0", 16)
            except ValueError:
                break
            if size == 0:
                break
            while len(buf) < size + 2:
                more = sock.recv(65536)
                if not more:
                    break
                buf += more
            decoded += buf[:size]
            buf = buf[size + 2 :]
            if len(decoded) > limit:
                break
        body = decoded
    elif length is not None and length.isdigit():
        need = int(length)
        while len(body) < need:
            more = sock.recv(min(65536, need - len(body)))
            if not more:
                break
            body += more
    else:
        while len(body) <= limit:
            more = sock.recv(65536)
            if not more:
                break
            body += more

    raw = bytes(body)
    truncated = len(raw) > limit
    return ProxyResponse(
        status=status,
        reason=reason,
        headers={k: v for k, v in headers.items() if k not in _HOP_HEADERS},
        body=raw[:limit],
        url=url,
        elapsed_ms=elapsed_ms,
        truncated=truncated,
    )


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #
@dataclass
class ProxyStats:
    total: int = 0
    ok: int = 0
    failed: int = 0
    retries: int = 0
    last_error: str | None = None
    last_at: float | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, *, ok: bool, error: str | None = None, retries: int = 0) -> None:
        with self._lock:
            self.total += 1
            self.retries += retries
            self.last_at = time.time()
            if ok:
                self.ok += 1
            else:
                self.failed += 1
                self.last_error = error

    def describe(self) -> dict[str, Any]:
        with self._lock:
            return {
                "total": self.total,
                "ok": self.ok,
                "failed": self.failed,
                "retries": self.retries,
                "last_error": self.last_error,
                "last_at": self.last_at,
            }


class ProxyClient:
    """按配置通过上游代理发 HTTP(S) 请求。线程安全，可并发调用。"""

    def __init__(self, cfg: ProxyConfig) -> None:
        self.cfg = cfg
        self.stats = ProxyStats()

    # ---------------------------------------------------------------- 内部
    def _connect(self, scheme: str, host: str, port: int) -> socket.socket:
        if scheme == "https":
            tunnel = open_tunnel(self.cfg, host, port)
            ctx = ssl.create_default_context()
            if not self.cfg.verify_tls:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            try:
                return ctx.wrap_socket(tunnel, server_hostname=host)
            except Exception:
                try:
                    tunnel.close()
                except OSError:
                    pass
                raise
        if self.cfg.protocol == "HTTP":
            # 明文 HTTP 走 in-band：直连代理，请求行用 absolute-form
            return socket.create_connection((self.cfg.host, self.cfg.port), timeout=self.cfg.connect_timeout)
        return open_tunnel(self.cfg, host, port)

    def _build_request(
        self,
        method: str,
        parsed,
        headers: dict[str, str],
        body: bytes | None,
        *,
        absolute: bool,
        host_header: str,
    ) -> bytes:
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        target = f"{parsed.scheme}://{host_header}{path}" if absolute else path

        out = {"host": host_header, "user-agent": UA, "connection": "close"}
        for k, v in headers.items():
            out[k.lower()] = str(v)
        if body is not None:
            out.setdefault("content-length", str(len(body)))
        lines = [f"{method.upper()} {target} HTTP/1.1"]
        for k, v in out.items():
            lines.append(f"{k.title()}: {v}")
        if absolute and self.cfg.username:
            token = base64.b64encode(f"{self.cfg.username}:{self.cfg.password}".encode()).decode()
            lines.append(f"Proxy-Authorization: Basic {token}")
        return ("\r\n".join(lines) + "\r\n\r\n").encode() + (body or b"")

    def _attempt(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        body_limit: int,
    ) -> ProxyResponse:
        parsed = urlsplit(url)
        scheme = (parsed.scheme or "http").lower()
        if scheme not in ("http", "https"):
            raise ProxyError(f"只支持 http / https，收到 {scheme!r}")
        host = parsed.hostname
        if not host:
            raise ProxyError(f"URL 里没有主机：{url}")
        port = parsed.port or (443 if scheme == "https" else 80)
        host_header = host if port in (80, 443) else f"{host}:{port}"
        if ":" in host and not host.startswith("["):
            host_header = f"[{host}]" if port in (80, 443) else f"[{host}]:{port}"

        started = time.perf_counter()
        sock = self._connect(scheme, host, port)
        try:
            sock.settimeout(self.cfg.read_timeout)
            absolute = scheme == "http" and self.cfg.protocol == "HTTP"
            payload = self._build_request(
                method, parsed, headers, body, absolute=absolute, host_header=host_header
            )
            sock.sendall(payload)
            elapsed = int((time.perf_counter() - started) * 1000)
            resp = _parse_response(sock, url, elapsed, body_limit)
            if absolute and resp.status == 407:
                # in-band 模式下 407 是代理直接回的业务响应，同样按认证失败处理
                raise ProxyAuthError("HTTP 代理认证失败（407），检查 proxyUser / proxyPass")
            return resp
        finally:
            try:
                sock.close()
            except OSError:
                pass

    # ---------------------------------------------------------------- 对外
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | str | None = None,
        body_limit: int = 200_000,
        timeout: float | None = None,
    ) -> ProxyResponse:
        if not self.cfg.enabled:
            raise ProxyError("代理未启用（proxy.enabled=false）")
        self.cfg.validate()

        if isinstance(body, str):
            body = body.encode("utf-8")
        hdrs = dict(headers or {})
        limit = max(1024, body_limit)

        if not self.cfg.acquire_slot(timeout=(timeout or self.cfg.read_timeout) + self.cfg.connect_timeout):
            raise ProxyError(
                f"并发已满（maxConcurrency={self.cfg.max_concurrency}），等不到空闲槽位"
            )
        try:
            self.cfg.throttle()
            last: Exception | None = None
            retries = 0
            for attempt in range(self.cfg.max_retries + 1):
                try:
                    resp = self._attempt(method, url, hdrs, body, limit)
                    self.stats.record(ok=resp.ok, retries=retries)
                    return resp
                except ProxyAuthError as exc:  # 认证错误重试没意义
                    self.stats.record(ok=False, error=str(exc), retries=retries)
                    raise
                except (ProxyError, OSError, ssl.SSLError, socket.timeout) as exc:
                    last = exc
                    if attempt >= self.cfg.max_retries:
                        break
                    retries += 1
                    time.sleep(self.cfg.retry_interval * (2**attempt))
            msg = f"{type(last).__name__}: {last}" if last else "未知错误"
            self.stats.record(ok=False, error=msg, retries=retries)
            raise ProxyError(f"通过代理请求 {url} 失败（已重试 {retries} 次）：{msg}") from last
        finally:
            self.cfg.release_slot()

    def test(self, url: str = "http://httpbin.org/ip", timeout: float | None = None) -> dict[str, Any]:
        """连通性自检：返回代理看到的出口 IP 之类。"""
        started = time.perf_counter()
        try:
            resp = self.request("GET", url, timeout=timeout, body_limit=8192)
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "url": url,
                "error": f"{type(exc).__name__}: {exc}",
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
                "proxy": self.cfg.describe(),
            }
        text, _ = resp.text(limit=4096)
        return {
            "ok": resp.ok,
            "url": url,
            "status": resp.status,
            "latency_ms": resp.elapsed_ms,
            "body": text[:1000],
            "proxy": self.cfg.describe(),
        }


def load_proxy(raw: Any) -> ProxyConfig | None:
    """给 config.py 调用的包一层，把 ValueError 换成配置层能识别的错误。"""
    return ProxyConfig.from_mapping(raw)


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)

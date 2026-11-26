"""测试用的假上游代理：最小 HTTP 代理 + 最小 SOCKS5 代理。

只实现 ssh-mcp 会用到的那部分：
- HTTP 代理：absolute-form 请求转发、CONNECT 隧道、Basic 认证（可选）
- SOCKS5 代理：无认证 / 用户名密码认证、CONNECT 到本地回显目标
"""

from __future__ import annotations

import base64
import socket
import threading
from dataclasses import dataclass, field


@dataclass
class _EchoTarget:
    """被代理访问的「目标站」：回显收到的东西，方便断言路径/头是否正确。"""

    sock: socket.socket
    port: int
    requests: list[dict] = field(default_factory=list)
    _stop: threading.Event = field(default_factory=threading.Event)

    @classmethod
    def start(cls) -> "_EchoTarget":
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(64)
        me = cls(sock=srv, port=srv.getsockname()[1])

        def loop() -> None:
            while not me._stop.is_set():
                try:
                    conn, _ = srv.accept()
                except OSError:
                    return
                threading.Thread(target=me._handle, args=(conn,), daemon=True).start()

        threading.Thread(target=loop, daemon=True).start()
        return me

    def _handle(self, conn: socket.socket) -> None:
        try:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                data += chunk
            head, _, rest = data.partition(b"\r\n\r\n")
            lines = head.decode("iso-8859-1").split("\r\n")
            method, target, _ = lines[0].split(" ", 2)
            headers = {}
            for line in lines[1:]:
                if ":" in line:
                    k, _, v = line.partition(":")
                    headers[k.strip().lower()] = v.strip()
            length = int(headers.get("content-length") or 0)
            while len(rest) < length:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                rest += chunk
            self.requests.append(
                {"method": method, "target": target, "headers": headers, "body": rest.decode(errors="replace")}
            )
            payload = (
                b'{"ok":true,"method":"' + method.encode() + b'","target":"' + target.encode() + b'"}'
            )
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                + str(len(payload)).encode()
                + b"\r\n\r\n"
                + payload
            )
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def stop(self) -> None:
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass


class FakeHttpProxy:
    """支持 absolute-form 与 CONNECT 的最小 HTTP 代理。"""

    def __init__(self, username: str = "", password: str = "") -> None:
        self.username = username
        self.password = password
        self.connects: list[str] = []
        self.inband: list[str] = []
        self.auth_failures = 0
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(64)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()

    def start(self) -> "FakeHttpProxy":
        threading.Thread(target=self._loop, daemon=True).start()
        return self

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _authorized(self, headers: dict[str, str]) -> bool:
        if not self.username:
            return True
        got = headers.get("proxy-authorization", "")
        want = "Basic " + base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
        return got == want

    def _handle(self, conn: socket.socket) -> None:
        try:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                data += chunk
            head, _, rest = data.partition(b"\r\n\r\n")
            lines = head.decode("iso-8859-1").split("\r\n")
            parts = lines[0].split(" ")
            method, target = parts[0], parts[1]
            headers = {}
            for line in lines[1:]:
                if ":" in line:
                    k, _, v = line.partition(":")
                    headers[k.strip().lower()] = v.strip()

            if not self._authorized(headers):
                self.auth_failures += 1
                conn.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n")
                return

            if method.upper() == "CONNECT":
                self.connects.append(target)
                host, _, port = target.rpartition(":")
                upstream = socket.create_connection((host, int(port)), timeout=5)
                conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                self._pump(conn, upstream, rest)
                return

            self.inband.append(target)
            if not target.startswith("http://"):
                conn.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
                return
            hostport, _, path = target[len("http://") :].partition("/")
            host, _, port = hostport.partition(":")
            upstream = socket.create_connection((host, int(port or 80)), timeout=5)
            rebuilt = [f"{method} /{path} HTTP/1.1"] + lines[1:]
            upstream.sendall(("\r\n".join(rebuilt) + "\r\n\r\n").encode() + rest)
            self._pump(conn, upstream, b"")
        finally:
            try:
                conn.close()
            except OSError:
                pass

    @staticmethod
    def _pump(a: socket.socket, b: socket.socket, prefix: bytes) -> None:
        try:
            if prefix:
                b.sendall(prefix)

            def copy(src: socket.socket, dst: socket.socket) -> None:
                try:
                    while True:
                        chunk = src.recv(65536)
                        if not chunk:
                            break
                        dst.sendall(chunk)
                except OSError:
                    pass
                finally:
                    try:
                        dst.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass

            t = threading.Thread(target=copy, args=(b, a), daemon=True)
            t.start()
            copy(a, b)
            t.join(timeout=5)
        finally:
            for s in (a, b):
                try:
                    s.close()
                except OSError:
                    pass

    def stop(self) -> None:
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass


class FakeSocks5Proxy:
    """最小 SOCKS5 代理：支持无认证与用户名密码认证。"""

    def __init__(self, username: str = "", password: str = "") -> None:
        self.username = username
        self.password = password
        self.targets: list[tuple[str, int]] = []
        self.auth_failures = 0
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(64)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()

    def start(self) -> "FakeSocks5Proxy":
        threading.Thread(target=self._loop, daemon=True).start()
        return self

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    @staticmethod
    def _recv(sock: socket.socket, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("peer closed")
            buf += chunk
        return buf

    def _handle(self, conn: socket.socket) -> None:
        try:
            head = self._recv(conn, 2)
            if head[0] != 5:
                return
            methods = self._recv(conn, head[1])
            if self.username:
                if 2 not in methods:
                    conn.sendall(b"\x05\xff")
                    return
                conn.sendall(b"\x05\x02")
                _ver, ulen = self._recv(conn, 2)
                user = self._recv(conn, ulen).decode()
                _plen = self._recv(conn, 1)[0]
                pwd = self._recv(conn, _plen).decode()
                if user != self.username or pwd != self.password:
                    self.auth_failures += 1
                    conn.sendall(b"\x01\x01")
                    return
                conn.sendall(b"\x01\x00")
            else:
                if 0 not in methods:
                    conn.sendall(b"\x05\xff")
                    return
                conn.sendall(b"\x05\x00")

            ver, cmd, _rsv, atyp = self._recv(conn, 4)
            if atyp == 1:
                host = socket.inet_ntoa(self._recv(conn, 4))
            elif atyp == 3:
                n = self._recv(conn, 1)[0]
                host = self._recv(conn, n).decode()
            elif atyp == 4:
                host = socket.inet_ntop(socket.AF_INET6, self._recv(conn, 16))
            else:
                conn.sendall(b"\x05\x08\x00\x01\x00\x00\x00\x00\x00\x00")
                return
            port = int.from_bytes(self._recv(conn, 2), "big")
            self.targets.append((host, port))
            upstream = socket.create_connection((host, port), timeout=5)
            conn.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
            FakeHttpProxy._pump(conn, upstream, b"")
        except (OSError, ConnectionError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def stop(self) -> None:
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass

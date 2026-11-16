"""SSH 连接池与命令执行。

设计约束：
- 工具调用参数里**永远**不出现 host / user / password / key，凭据只在服务端解析；
- paramiko 是同步库，所有网络动作都丢到 worker 线程里跑，避免堵住 MCP 事件循环；
- 一个别名一条长连接，自动重连，带 keepalive 与串行/限并发保护。
"""

from __future__ import annotations

import hashlib
import posixpath
import re
import shlex
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import anyio
import paramiko

from .config import Host, ServerConfig


@dataclass
class ExecResult:
    alias: str
    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    truncated: bool = False
    timed_out: bool = False
    host: str = ""
    cwd: str | None = None
    sudo: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = {
            "alias": self.alias,
            "host": self.host,
            "command": self.command,
            "cwd": self.cwd,
            "sudo": self.sudo,
            "exit_code": self.exit_code,
            "ok": self.exit_code == 0 and not self.timed_out,
            "timed_out": self.timed_out,
            "truncated": self.truncated,
            "duration_ms": self.duration_ms,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }
        return d


class SSHExecError(RuntimeError):
    """连接 / 通道层面的错误（不是远端命令非零退出）。"""


class ShellError(RuntimeError):
    """交互式 shell 会话层面的错误。"""


_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"      # CSI
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC
    r"|\x1b[()][0-9A-Za-z]"           # 字符集切换 ESC ( B
    r"|\x1b[@-Z\\-_]"                 # 其它单字符转义
)
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def strip_ansi(text: str) -> str:
    """去掉 ANSI 转义，只留可读文本（给不支持颜色的调用方 / 桌面端界面用）。"""
    cleaned = _ANSI_RE.sub("", text)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    return _CTRL_RE.sub("", cleaned)


@dataclass
class ShellSession:
    """一条 PTY 交互会话。所有 channel 操作都在它的锁里做。"""

    sid: str
    alias: str
    host: Host
    channel: paramiko.Channel
    cols: int
    rows: int
    term: str
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    bytes_out: int = 0
    closed: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)

    def info(self) -> dict[str, Any]:
        return {
            "session_id": self.sid,
            "alias": self.alias,
            "host": f"{self.host.host}:{self.host.port}",
            "term": self.term,
            "size": f"{self.cols}x{self.rows}",
            "bytes_out": self.bytes_out,
            "idle_seconds": int(time.time() - self.last_used),
            "uptime_seconds": int(time.time() - self.created_at),
            "closed": self.closed,
        }


@dataclass
class _Chan:
    """一条已建立的连接及其运行统计。"""

    client: paramiko.SSHClient
    host: Host
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    exec_count: int = 0
    jump_alias: str | None = None


def _host_key_policy(host: Host) -> paramiko.MissingHostKeyPolicy:
    if host.host_key_policy == "auto_add":
        return paramiko.AutoAddPolicy()
    if host.host_key_policy == "ignore":
        return paramiko.WarningPolicy()
    return paramiko.RejectPolicy()


class SSHPool:
    """按 alias 维护长连接的 SSH 中间层。"""

    def __init__(self, cfg: ServerConfig) -> None:
        self.cfg = cfg
        self._conns: dict[str, _Chan] = {}
        self._locks: dict[str, anyio.Lock] = {}
        self._sems: dict[str, anyio.Semaphore] = {}
        self._sftp_locks: dict[str, threading.Lock] = {}
        self._shells: dict[str, ShellSession] = {}
        self._guard = threading.Lock()

    # ---------------------------------------------------------------- helpers
    def _lock(self, alias: str) -> anyio.Lock:
        with self._guard:
            lock = self._locks.get(alias)
            if lock is None:
                lock = anyio.Lock()
                self._locks[alias] = lock
            return lock

    def _sem(self, alias: str, limit: int) -> anyio.Semaphore:
        with self._guard:
            sem = self._sems.get(alias)
            if sem is None:
                sem = anyio.Semaphore(max(1, limit))
                self._sems[alias] = sem
            return sem

    def _sftp_lock(self, alias: str) -> threading.Lock:
        with self._guard:
            lock = self._sftp_locks.get(alias)
            if lock is None:
                lock = threading.Lock()
                self._sftp_locks[alias] = lock
            return lock

    # ------------------------------------------------------------ connection
    def _connect_sync(self, host: Host, depth: int = 0) -> tuple[paramiko.SSHClient, str | None]:
        if depth > 4:
            raise SSHExecError("proxy_jump 嵌套层级过深（>4），疑似配置成环")

        client = paramiko.SSHClient()
        if host.known_hosts:
            kh = Path(host.known_hosts)
            if kh.is_file():
                client.load_host_keys(str(kh))
                if host.host_key_policy == "auto_add":
                    # AutoAddPolicy 只在持有私有 known_hosts 时写回，避免污染系统文件
                    client._host_keys_filename = str(kh)  # type: ignore[attr-defined]
            else:
                client.load_system_host_keys()
        else:
            client.load_system_host_keys()
        client.set_missing_host_key_policy(_host_key_policy(host))

        jump_alias: str | None = None
        sock = None
        jump_client = None
        if host.proxy_jump:
            jump_alias = host.proxy_jump
            if jump_alias == host.alias:
                raise SSHExecError("proxy_jump 不能指向自己")
            jump_host = self.cfg.hosts.get(jump_alias)
            if jump_host is None:
                raise SSHExecError(f"proxy_jump 指向未配置的别名 {jump_alias!r}")
            jump_client, _ = self._connect_sync(jump_host, depth + 1)
            transport = jump_client.get_transport()
            if transport is None or not transport.is_active():
                raise SSHExecError(f"跳板机 {jump_alias} 连接不可用")
            sock = transport.open_channel(
                "direct-tcpip", (host.host, host.port), ("127.0.0.1", 0), timeout=host.connect_timeout
            )

        kwargs: dict[str, Any] = {
            "hostname": host.host,
            "port": host.port,
            "username": host.user,
            "timeout": host.connect_timeout,
            "banner_timeout": max(host.connect_timeout, 30.0),
            "auth_timeout": max(host.connect_timeout, 30.0),
            "allow_agent": host.auth.allow_agent,
            "look_for_keys": host.auth.look_for_keys,
        }
        if sock is not None:
            kwargs["sock"] = sock
        if host.auth.method == "password":
            kwargs["password"] = host.auth.resolve_password()
        elif host.auth.method == "key":
            if not host.auth.key_file:
                raise SSHExecError(f"hosts.{host.alias}.auth 使用 key 方式但未配置 key_file")
            kwargs["key_filename"] = host.auth.key_file
            pp = host.auth.resolve_passphrase()
            if pp:
                kwargs["passphrase"] = pp

        try:
            client.connect(**kwargs)
        except Exception as exc:  # noqa: BLE001 - 统一包装成可读错误
            try:
                client.close()
            finally:
                pass
            raise SSHExecError(f"连接 {host.alias} ({host.host}:{host.port}) 失败: {exc}") from exc

        transport = client.get_transport()
        if transport is not None and host.keepalive > 0:
            transport.set_keepalive(host.keepalive)
        if jump_client is not None:
            # 跳板 client 只用于承载 channel，随主连接一起存活
            client._ssh_mcp_jump = jump_client  # type: ignore[attr-defined]
        return client, jump_alias

    async def get_conn(self, alias: str, *, reconnect: bool = False) -> _Chan:
        host = self.cfg.get(alias)
        async with self._lock(alias):
            conn = self._conns.get(alias)
            alive = conn is not None and conn.client.get_transport() is not None and conn.client.get_transport().is_active()
            if conn is not None and alive and not reconnect:
                conn.last_used = time.time()
                return conn
            if conn is not None:
                await anyio.to_thread.run_sync(self._close_sync, conn.client)
                self._conns.pop(alias, None)
            client, jump_alias = await anyio.to_thread.run_sync(self._connect_sync, host)
            conn = _Chan(client=client, host=host, jump_alias=jump_alias)
            self._conns[alias] = conn
            return conn

    @staticmethod
    def _close_sync(client: paramiko.SSHClient) -> None:
        for attr in ("_ssh_mcp_jump",):
            jump = getattr(client, attr, None)
            if jump is not None:
                try:
                    jump.close()
                except Exception:  # noqa: BLE001
                    pass
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass

    async def close(self, alias: str | None = None) -> list[str]:
        closed: list[str] = []
        aliases = [alias] if alias else list(self._conns)
        # 连接关了，挂在它上面的交互会话也一起收掉
        with self._guard:
            doomed = [sid for sid, s in self._shells.items() if alias is None or s.alias == alias]
            sessions = [self._shells.pop(sid) for sid in doomed]
        for sess in sessions:
            self._close_shell_sync(sess)
        for a in aliases:
            conn = self._conns.pop(a, None)
            if conn is None:
                continue
            await anyio.to_thread.run_sync(self._close_sync, conn.client)
            closed.append(a)
        return closed

    def close_sync_all(self) -> list[str]:
        """同步关闭所有连接（给配置热加载的后台清理线程用）。"""
        self.close_all_shells()
        closed: list[str] = []
        for alias, conn in list(self._conns.items()):
            self._conns.pop(alias, None)
            self._close_sync(conn.client)
            closed.append(alias)
        return closed

    def status(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for alias, conn in sorted(self._conns.items()):
            transport = conn.client.get_transport()
            rows.append(
                {
                    "alias": alias,
                    "host": f"{conn.host.host}:{conn.host.port}",
                    "user": conn.host.user,
                    "connected": bool(transport is not None and transport.is_active()),
                    "exec_count": conn.exec_count,
                    "idle_seconds": int(time.time() - conn.last_used),
                    "uptime_seconds": int(time.time() - conn.created_at),
                    "via_jump": conn.jump_alias,
                }
            )
        return rows

    # ------------------------------------------------------------ exec engine
    def _open_chan(self, conn: _Chan, command: str, timeout: float):
        transport = conn.client.get_transport()
        if transport is None or not transport.is_active():
            raise SSHExecError(f"{conn.host.alias} 的连接已断开")
        chan = transport.open_session(timeout=min(timeout, 30.0))
        chan.settimeout(None)
        chan.exec_command(command)
        return chan

    @staticmethod
    def _drain(chan: paramiko.Channel, deadline: float, max_bytes: int) -> tuple[bytes, bytes, bool, bool]:
        out = bytearray()
        err = bytearray()
        truncated = False
        timed_out = False
        while True:
            got_data = False
            while chan.recv_ready():
                chunk = chan.recv(65536)
                if not chunk:
                    break
                got_data = True
                room = max_bytes - len(out)
                if room > 0:
                    out += chunk[:room]
                    if len(chunk) > room:
                        truncated = True
                else:
                    truncated = True
            while chan.recv_stderr_ready():
                chunk = chan.recv_stderr(65536)
                if not chunk:
                    break
                got_data = True
                room = max_bytes - len(err)
                if room > 0:
                    err += chunk[:room]
                    if len(chunk) > room:
                        truncated = True
                else:
                    truncated = True
            if chan.exit_status_ready() and not chan.recv_ready() and not chan.recv_stderr_ready():
                break
            if time.monotonic() > deadline:
                timed_out = True
                try:
                    chan.close()
                except Exception:  # noqa: BLE001
                    pass
                break
            if not got_data:
                # paramiko 的 Channel 在 Windows 上不能进 select()，用短轮询最稳
                time.sleep(0.01)
        return bytes(out), bytes(err), truncated, timed_out

    def _exec_sync(
        self,
        conn: _Chan,
        command: str,
        *,
        timeout: float,
        max_bytes: int,
        sudo_password: str | None,
        encoding: str,
    ) -> tuple[int, bytes, bytes, bool, bool]:
        chan = self._open_chan(conn, command, timeout)
        try:
            if sudo_password is not None:
                chan.sendall(sudo_password.encode(encoding) + b"\n")
            deadline = time.monotonic() + timeout
            out, err, truncated, timed_out = self._drain(chan, deadline, max_bytes)
            exit_code = -1
            if not timed_out:
                try:
                    exit_code = chan.recv_exit_status()
                except Exception:  # noqa: BLE001
                    exit_code = -1
            return exit_code, out, err, truncated, timed_out
        finally:
            try:
                chan.close()
            except Exception:  # noqa: BLE001
                pass

    async def exec(
        self,
        alias: str,
        command: str,
        *,
        cwd: str | None = None,
        timeout: float | None = None,
        max_output_bytes: int | None = None,
        sudo: bool = False,
        encoding: str | None = None,
        _attempt: int = 0,
    ) -> ExecResult:
        host = self.cfg.get(alias)
        policy = self.cfg.policy
        policy.check_command(command)

        eff_timeout = min(
            timeout if timeout is not None else host.command_timeout,
            policy.max_command_timeout,
        )
        if eff_timeout <= 0:
            raise ValueError("timeout 必须大于 0")
        eff_max = min(max_output_bytes or host.max_output_bytes, policy.max_output_bytes)

        sudo_password: str | None = None
        if sudo:
            if not policy.allow_sudo:
                raise PermissionError("policy.allow_sudo=false，禁止提权执行")
            if host.sudo is None:
                raise PermissionError(f"{alias} 未配置 sudo 段，无法提权执行")
            sudo_password = host.sudo.resolve_password()

        eff_encoding = encoding or host.encoding
        remote_cmd = build_remote_command(
            command,
            shell_kind=host.shell_kind,
            posix_shell=host.posix_shell,
            cwd=cwd or host.default_cwd,
            sudo=sudo,
            sudo_user=host.sudo.user if (sudo and host.sudo) else None,
        )

        sem = self._sem(alias, host.max_concurrency)
        async with sem:
            conn = await self.get_conn(alias)
            started = time.perf_counter()
            try:
                exit_code, out, err, truncated, timed_out = await anyio.to_thread.run_sync(
                    lambda: self._exec_sync(
                        conn,
                        remote_cmd,
                        timeout=eff_timeout,
                        max_bytes=eff_max,
                        sudo_password=sudo_password,
                        encoding=eff_encoding,
                    )
                )
            except SSHExecError:
                raise
            except Exception as exc:  # noqa: BLE001 - 连接被对端踢掉时重连一次
                if _attempt >= 1:
                    raise SSHExecError(f"{alias} 执行失败: {exc}") from exc
                await self.get_conn(alias, reconnect=True)
                return await self.exec(
                    alias,
                    command,
                    cwd=cwd,
                    timeout=timeout,
                    max_output_bytes=max_output_bytes,
                    sudo=sudo,
                    encoding=encoding,
                    _attempt=_attempt + 1,
                )

        conn.exec_count += 1
        conn.last_used = time.time()
        duration_ms = int((time.perf_counter() - started) * 1000)
        return ExecResult(
            alias=alias,
            host=f"{host.host}:{host.port}",
            command=command,
            cwd=cwd or host.default_cwd,
            sudo=sudo,
            exit_code=exit_code,
            stdout=out.decode(eff_encoding, errors="replace"),
            stderr=err.decode(eff_encoding, errors="replace"),
            duration_ms=duration_ms,
            truncated=truncated,
            timed_out=timed_out,
        )

    async def exec_many(
        self,
        aliases: list[str],
        command: str,
        *,
        on_start: Callable[[str], None] | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []

        async def one(a: str) -> None:
            if on_start:
                on_start(a)
            try:
                res = await self.exec(a, command, **kwargs)
                results.append(res.to_dict())
            except Exception as exc:  # noqa: BLE001 - 单机失败不影响其他主机
                results.append(
                    {
                        "alias": a,
                        "ok": False,
                        "exit_code": None,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )

        async with anyio.create_task_group() as tg:
            for a in aliases:
                tg.start_soon(one, a)
        results.sort(key=lambda r: aliases.index(r["alias"]))
        return results

    # ------------------------------------------------------------------ sftp
    def _sftp_op(self, conn: _Chan, fn: Callable[[paramiko.SFTPClient], Any]) -> Any:
        sftp = conn.client.open_sftp()
        try:
            return fn(sftp)
        finally:
            try:
                sftp.close()
            except Exception:  # noqa: BLE001
                pass

    async def upload(self, alias: str, local_path: str, remote_path: str) -> dict[str, Any]:
        host = self.cfg.get(alias)
        if not self.cfg.policy.allow_upload:
            raise PermissionError("policy.allow_upload=false，禁止上传")
        lp = Path(local_path).expanduser()
        if not lp.is_file():
            raise FileNotFoundError(f"本地文件不存在: {lp}")
        digest = hashlib.sha256(lp.read_bytes()).hexdigest()
        size = lp.stat().st_size

        def op(sftp: paramiko.SFTPClient) -> dict[str, Any]:
            _ensure_remote_dir(sftp, posixpath.dirname(remote_path))
            sftp.put(str(lp), remote_path)
            st = sftp.stat(remote_path)
            return {"remote_size": st.st_size}

        conn = await self.get_conn(alias)
        with self._sftp_lock(alias):
            info = await anyio.to_thread.run_sync(lambda: self._sftp_op(conn, op))
        return {
            "alias": alias,
            "local_path": str(lp),
            "remote_path": remote_path,
            "local_size": size,
            "remote_size": info["remote_size"],
            "sha256": digest,
            "ok": info["remote_size"] == size,
        }

    async def download(self, alias: str, remote_path: str, local_path: str) -> dict[str, Any]:
        self.cfg.get(alias)
        if not self.cfg.policy.allow_download:
            raise PermissionError("policy.allow_download=false，禁止下载")
        dest = Path(local_path).expanduser()
        dest.parent.mkdir(parents=True, exist_ok=True)

        def op(sftp: paramiko.SFTPClient) -> int:
            sftp.get(remote_path, str(dest))
            return sftp.stat(remote_path).st_size

        conn = await self.get_conn(alias)
        with self._sftp_lock(alias):
            remote_size = await anyio.to_thread.run_sync(lambda: self._sftp_op(conn, op))
        data = dest.read_bytes()
        return {
            "alias": alias,
            "remote_path": remote_path,
            "local_path": str(dest.resolve()),
            "remote_size": remote_size,
            "local_size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "ok": len(data) == remote_size,
        }

    async def write_remote_file(self, alias: str, remote_path: str, content: str, mode: str | None = None) -> dict[str, Any]:
        self.cfg.get(alias)
        if not self.cfg.policy.allow_write_file:
            raise PermissionError("policy.allow_write_file=false，禁止写远端文件")
        payload = content.encode("utf-8")

        def op(sftp: paramiko.SFTPClient) -> int:
            _ensure_remote_dir(sftp, posixpath.dirname(remote_path))
            with sftp.open(remote_path, "wb") as fh:
                fh.write(payload)
            if mode:
                sftp.chmod(remote_path, int(mode, 8))
            return sftp.stat(remote_path).st_size

        conn = await self.get_conn(alias)
        with self._sftp_lock(alias):
            size = await anyio.to_thread.run_sync(lambda: self._sftp_op(conn, op))
        return {
            "alias": alias,
            "remote_path": remote_path,
            "bytes_written": size,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "mode": mode,
        }

    async def read_remote_file(self, alias: str, remote_path: str, max_bytes: int = 262144) -> dict[str, Any]:
        self.cfg.get(alias)

        def op(sftp: paramiko.SFTPClient) -> tuple[bytes, int]:
            st = sftp.stat(remote_path)
            with sftp.open(remote_path, "rb") as fh:
                return fh.read(max_bytes), st.st_size

        conn = await self.get_conn(alias)
        with self._sftp_lock(alias):
            data, size = await anyio.to_thread.run_sync(lambda: self._sftp_op(conn, op))
        return {
            "alias": alias,
            "remote_path": remote_path,
            "size": size,
            "truncated": size > len(data),
            "content": data.decode(self.cfg.get(alias).encoding, errors="replace"),
        }

    # ------------------------------------------------------------ shell / PTY
    # 交互式会话：get_pty() + invoke_shell()，远端会把命令当成终端程序来跑，
    # 所以 top / vi / less / 带颜色的日志都能正常工作。

    def _reap_shells(self) -> None:
        """关掉超时没动静的会话，防止忘了 close 一直挂着。"""
        ttl = self.cfg.policy.shell_idle_timeout
        now = time.time()
        with self._guard:
            dead = [sid for sid, s in self._shells.items() if now - s.last_used > ttl]
            for sid in dead:
                sess = self._shells.pop(sid, None)
                if sess is not None:
                    self._close_shell_sync(sess)

    @staticmethod
    def _close_shell_sync(sess: ShellSession) -> None:
        with sess.lock:
            sess.closed = True
            try:
                sess.channel.close()
            except Exception:  # noqa: BLE001
                pass

    def _drain_shell(self, sess: ShellSession, *, idle: float, timeout: float, max_bytes: int) -> tuple[bytes, bool]:
        """把 channel 上当前能读的数据全读走。

        读到「连续 idle 秒没新数据」或「总时长超过 timeout」为止——这样一次
        send 之后能拿到程序的完整一屏输出，而不是半截。
        """
        buf = bytearray()
        deadline = time.monotonic() + timeout
        last_data = time.monotonic()
        while True:
            got = False
            while sess.channel.recv_ready():
                chunk = sess.channel.recv(65536)
                if not chunk:
                    break
                got = True
                room = max_bytes - len(buf)
                if room > 0:
                    buf += chunk[:room]
            while sess.channel.recv_stderr_ready():
                chunk = sess.channel.recv_stderr(65536)
                if not chunk:
                    break
                got = True
                room = max_bytes - len(buf)
                if room > 0:
                    buf += chunk[:room]
            now = time.monotonic()
            if got:
                last_data = now
                continue
            if sess.channel.closed or sess.channel.exit_status_ready():
                sess.closed = True
                break
            if now - last_data >= idle or now >= deadline:
                break
            time.sleep(0.02)
        return bytes(buf), sess.closed

    def _shell_result(self, sess: ShellSession, raw: bytes) -> dict[str, Any]:
        text = raw.decode(sess.host.encoding, errors="replace")
        sess.bytes_out += len(raw)
        sess.last_used = time.time()
        return {
            "session_id": sess.sid,
            "alias": sess.alias,
            "output": text,
            "plain": strip_ansi(text),
            "bytes": len(raw),
            "closed": sess.closed,
        }

    def _open_shell_sync(
        self, conn: _Chan, alias: str, cols: int, rows: int, term: str, command: str | None
    ) -> ShellSession:
        transport = conn.client.get_transport()
        if transport is None or not transport.is_active():
            raise ShellError(f"{alias} 的连接已断开")
        chan = transport.open_session(timeout=15)
        chan.get_pty(term=term, width=cols, height=rows)
        chan.invoke_shell()
        chan.settimeout(None)

        sess = ShellSession(
            sid=uuid.uuid4().hex[:12],
            alias=alias,
            host=conn.host,
            channel=chan,
            cols=cols,
            rows=rows,
            term=term,
        )
        if command:
            try:
                chan.send(command.encode(sess.host.encoding) + b"\n")
            except Exception:  # noqa: BLE001
                pass
        return sess

    async def open_shell(
        self,
        alias: str,
        *,
        cols: int = 120,
        rows: int = 32,
        term: str = "xterm-256color",
        command: str | None = None,
        wait: float = 0.6,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        host = self.cfg.get(alias)
        policy = self.cfg.policy
        if not policy.allow_shell:
            raise PermissionError("policy.allow_shell=false，禁止交互式 shell")
        if command:
            policy.check_command(command)
        if not (10 <= cols <= 500 and 5 <= rows <= 200):
            raise ValueError("cols 需在 10-500、rows 需在 5-200 之间")

        self._reap_shells()
        with self._guard:
            active = sum(1 for s in self._shells.values() if not s.closed)
            if active >= policy.max_shell_sessions:
                raise ShellError(
                    f"已达交互会话上限 {policy.max_shell_sessions}，先 ssh_shell_close 掉不用的"
                )

        conn = await self.get_conn(alias)
        sess = await anyio.to_thread.run_sync(
            lambda: self._open_shell_sync(conn, alias, cols, rows, term, command)
        )
        with self._guard:
            self._shells[sess.sid] = sess

        raw, _closed = await anyio.to_thread.run_sync(
            lambda: self._drain_shell(sess, idle=wait, timeout=timeout, max_bytes=host.max_output_bytes)
        )
        result = self._shell_result(sess, raw)
        result["info"] = sess.info()
        result["hint"] = "用 ssh_shell_send 发送按键（回车用 \\r），ssh_shell_read 取后续输出"
        return result

    def _get_shell(self, sid: str) -> ShellSession:
        with self._guard:
            sess = self._shells.get(sid)
        if sess is None:
            raise ShellError(f"没有会话 {sid!r}；用 ssh_shell_open 新建，或用 ssh_status 看现有会话")
        return sess

    async def shell_send(
        self,
        sid: str,
        data: str,
        *,
        wait: float = 0.35,
        timeout: float = 8.0,
    ) -> dict[str, Any]:
        sess = self._get_shell(sid)
        host = sess.host

        def job() -> tuple[bytes, bool]:
            with sess.lock:
                if sess.closed:
                    return b"", True
                try:
                    sess.channel.send(data.encode(host.encoding))
                except Exception as exc:  # noqa: BLE001
                    sess.closed = True
                    raise ShellError(f"发送失败（会话可能已结束）: {exc}") from exc
                return self._drain_shell(
                    sess, idle=wait, timeout=timeout, max_bytes=host.max_output_bytes
                )

        raw, _closed = await anyio.to_thread.run_sync(job)
        return self._shell_result(sess, raw)

    async def shell_read(self, sid: str, *, timeout: float = 1.0) -> dict[str, Any]:
        sess = self._get_shell(sid)

        def job() -> tuple[bytes, bool]:
            with sess.lock:
                if sess.closed:
                    return b"", True
                return self._drain_shell(
                    sess, idle=min(0.2, timeout), timeout=timeout, max_bytes=sess.host.max_output_bytes
                )

        raw, _closed = await anyio.to_thread.run_sync(job)
        return self._shell_result(sess, raw)

    async def shell_resize(self, sid: str, cols: int, rows: int) -> dict[str, Any]:
        sess = self._get_shell(sid)
        if not (10 <= cols <= 500 and 5 <= rows <= 200):
            raise ValueError("cols 需在 10-500、rows 需在 5-200 之间")

        def job() -> None:
            with sess.lock:
                if not sess.closed:
                    sess.channel.resize_pty(width=cols, height=rows)
                    sess.cols, sess.rows = cols, rows
                    sess.last_used = time.time()

        await anyio.to_thread.run_sync(job)
        return {"session_id": sid, "size": f"{sess.cols}x{sess.rows}", "closed": sess.closed}

    async def shell_close(self, sid: str) -> dict[str, Any]:
        with self._guard:
            sess = self._shells.pop(sid, None)
        if sess is None:
            return {"session_id": sid, "closed": True, "existed": False}
        await anyio.to_thread.run_sync(lambda: self._close_shell_sync(sess))
        return {"session_id": sid, "closed": True, "existed": True, "alias": sess.alias}

    def shell_list(self) -> list[dict[str, Any]]:
        self._reap_shells()
        with self._guard:
            sessions = list(self._shells.values())
        return [s.info() for s in sorted(sessions, key=lambda s: s.created_at)]

    def close_all_shells(self) -> list[str]:
        with self._guard:
            sessions = list(self._shells.items())
            self._shells.clear()
        for _sid, sess in sessions:
            self._close_shell_sync(sess)
        return [sid for sid, _ in sessions]


def _ensure_remote_dir(sftp: paramiko.SFTPClient, remote_dir: str) -> None:
    if not remote_dir or remote_dir in (".", "/", ""):
        return
    parts = []
    cur = remote_dir
    while cur and cur not in ("/", "."):
        try:
            sftp.stat(cur)
            break
        except IOError:
            parts.append(cur)
            cur = posixpath.dirname(cur)
    for d in reversed(parts):
        try:
            sftp.mkdir(d)
        except IOError:
            pass


def build_remote_command(
    command: str,
    *,
    shell_kind: str = "posix",
    posix_shell: str = "bash",
    cwd: str | None = None,
    sudo: bool = False,
    sudo_user: str | None = None,
) -> str:
    """把用户命令包装成最终交给 exec 请求的字符串。

    posix:  [sudo -S -p '' [-u user]] bash -lc '<cd ... && cmd>'
    powershell: powershell -NoProfile -NonInteractive -Command "<Set-Location ...; cmd>"
    cmd:    cmd /c "<cmd>"
    """
    if shell_kind == "powershell":
        body = command
        if cwd:
            safe_cwd = cwd.replace("'", "''")
            body = "Set-Location -LiteralPath '" + safe_cwd + "'; " + command
        escaped = body.replace('"', '\\"')
        return 'powershell -NoProfile -NonInteractive -Command "' + escaped + '"'

    if shell_kind == "cmd":
        body = command
        if cwd:
            body = 'cd /d "' + cwd + '" && ' + command
        return 'cmd /c "' + body + '"'

    inner = command
    if cwd:
        inner = "cd " + shlex.quote(cwd) + " && " + command
    quoted = shlex.quote(inner)
    if sudo:
        parts = ["sudo", "-S", "-p", "''"]
        if sudo_user:
            parts += ["-u", sudo_user]
        parts += [posix_shell, "-lc", quoted]
        return " ".join(parts)
    return posix_shell + " -lc " + quoted

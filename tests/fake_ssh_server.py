"""本地回环用的假 SSH 服务器：只用于端到端测试 ssh-mcp 本身，不外联。

支持：密码认证、exec 通道（命令真的在本机 cmd/pwsh 里执行）、SFTP 子系统。
"""

from __future__ import annotations

import os
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import paramiko
from paramiko import SFTPAttributes, SFTPHandle, SFTPServer, SFTPServerInterface


class LocalSFTPServer(SFTPServerInterface):
    """把 SFTP 请求映射到某个本地临时目录。"""

    def __init__(self, server, *largs, **kwargs):
        super().__init__(server, *largs, **kwargs)
        root = getattr(server, "sftp_root", None) or tempfile.mkdtemp(prefix="ssh-mcp-sftp-")
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _real(self, path: str) -> Path:
        rel = path.replace("\\", "/").lstrip("/")
        target = (self.root / rel).resolve()
        if self.root.resolve() not in target.parents and target != self.root.resolve():
            raise OSError(f"path escapes root: {path}")
        return target

    def list_folder(self, path):
        real = self._real(path)
        out = []
        for name in os.listdir(real):
            attr = SFTPAttributes.from_stat(os.stat(real / name))
            attr.filename = name
            out.append(attr)
        return out

    def stat(self, path):
        return SFTPAttributes.from_stat(os.stat(self._real(path)))

    def lstat(self, path):
        return SFTPAttributes.from_stat(os.lstat(self._real(path)))

    def open(self, path, flags, attr):
        real = self._real(path)
        real.parent.mkdir(parents=True, exist_ok=True)
        mode = "r+b"
        if flags & os.O_WRONLY:
            mode = "ab" if flags & os.O_APPEND else "wb"
        elif flags & os.O_RDWR:
            mode = "r+b"
        else:
            mode = "rb"
        if (flags & os.O_CREAT) and not real.exists():
            real.touch()
        handle = SFTPHandle(flags)
        fobj = open(real, mode)
        handle.readfile = fobj
        handle.writefile = fobj
        return handle

    def remove(self, path):
        real = self._real(path)
        if real.is_dir():
            os.rmdir(real)
        else:
            os.remove(real)
        return paramiko.SFTP_OK

    def rename(self, oldpath, newpath):
        os.replace(self._real(oldpath), self._real(newpath))
        return paramiko.SFTP_OK

    def mkdir(self, path, attr):
        self._real(path).mkdir(parents=True, exist_ok=True)
        return paramiko.SFTP_OK

    def rmdir(self, path):
        os.rmdir(self._real(path))
        return paramiko.SFTP_OK

    def chattr(self, path, attr):
        real = self._real(path)
        if attr._flags & SFTPAttributes.FLAG_SIZE and attr.st_size is not None:
            os.truncate(real, attr.st_size)
        if attr._flags & SFTPAttributes.FLAG_PERMISSIONS and attr.st_mode is not None:
            os.chmod(real, attr.st_mode)
        return paramiko.SFTP_OK


class _ServerIface(paramiko.ServerInterface):
    def __init__(self, username: str, password: str, root: Path, authorized_key=None) -> None:
        self.username = username
        self.password = password
        self.authorized_key = authorized_key
        self.sftp_root = str(root)
        self.command: str | None = None
        self.pty_size: tuple[int, int] = (0, 0)
        self.shell_count = 0

    def check_auth_password(self, username, password):
        if username == self.username and password == self.password:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_auth_publickey(self, username, key):
        if self.authorized_key is not None and username == self.username and key == self.authorized_key:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "publickey,password" if self.authorized_key is not None else "password"

    def check_channel_request(self, kind, chanid):
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    # ---- 交互式 shell：假实现成「回显 + 少量内建命令」，够测协议链路 ----
    def check_channel_pty_request(self, channel, term, width, height, pixelwidth, pixelheight, modes):
        self.pty_size = (width, height)
        channel._ssh_mcp_size = (width, height)  # noqa: SLF001
        return True

    def check_channel_window_change_request(self, channel, width, height, pixelwidth, pixelheight):
        self.pty_size = (width, height)
        channel._ssh_mcp_size = (width, height)  # noqa: SLF001
        return True

    def check_channel_shell_request(self, channel):
        self.shell_count += 1
        threading.Thread(target=self._shell_loop, args=(channel,), daemon=True).start()
        return True

    def _shell_loop(self, channel) -> None:
        width, height = getattr(channel, "_ssh_mcp_size", (0, 0))  # noqa: SLF001
        try:
            channel.sendall(f"fake-pty {width}x{height}\r\n$ ".encode())
            buf = b""
            while channel.active:
                if not channel.recv_ready():
                    time.sleep(0.02)
                    continue
                data = channel.recv(4096)
                if not data:
                    break
                channel.sendall(data)  # 终端会回显输入
                buf += data
                while b"\r" in buf or b"\n" in buf:
                    sep = b"\r" if b"\r" in buf else b"\n"
                    line, buf = buf.split(sep, 1)
                    cmd = line.decode("utf-8", "replace").strip()
                    if cmd in ("exit", "logout"):
                        channel.sendall(b"bye\r\n")
                        channel.close()
                        return
                    if cmd == "stty size":
                        w, h = getattr(channel, "_ssh_mcp_size", (0, 0))  # noqa: SLF001
                        channel.sendall(f"{h} {w}\r\n".encode())
                    channel.sendall(b"$ ")
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                channel.close()
            except Exception:  # noqa: BLE001
                pass

    def check_channel_exec_request(self, channel, command):
        cmd = command.decode("utf-8", errors="replace")
        self.command = cmd
        threading.Thread(target=self._run, args=(channel, cmd), daemon=True).start()
        return True

    def _run(self, channel, cmd: str) -> None:
        try:
            proc = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                cwd=self.sftp_root,
                timeout=120,
            )
            if proc.stdout:
                channel.sendall(proc.stdout)
            if proc.stderr:
                channel.sendall_stderr(proc.stderr)
            channel.send_exit_status(proc.returncode)
        except Exception as exc:  # noqa: BLE001
            try:
                channel.sendall_stderr(str(exc).encode())
                channel.send_exit_status(1)
            except Exception:  # noqa: BLE001
                pass
        finally:
            try:
                channel.close()
            except Exception:  # noqa: BLE001
                pass


class FakeSSHServer:
    def __init__(self, username: str = "testuser", password: str = "testpass", authorized_key=None) -> None:
        self.authorized_key = authorized_key
        self.username = username
        self.password = password
        self.root = Path(tempfile.mkdtemp(prefix="ssh-mcp-root-"))
        self.host_key = paramiko.RSAKey.generate(2048)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(20)
        self.port = self.sock.getsockname()[1]
        self._closing = False
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self.transports: list[paramiko.Transport] = []

    def start(self) -> "FakeSSHServer":
        self._thread.start()
        return self

    def _accept_loop(self) -> None:
        while not self._closing:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        transport = paramiko.Transport(conn)
        transport.add_server_key(self.host_key)
        transport.set_subsystem_handler("sftp", SFTPServer, LocalSFTPServer)
        self.transports.append(transport)
        try:
            transport.start_server(server=_ServerIface(self.username, self.password, self.root, self.authorized_key))
        except Exception:  # noqa: BLE001
            return
        while transport.is_active() and not self._closing:
            time.sleep(0.1)

    def stop(self) -> None:
        self._closing = True
        try:
            self.sock.close()
        except OSError:
            pass
        for t in self.transports:
            try:
                t.close()
            except Exception:  # noqa: BLE001
                pass

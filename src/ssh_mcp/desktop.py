"""ssh-mcp 桌面端（tkinter，无第三方 GUI 依赖）。

走原生窗口：
- 左侧主机列表，右侧详情 + 命令终端；
- 新增 / 编辑 / 删除主机，密码走凭据文件，保存前校验、覆盖前备份；
- 一条命令直达目标机（凭据全部由进程持有，界面只显示来源标记）；
- 可在同一进程里一键起停 MCP HTTP 端点，供各个客户端会话连接。

跑法：
    python -m ssh_mcp.desktop
    python -m ssh_mcp.desktop --config E:\\path\\servers.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable

import tkinter as tk
from tkinter import messagebox, ttk

from . import server as srv
from . import __author__, __author_email__, __github__, __version__
from .config import ConfigError, Host
from .config_store import cred_dir, delete_host, ensure_config, upsert_host

APP_TITLE = "ssh-mcp 桌面端"
FONT = ("Microsoft YaHei UI", 9)
MONO = ("Consolas", 9)
LOG_PATH = Path.home() / ".ssh-mcp" / "logs" / "desktop.log"
SERVICE_LOG = Path.home() / ".ssh-mcp" / "logs" / "service.log"
SERVICE_STATE = Path.home() / ".ssh-mcp" / "service.json"
UI_STATE_PATH = Path.home() / ".ssh-mcp" / "desktop-ui.json"
STILL_ACTIVE = 259

AUTH_LABEL = {"key": "私钥", "password": "密码", "agent": "Agent"}


def log_line(text: str) -> None:
    """桌面端日志：pythonw 没有控制台，异常只能落到文件里。"""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {text.rstrip()}\n")
    except Exception:  # noqa: BLE001
        pass


def ensure_stdio() -> None:
    """pythonw.exe 下 sys.stdout/sys.stderr 是 None，很多库会因此炸掉。"""
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        stream = open(LOG_PATH, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
    except Exception:  # noqa: BLE001
        import io

        stream = io.StringIO()
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream
STORE_LABEL = [
    ("file", "凭据文件（推荐）"),
    ("env", "环境变量"),
    ("plain", "直接写入 yaml"),
]
STORE_VALUES = [label for _, label in STORE_LABEL]
STORE_KEYS = {label: key for key, label in STORE_LABEL}


# --------------------------------------------------------------------------- #
# 异步执行器：paramiko 是同步库，统一丢到常驻 event loop 线程里跑
# --------------------------------------------------------------------------- #
class AsyncRunner:
    def __init__(self, app: "App") -> None:
        self.app = app
        self.loop = asyncio.new_event_loop()
        self.results: queue.Queue[tuple[Any, Any, Any]] = queue.Queue()
        threading.Thread(target=self._serve, daemon=True).start()
        app.after(60, self._poll)

    def _serve(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(
        self,
        coro: Any,
        on_done: Callable[[Any], None] | None = None,
        on_error: Callable[[BaseException], None] | None = None,
    ) -> Any:
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        fut.add_done_callback(lambda f: self.results.put((f, on_done, on_error)))
        return fut

    def _poll(self) -> None:
        while True:
            try:
                fut, on_done, on_error = self.results.get_nowait()
            except queue.Empty:
                break
            try:
                result = fut.result()
            except BaseException as exc:  # noqa: BLE001
                handler = on_error or self.app.show_error
                handler(exc)
            else:
                if on_done:
                    on_done(result)
        self.app.after(60, self._poll)

    def shutdown(self) -> None:
        try:
            self.loop.call_soon_threadsafe(self.loop.stop)
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# 主机编辑对话框
# --------------------------------------------------------------------------- #
class HostDialog(tk.Toplevel):
    def __init__(self, app: "App", host: dict[str, Any] | None) -> None:
        super().__init__(app)
        self.app = app
        self.host = host
        self.result: dict[str, Any] | None = None
        self.title(("编辑主机 " + host["alias"]) if host else "新增主机")
        self.transient(app)
        self.resizable(False, False)
        self.configure(padx=14, pady=12)

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True)

        basic = ttk.Frame(nb, padding=12)
        auth = ttk.Frame(nb, padding=12)
        adv = ttk.Frame(nb, padding=12)
        nb.add(basic, text=" 基本信息 ")
        nb.add(auth, text=" 认证与 sudo ")
        nb.add(adv, text=" 高级 ")

        self.vars: dict[str, tk.Variable] = {}
        self._build_basic(basic)
        self._build_auth(auth)
        self._build_adv(adv)

        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(12, 0))
        ttk.Label(bar, text="保存前会先校验配置，原文件备份为 .bak", foreground="#6b7a8d").pack(side="left")
        ttk.Button(bar, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(bar, text="保存", style="Accent.TButton", command=self.save).pack(side="right", padx=(0, 8))

        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()
        self._center()
        self._apply(host)

    # -------------------------------------------------------------- geometry
    def _center(self) -> None:
        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        px = self.app.winfo_rootx() + (self.app.winfo_width() - w) // 2
        py = self.app.winfo_rooty() + (self.app.winfo_height() - h) // 2
        self.geometry(f"+{max(px, 0)}+{max(py, 0)}")

    # ------------------------------------------------------------------ form
    def _field(self, parent: ttk.Frame, row: int, label: str, key: str, tip: str = "", width: int = 34) -> ttk.Entry:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=(0, 4))
        var = tk.StringVar()
        self.vars[key] = var
        ent = ttk.Entry(parent, textvariable=var, width=width, font=FONT)
        ent.grid(row=row, column=1, sticky="we", pady=(0, 4))
        if tip:
            ttk.Label(parent, text=tip, foreground="#6b7a8d").grid(row=row, column=2, sticky="w", padx=(8, 0))
        return ent

    def _combo(self, parent: ttk.Frame, row: int, label: str, key: str, values: list[str], width: int = 32) -> ttk.Combobox:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=(0, 4))
        var = tk.StringVar()
        self.vars[key] = var
        cb = ttk.Combobox(parent, textvariable=var, values=values, state="readonly", width=width)
        cb.grid(row=row, column=1, sticky="we", pady=(0, 4))
        return cb

    def _build_basic(self, f: ttk.Frame) -> None:
        f.columnconfigure(1, weight=1)
        self.ent_alias = self._field(f, 0, "别名 alias *", "alias", "会话端只用这个名字", 30)
        self._field(f, 1, "说明", "description", "", 30)
        self._field(f, 2, "主机 host *", "host", "IP 或域名", 30)
        self._field(f, 3, "端口", "port", "默认 22", 30)
        self._field(f, 4, "用户", "user", "", 30)
        self._field(f, 5, "标签", "tags", "逗号分隔", 30)
        self._field(f, 6, "默认工作目录", "default_cwd", "可留空", 30)
        self._combo(f, 7, "远端 shell", "shell_kind", ["posix", "powershell", "cmd"], 30)
        self._field(f, 8, "posix shell", "posix_shell", "默认 bash；Alpine 改 sh", 30)

    def _build_auth(self, f: ttk.Frame) -> None:
        f.columnconfigure(1, weight=1)

        ttk.Label(f, text="认证方式").grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.var_method = tk.StringVar(value="key")
        self.vars["auth_method"] = self.var_method
        row = ttk.Frame(f)
        row.grid(row=0, column=1, sticky="w", pady=(0, 6))
        for key, text in (("key", "私钥"), ("password", "密码"), ("agent", "SSH Agent")):
            ttk.Radiobutton(row, text=text, value=key, variable=self.var_method, command=self._sync).pack(side="left", padx=(0, 14))

        self.frm_key = ttk.LabelFrame(f, text="私钥", padding=10)
        self.frm_key.grid(row=1, column=0, columnspan=2, sticky="we", pady=(0, 8))
        self.frm_key.columnconfigure(1, weight=1)
        self._field(self.frm_key, 0, "私钥路径", "key_file", "如 ~/.ssh/id_ed25519", 40)
        self._field(self.frm_key, 1, "私钥口令", "key_passphrase", "留空 = 不修改", 40)
        ttk.Label(self.frm_key, text="口令存储").grid(row=2, column=0, sticky="w", pady=(0, 4))
        self.vars["key_passphrase_mode_label"] = tk.StringVar(value=STORE_VALUES[0])
        ttk.Combobox(
            self.frm_key,
            textvariable=self.vars["key_passphrase_mode_label"],
            values=STORE_VALUES,
            state="readonly",
            width=38,
        ).grid(row=2, column=1, sticky="we", pady=(0, 4))
        self._field(self.frm_key, 3, "口令环境变量名", "key_passphrase_env", "选环境变量时填", 40)

        self.frm_pw = ttk.LabelFrame(f, text="密码", padding=10)
        self.frm_pw.grid(row=1, column=0, columnspan=2, sticky="we", pady=(0, 8))
        self.frm_pw.columnconfigure(1, weight=1)
        ttk.Label(self.frm_pw, text="密码").grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.var_password = tk.StringVar()
        self.vars["password"] = self.var_password
        self.ent_password = ttk.Entry(self.frm_pw, textvariable=self.var_password, show="•", width=42, font=FONT)
        self.ent_password.grid(row=0, column=1, sticky="we", pady=(0, 4))
        ttk.Label(self.frm_pw, text="存储方式").grid(row=1, column=0, sticky="w", pady=(0, 4))
        self.var_pw_mode = tk.StringVar(value=STORE_VALUES[0])
        self.vars["password_mode_label"] = self.var_pw_mode
        ttk.Combobox(self.frm_pw, textvariable=self.var_pw_mode, values=STORE_VALUES, state="readonly", width=40).grid(
            row=1, column=1, sticky="we", pady=(0, 4)
        )
        self._field(self.frm_pw, 2, "环境变量名", "password_env", "选环境变量时填", 42)
        self.lbl_pw_state = ttk.Label(self.frm_pw, text="", foreground="#6b7a8d")
        self.lbl_pw_state.grid(row=3, column=0, columnspan=2, sticky="w")

        self.frm_agent = ttk.Frame(f)
        self.frm_agent.grid(row=1, column=0, columnspan=2, sticky="we", pady=(0, 8))
        ttk.Label(
            self.frm_agent,
            text="使用本机 ssh-agent 里已加载的私钥（服务进程需要能看到该 agent）。",
            foreground="#6b7a8d",
        ).pack(anchor="w")

        sudo_box = ttk.LabelFrame(f, text="提权 sudo（可选）", padding=10)
        sudo_box.grid(row=2, column=0, columnspan=2, sticky="we")
        sudo_box.columnconfigure(1, weight=1)
        self.var_sudo = tk.BooleanVar(value=False)
        self.vars["sudo_enabled"] = self.var_sudo
        ttk.Checkbutton(sudo_box, text="允许 sudo: true 调用", variable=self.var_sudo, command=self._sync).grid(
            row=0, column=0, columnspan=2, sticky="w"
        )
        self._field(sudo_box, 1, "sudo 目标用户", "sudo_user", "留空 = root", 42)
        ttk.Label(sudo_box, text="sudo 密码").grid(row=2, column=0, sticky="w", pady=(0, 4))
        self.var_sudo_pw = tk.StringVar()
        self.vars["sudo_password"] = self.var_sudo_pw
        ttk.Entry(sudo_box, textvariable=self.var_sudo_pw, show="•", width=44, font=FONT).grid(
            row=2, column=1, sticky="we", pady=(0, 4)
        )
        ttk.Label(sudo_box, text="密码存储").grid(row=3, column=0, sticky="w", pady=(0, 4))
        self.var_sudo_mode = tk.StringVar(value=STORE_VALUES[0])
        self.vars["sudo_password_mode_label"] = self.var_sudo_mode
        ttk.Combobox(sudo_box, textvariable=self.var_sudo_mode, values=STORE_VALUES, state="readonly", width=42).grid(
            row=3, column=1, sticky="we", pady=(0, 4)
        )
        self._field(sudo_box, 4, "sudo 环境变量名", "sudo_password_env", "", 42)
        self.lbl_sudo_state = ttk.Label(sudo_box, text="", foreground="#6b7a8d")
        self.lbl_sudo_state.grid(row=5, column=0, columnspan=2, sticky="w")

    def _build_adv(self, f: ttk.Frame) -> None:
        f.columnconfigure(1, weight=1)
        self._field(f, 0, "连接超时(s)", "connect_timeout", "默认 15", 20)
        self._field(f, 1, "命令超时(s)", "command_timeout", "默认 120", 20)
        self._field(f, 2, "keepalive(s)", "keepalive", "默认 30，0 关闭", 20)
        self._field(f, 3, "并发上限", "max_concurrency", "默认 4", 20)
        self._combo(f, 4, "Host key 策略", "host_key_policy", ["auto_add", "strict", "ignore"], 20)
        self._field(f, 5, "known_hosts", "known_hosts", "默认 ~/.ssh/known_hosts", 28)
        self._field(f, 6, "跳板机别名", "proxy_jump", "填另一台的 alias", 20)

    # ----------------------------------------------------------------- state
    def _apply(self, host: dict[str, Any] | None) -> None:
        defaults = {
            "port": "22",
            "shell_kind": "posix",
            "host_key_policy": "auto_add",
            "auth_method": "key",
        }
        for key, var in self.vars.items():
            if key.endswith("_mode_label"):
                continue
            if key in defaults and not (host and host.get(key)):
                var.set(defaults[key])
        if not host:
            self.vars["key_passphrase_mode_label"].set(STORE_VALUES[0])
            self.vars["password_mode_label"].set(STORE_VALUES[0])
            self.vars["sudo_password_mode_label"].set(STORE_VALUES[0])
            self._sync()
            return

        self.vars["alias"].set(host["alias"])
        self.ent_alias.configure(state="readonly")
        for key in ("description", "host", "user", "default_cwd", "posix_shell", "known_hosts"):
            if key in self.vars:
                self.vars[key].set(host.get(key) or "")
        self.vars["port"].set(str(host.get("port") or 22))
        self.vars["tags"].set(", ".join(host.get("tags") or []))
        self.vars["shell_kind"].set(host.get("shell_kind") or "posix")
        self.vars["host_key_policy"].set(host.get("host_key_policy") or "auto_add")
        self.vars["connect_timeout"].set("")
        self.vars["keepalive"].set("")
        self.vars["command_timeout"].set(str(host.get("command_timeout") or ""))
        self.vars["max_concurrency"].set(str(host.get("max_concurrency") or ""))
        self.vars["proxy_jump"].set(host.get("proxy_jump") or "")

        auth = host.get("auth") or {}
        self.var_method.set(auth.get("method") or "key")
        self.vars["key_file"].set(auth.get("key_file") or "")
        src = auth.get("password_source")
        self.lbl_pw_state.configure(text=f"已保存：{src}（密码留空表示不修改）" if src else "还没有保存密码")

        sudo = host.get("sudo") or {}
        self.var_sudo.set(bool(host.get("sudo")))
        self.vars["sudo_user"].set(sudo.get("user") or "")
        ssrc = sudo.get("password_source")
        self.lbl_sudo_state.configure(
            text=f"已保存：{ssrc}（留空表示不修改）" if ssrc else ("留空表示 NOPASSWD" if sudo else "")
        )
        self._sync()

    def _sync(self) -> None:
        method = self.var_method.get()
        if method == "key":
            self.frm_key.grid()
        else:
            self.frm_key.grid_remove()
        if method == "password":
            self.frm_pw.grid()
        else:
            self.frm_pw.grid_remove()
        if method == "agent":
            self.frm_agent.grid()
        else:
            self.frm_agent.grid_remove()

    # ------------------------------------------------------------------ save
    def _num(self, key: str) -> int | None:
        raw = str(self.vars[key].get()).strip()
        if raw == "":
            return None
        try:
            return int(raw)
        except ValueError as exc:
            raise ConfigError(f"{key} 必须是整数：{raw}") from exc

    def payload(self) -> dict[str, Any]:
        mode_key = lambda label_var: STORE_KEYS.get(label_var.get(), "file")  # noqa: E731
        return {
            "alias": str(self.vars["alias"].get()).strip(),
            "host": str(self.vars["host"].get()).strip(),
            "port": self._num("port") or 22,
            "user": str(self.vars["user"].get()).strip(),
            "description": str(self.vars["description"].get()).strip(),
            "tags": str(self.vars["tags"].get()),
            "default_cwd": str(self.vars["default_cwd"].get()).strip(),
            "shell_kind": self.vars["shell_kind"].get() or "posix",
            "posix_shell": str(self.vars["posix_shell"].get()).strip(),
            "connect_timeout": self._num("connect_timeout"),
            "command_timeout": self._num("command_timeout"),
            "keepalive": self._num("keepalive"),
            "max_concurrency": self._num("max_concurrency"),
            "host_key_policy": self.vars["host_key_policy"].get() or "auto_add",
            "known_hosts": str(self.vars["known_hosts"].get()).strip(),
            "proxy_jump": str(self.vars["proxy_jump"].get()).strip(),
            "auth_method": self.var_method.get(),
            "key_file": str(self.vars["key_file"].get()).strip(),
            "key_passphrase": str(self.vars["key_passphrase"].get()),
            "key_passphrase_mode": mode_key(self.vars["key_passphrase_mode_label"]),
            "key_passphrase_env": str(self.vars["key_passphrase_env"].get()).strip(),
            "password": str(self.var_password.get()),
            "password_mode": mode_key(self.vars["password_mode_label"]),
            "password_env": str(self.vars["password_env"].get()).strip(),
            "sudo_enabled": bool(self.var_sudo.get()),
            "sudo_user": str(self.vars["sudo_user"].get()).strip(),
            "sudo_password": str(self.var_sudo_pw.get()),
            "sudo_password_mode": mode_key(self.vars["sudo_password_mode_label"]),
            "sudo_password_env": str(self.vars["sudo_password_env"].get()).strip(),
        }

    def save(self) -> None:
        try:
            data = self.payload()
            if not data["alias"]:
                raise ConfigError("别名不能为空")
            if not data["host"]:
                raise ConfigError("主机地址不能为空")
            upsert_host(self.app.cfg_path, data["alias"], data)
        except ConfigError as exc:
            messagebox.showerror("保存失败", str(exc), parent=self)
            return
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("保存失败", f"{type(exc).__name__}: {exc}", parent=self)
            return
        self.result = data
        self.destroy()


# --------------------------------------------------------------------------- #
# 独立服务进程：桌面端只是个遥控器，关窗口不影响 MCP 端点
# --------------------------------------------------------------------------- #
def _port_open(port: int, timeout: float = 0.25) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            return s.connect_ex(("127.0.0.1", port)) == 0
    except OSError:
        return False


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    try:
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong(0)
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return bool(ok) and code.value == STILL_ACTIVE
    except Exception:  # noqa: BLE001
        return False


class LocalService:
    """用 `python -m ssh_mcp --transport http` 起一个独立进程，pid 记在 service.json。"""

    def __init__(self, cfg_path: Path) -> None:
        self.cfg_path = cfg_path
        self.proc: subprocess.Popen | None = None
        self._log_handle: Any = None

    # ------------------------------------------------------------ state file
    def read_state(self) -> dict[str, Any]:
        try:
            data = json.loads(SERVICE_STATE.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def _write_state(self, data: dict[str, Any]) -> None:
        try:
            SERVICE_STATE.parent.mkdir(parents=True, exist_ok=True)
            SERVICE_STATE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    def _clear_state(self) -> None:
        try:
            SERVICE_STATE.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass

    def status(self) -> dict[str, Any]:
        state = self.read_state()
        pid = int(state.get("pid") or 0)
        port = int(state.get("port") or 0)
        alive = _pid_alive(pid)
        if self.proc is not None and self.proc.poll() is not None:
            self.proc = None
        if alive and port:
            # 有 pid 就用 pid 判断，避免每几秒往服务上打一次空连接
            return {
                "running": True,
                "listening": True,
                "managed": True,
                "pid": pid,
                "port": port,
                "token": state.get("token") or "",
                "config": state.get("config") or str(self.cfg_path),
            }
        # 没有 pid 记录（比如命令行起的）：只能探端口
        listening = bool(port) and _port_open(port)
        return {
            "running": listening,
            "listening": listening,
            "managed": False,
            "pid": None,
            "port": port or None,
            "token": state.get("token") or "",
            "config": state.get("config") or str(self.cfg_path),
        }

    # --------------------------------------------------------------- control
    def start(self, port: int, token: str) -> dict[str, Any]:
        cur = self.status()
        if cur["running"]:
            return {"ok": True, "already": True, **cur}

        exe = Path(sys.executable)
        if exe.name.lower() == "pythonw.exe":
            exe = exe.with_name("python.exe")

        SERVICE_LOG.parent.mkdir(parents=True, exist_ok=True)
        self._log_handle = SERVICE_LOG.open("a", encoding="utf-8", buffering=1)
        self._log_handle.write(f"\n===== start {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
        self._log_handle.flush()

        cmd = [
            str(exe), "-m", "ssh_mcp",
            "--transport", "http",
            "--host", "127.0.0.1",
            "--port", str(port),
            "--config", str(self.cfg_path),
        ]
        if token:
            cmd += ["--token", token]

        flags = 0
        if os.name == "nt":
            flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP

        try:
            self.proc = subprocess.Popen(
                cmd,
                stdout=self._log_handle,
                stderr=self._log_handle,
                stdin=subprocess.DEVNULL,
                creationflags=flags,
                close_fds=True,
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        self._write_state(
            {
                "pid": self.proc.pid,
                "port": port,
                "token": token,
                "config": str(self.cfg_path),
                "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
        return {"ok": True, "pid": self.proc.pid, "port": port, "already": False}

    def stop(self) -> dict[str, Any]:
        state = self.read_state()
        pid = int(state.get("pid") or 0)
        killed = False
        if pid and _pid_alive(pid):
            try:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T", "/F"],
                        capture_output=True,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                        timeout=10,
                    )
                else:
                    os.kill(pid, 15)
                killed = True
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if self.proc is not None:
            try:
                self.proc.terminate()
            except Exception:  # noqa: BLE001
                pass
            self.proc = None
        self._clear_state()
        if self._log_handle is not None:
            try:
                self._log_handle.close()
            except Exception:  # noqa: BLE001
                pass
            self._log_handle = None
        return {"ok": True, "killed": killed, "pid": pid or None}

    def log_tail(self, lines: int = 20) -> str:
        try:
            text = SERVICE_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
            return "\n".join(text[-lines:])
        except Exception:  # noqa: BLE001
            return ""


# --------------------------------------------------------------------------- #
# MCP 客户端配置对话框：把可直接粘贴的 JSON 递给用户
# --------------------------------------------------------------------------- #
class McpConfigDialog(tk.Toplevel):
    def __init__(self, app: "App") -> None:
        super().__init__(app)
        self.app = app
        self.title("MCP 配置")
        self.transient(app)
        self.configure(padx=14, pady=12)
        self.resizable(True, True)

        ttk.Label(
            self,
            text="把下面这段 JSON 贴进客户端的 mcpServers（DeepSeek Harness / Claude Desktop / Cursor 都认）。",
            wraplength=640,
        ).pack(anchor="w")

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, pady=(10, 0))

        port = app.var_port.get().strip()
        running = app.service.status()["listening"]

        # ---- HTTP ---------------------------------------------------------
        f_http = ttk.Frame(nb, padding=10)
        nb.add(f_http, text="  HTTP（服务模式，推荐）  ")
        if running:
            ttk.Label(f_http, text=f"服务运行中：127.0.0.1:{port}", foreground="#15803d").pack(anchor="w", pady=(0, 6))
        else:
            ttk.Label(
                f_http,
                text="服务还没启动：先点右上角「启动 MCP 服务」，这段配置才连得上。",
                foreground="#b45309",
                wraplength=620,
            ).pack(anchor="w", pady=(0, 6))
        self.txt_http = self._code_box(f_http, app.build_http_config())
        row = ttk.Frame(f_http)
        row.pack(fill="x", pady=(8, 0))
        self.lbl_http_copied = ttk.Label(row, text="", foreground="#15803d")
        self.lbl_http_copied.pack(side="left")
        ttk.Button(row, text="复制 HTTP 配置", style="Accent.TButton",
                   command=lambda: self._copy(self.txt_http, self.lbl_http_copied)).pack(side="right")
        ttk.Label(
            f_http,
            text="如果客户端把类型写成 http / streamable_http，把 \"type\" 的值改成它认的写法即可；"
                 "url 与 headers 不用动。",
            foreground="#6b7a8d",
            wraplength=620,
        ).pack(anchor="w", pady=(8, 0))

        # ---- stdio --------------------------------------------------------
        f_stdio = ttk.Frame(nb, padding=10)
        nb.add(f_stdio, text="  stdio（不用常驻服务）  ")
        ttk.Label(
            f_stdio,
            text="由客户端自己拉起一个子进程，适合单会话；不需要桌面端开着服务。",
            foreground="#6b7a8d",
            wraplength=620,
        ).pack(anchor="w", pady=(0, 6))
        self.txt_stdio = self._code_box(f_stdio, app.build_stdio_config())
        row2 = ttk.Frame(f_stdio)
        row2.pack(fill="x", pady=(8, 0))
        self.lbl_stdio_copied = ttk.Label(row2, text="", foreground="#15803d")
        self.lbl_stdio_copied.pack(side="left")
        ttk.Button(row2, text="复制 stdio 配置", command=lambda: self._copy(self.txt_stdio, self.lbl_stdio_copied)).pack(side="right")

        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(12, 0))
        ttk.Label(
            bar,
            text=f"凭据全部留在本机：配置在 {app.cfg_path}，token 在 ~/.ssh-mcp/token",
            foreground="#6b7a8d",
        ).pack(side="left")
        ttk.Button(bar, text="关闭", command=self.destroy).pack(side="right")

        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()
        self._center()
        self.geometry("700x560")

    def _code_box(self, parent: ttk.Frame, text: str) -> tk.Text:
        box = tk.Text(parent, height=11, wrap="none", font=MONO, relief="flat", bg="#0d1117",
                      fg="#d6e2f0", padx=10, pady=8, insertbackground="#d6e2f0")
        box.insert("1.0", text)
        box.configure(state="disabled")
        box.pack(fill="both", expand=True)
        return box

    def _copy(self, widget: tk.Text, label: ttk.Label) -> None:
        text = widget.get("1.0", "end-1c")
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update()
        label.configure(text="已复制到剪贴板")
        self.app._term("# 已复制 MCP 配置（粘贴到客户端的 mcpServers 里）", "dim")
        self.after(2500, lambda: label.configure(text=""))

    def _center(self) -> None:
        self.update_idletasks()
        w, h = 700, 560
        px = self.app.winfo_rootx() + (self.app.winfo_width() - w) // 2
        py = self.app.winfo_rooty() + (self.app.winfo_height() - h) // 2
        self.geometry(f"{w}x{h}+{max(px, 0)}+{max(py, 0)}")


# --------------------------------------------------------------------------- #
# 关于窗口：作者 + 版本 + GitHub 地址
# --------------------------------------------------------------------------- #
class AboutDialog(tk.Toplevel):
    def __init__(self, app: "App") -> None:
        super().__init__(app)
        self.app = app
        self.title("关于 ssh-mcp")
        self.transient(app)
        self.resizable(False, False)
        self.configure(padx=22, pady=18)

        ttk.Label(self, text="ssh-mcp", font=("Microsoft YaHei UI", 17, "bold")).pack(anchor="w")
        ttk.Label(
            self,
            text=f"版本 {__version__}    SSH 凭据留在服务端的 MCP 中间层",
            foreground="#6b7a8d",
        ).pack(anchor="w", pady=(2, 14))

        grid = ttk.Frame(self)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)

        ttk.Label(grid, text="作者", foreground="#6b7a8d").grid(row=0, column=0, sticky="w", pady=(0, 6))
        ttk.Label(grid, text=__author__).grid(row=0, column=1, sticky="w", padx=(12, 0), pady=(0, 6))

        ttk.Label(grid, text="邮箱", foreground="#6b7a8d").grid(row=1, column=0, sticky="w", pady=(0, 6))
        ttk.Label(grid, text=__author_email__).grid(row=1, column=1, sticky="w", padx=(12, 0), pady=(0, 6))

        ttk.Label(grid, text="GitHub", foreground="#6b7a8d").grid(row=2, column=0, sticky="w", pady=(0, 6))
        link = tk.Label(grid, text=__github__, fg="#0a66c2", cursor="hand2", font=("Microsoft YaHei UI", 9, "underline"))
        link.grid(row=2, column=1, sticky="w", padx=(12, 0), pady=(0, 6))
        link.bind("<Button-1>", lambda e: self.open_github())

        ttk.Label(
            self,
            text="凭据只保存在本机；会话端只拿到别名。",
            foreground="#6b7a8d",
        ).pack(anchor="w", pady=(10, 0))

        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(16, 0))
        ttk.Button(bar, text="复制地址", command=self.copy_link).pack(side="left")
        ttk.Button(bar, text="打开 GitHub", style="Accent.TButton", command=self.open_github).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="关闭", command=self.destroy).pack(side="right")

        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()
        self.update_idletasks()
        w, h = max(self.winfo_reqwidth(), 420), self.winfo_reqheight()
        px = app.winfo_rootx() + (app.winfo_width() - w) // 2
        py = app.winfo_rooty() + (app.winfo_height() - h) // 2
        self.geometry(f"{w}x{h}+{max(px, 0)}+{max(py, 0)}")

    def open_github(self) -> None:
        try:
            import webbrowser

            webbrowser.open(__github__)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("打开失败", str(exc), parent=self)

    def copy_link(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(__github__)
        self.update()
        self.app._term(f"# 已复制仓库地址：{__github__}", "dim")



# --------------------------------------------------------------------------- #
# 主窗口
# --------------------------------------------------------------------------- #
class App(tk.Tk):
    def __init__(self, config_path: Path) -> None:
        super().__init__()
        self.cfg_path = config_path
        created = ensure_config(config_path)
        self.rt = srv.init_runtime(str(config_path))
        self.runner = AsyncRunner(self)
        self._current: str | None = None
        self.service = LocalService(config_path)
        self._starting = False

        self.title(f"{APP_TITLE} — {config_path}")
        self.geometry("1280x800")
        self.minsize(1100, 660)
        self.configure(bg="#f3f4f6")
        self._init_style()
        self._build_ui()
        self._load_ui_state()
        self._refresh_hosts()
        self._sync_service_ui()
        self.after(3000, self._poll_service)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        # tkinter 默认把回调异常打到 stderr，pythonw 下等于丢进黑洞
        self.report_callback_exception = self._on_tk_error
        if created:
            self._term(f"# 已创建新配置：{config_path}", "dim")
            self._term("# 点「新增主机」开始；凭据只保存在本机，界面不会回显密码。", "dim")

    # ----------------------------------------------------------------- style
    def _init_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            style.theme_use("clam")
        self.option_add("*Font", FONT)
        style.configure(".", font=FONT)
        style.configure("Treeview", rowheight=26, font=FONT, fieldbackground="#ffffff")
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("Accent.TButton", font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("Hint.TLabel", foreground="#6b7a8d")
        style.configure("Status.TLabel", foreground="#4b5563")

    # -------------------------------------------------------------------- ui
    def _build_ui(self) -> None:
        bar = ttk.Frame(self, padding=(12, 10, 12, 6))
        bar.pack(fill="x")
        ttk.Button(bar, text="新增主机", style="Accent.TButton", command=self.on_add).pack(side="left")
        ttk.Button(bar, text="编辑", command=self.on_edit).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="删除", command=self.on_delete).pack(side="left", padx=(6, 0))
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=12)
        ttk.Button(bar, text="测试连接", command=self.on_test).pack(side="left")
        ttk.Button(bar, text="刷新", command=self._refresh_hosts).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="MCP 配置", style="Accent.TButton", command=self.show_mcp_config).pack(side="left", padx=(6, 0))

        more = ttk.Menubutton(bar, text="更多")
        menu = tk.Menu(more, tearoff=0)
        menu.add_command(label="打开 servers.yaml", command=self.on_open_config)
        menu.add_command(label="打开凭据目录", command=self.on_open_creds)
        menu.add_command(label="打开桌面端日志", command=self.on_open_log)
        menu.add_command(label="打开服务日志", command=self.on_open_service_log)
        menu.add_separator()
        menu.add_command(label="复制当前配置路径", command=self.on_copy_config_path)
        menu.add_separator()
        menu.add_command(label=f"关于 ssh-mcp  v{__version__}", command=self.show_about)
        more["menu"] = menu
        more.pack(side="left", padx=(6, 0))

        svc = ttk.Frame(bar)
        svc.pack(side="right")
        ttk.Label(svc, text="端口").pack(side="left", padx=(0, 4))
        self.var_port = tk.StringVar(value="8765")
        ttk.Entry(svc, textvariable=self.var_port, width=6).pack(side="left")
        ttk.Label(svc, text="Token").pack(side="left", padx=(8, 4))
        self.var_token = tk.StringVar(value="")
        ttk.Entry(svc, textvariable=self.var_token, width=12, show="•").pack(side="left")
        self.btn_service = ttk.Button(svc, text="启动 MCP 服务", command=self.toggle_service)
        self.btn_service.pack(side="left", padx=(8, 0))
        self.lbl_service = ttk.Label(svc, text="● 未启动", style="Status.TLabel")
        self.lbl_service.pack(side="left", padx=(8, 0))

        body = ttk.PanedWindow(self, orient="horizontal")
        body.pack(fill="both", expand=True, padx=12, pady=(0, 8))

        left = ttk.Frame(body)
        cols = ("alias", "target", "user", "auth", "tags", "state")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", selectmode="browse")
        for key, text, width, anchor in (
            ("alias", "别名", 130, "w"),
            ("target", "目标", 150, "w"),
            ("user", "用户", 90, "w"),
            ("auth", "认证", 70, "w"),
            ("tags", "标签", 120, "w"),
            ("state", "状态", 70, "w"),
        ):
            self.tree.heading(key, text=text)
            self.tree.column(key, width=width, anchor=anchor, stretch=(key in ("target", "tags")))
        self.tree.tag_configure("online", foreground="#15803d")
        self.tree.tag_configure("offline", foreground="#6b7280")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._on_select())
        self.tree.bind("<Double-1>", lambda e: self.on_edit())
        sb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        body.add(left, weight=3)

        right = ttk.Frame(body)
        body.add(right, weight=4)

        detail = ttk.LabelFrame(right, text=" 主机详情 ", padding=10)
        detail.pack(fill="x")
        self.detail = tk.Text(detail, height=8, wrap="word", font=FONT, relief="flat", bg="#ffffff", padx=8, pady=6)
        self.detail.pack(fill="x")
        self.detail.tag_configure("k", foreground="#6b7a8d")
        self.detail.tag_configure("v", foreground="#111827")
        self.detail.configure(state="disabled")

        term = ttk.LabelFrame(right, text=" 命令终端 ", padding=(10, 8))
        term.pack(fill="both", expand=True, pady=(10, 0))
        self.terminal = tk.Text(term, wrap="word", font=MONO, relief="flat", bg="#0d1117", fg="#d6e2f0", padx=10, pady=8)
        self.terminal.pack(fill="both", expand=True)
        self.terminal.tag_configure("cmd", foreground="#7ee787")
        self.terminal.tag_configure("err", foreground="#ff7b72")
        self.terminal.tag_configure("dim", foreground="#8b98a8")
        self.terminal.tag_configure("out", foreground="#d6e2f0")
        self.terminal.configure(state="disabled")

        cmdbar = ttk.Frame(term)
        cmdbar.pack(fill="x", pady=(8, 0))
        self.var_sudo_exec = tk.BooleanVar(value=False)
        ttk.Checkbutton(cmdbar, text="sudo", variable=self.var_sudo_exec).pack(side="left")
        self.var_cmd = tk.StringVar()
        self.ent_cmd = ttk.Entry(cmdbar, textvariable=self.var_cmd, font=MONO)
        self.ent_cmd.pack(side="left", fill="x", expand=True, padx=8)
        self.ent_cmd.bind("<Return>", lambda e: self.on_run())
        self.ent_cmd.configure(state="disabled")
        self.btn_run = ttk.Button(cmdbar, text="执行", command=self.on_run, state="disabled")
        self.btn_run.pack(side="left")

        self.status = ttk.Label(self, text="", style="Status.TLabel", padding=(12, 4))
        self.status.pack(fill="x", side="bottom")

        # 作者信息常驻状态栏右侧，点击打开「关于」
        self.credit = ttk.Label(
            self,
            text=f"by {__author__}  ·  {__github__.replace('https://', '')}",
            foreground="#0a66c2",
            cursor="hand2",
            padding=(12, 4),
        )
        self.credit.pack(fill="x", side="bottom", anchor="e")
        self.credit.configure(anchor="e")
        self.credit.bind("<Button-1>", lambda e: self.show_about())

    # ------------------------------------------------------------- formatting
    def _term(self, text: str, tag: str = "out") -> None:
        self.terminal.configure(state="normal")
        if self.terminal.index("end-1c") != "1.0":
            self.terminal.insert("end", "\n")
        self.terminal.insert("end", text, tag)
        self.terminal.see("end")
        self.terminal.configure(state="disabled")

    def _clear_term(self) -> None:
        self.terminal.configure(state="normal")
        self.terminal.delete("1.0", "end")
        self.terminal.configure(state="disabled")

    def show_error(self, exc: BaseException) -> None:
        log_line(f"error: {type(exc).__name__}: {exc}\n{traceback.format_exc()}")
        messagebox.showerror("出错了", f"{type(exc).__name__}: {exc}")

    def _on_tk_error(self, exc_type, exc_value, tb) -> None:  # noqa: ANN001
        text = "".join(traceback.format_exception(exc_type, exc_value, tb))
        log_line("tk callback error:\n" + text)
        try:
            self._term(f"[界面错误] {exc_type.__name__}: {exc_value}", "err")
        except Exception:  # noqa: BLE001
            pass
        messagebox.showerror("操作失败", f"{exc_type.__name__}: {exc_value}\n\n详情见 {LOG_PATH}")

    # ------------------------------------------------------------------ list
    def _refresh_hosts(self) -> None:
        cfg = self.rt.cfg
        conns = {c["alias"]: c for c in self.rt.pool.status()}
        keep = self._current
        self.tree.delete(*self.tree.get_children())
        for alias, host in sorted(cfg.hosts.items()):
            connected = bool(conns.get(alias, {}).get("connected"))
            self.tree.insert(
                "",
                "end",
                iid=alias,
                values=(
                    alias,
                    f"{host.host}:{host.port}",
                    host.user or "-",
                    AUTH_LABEL.get(host.auth.method, host.auth.method),
                    ", ".join(host.tags) or "-",
                    "在线" if connected else "未连接",
                ),
                tags=("online" if connected else "offline",),
            )
        if keep and self.tree.exists(keep):
            self.tree.selection_set(keep)
            self._on_select()
        else:
            self._show_detail_empty()
        self._update_status()

    def _update_status(self) -> None:
        rt = self.rt
        online = sum(1 for c in rt.pool.status() if c["connected"])
        self.status.configure(
            text=f"配置：{self.cfg_path}    主机 {len(rt.cfg.hosts)}    在线 {online}    "
            f"审计：{'开' if rt.cfg.audit.enabled else '关'}    "
            f"策略：sudo {'开' if rt.cfg.policy.allow_sudo else '关'} / "
            f"黑名单 {len(rt.cfg.policy.deny_commands)} 条"
        )

    def _show_detail_empty(self) -> None:
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        if not self.rt.cfg.hosts:
            self.detail.insert("end", "还没有主机。点「新增主机」填表保存即可，无需手改 YAML。\n", "k")
        else:
            self.detail.insert("end", "从左侧选一台主机查看详情；选中后可在下方终端直接执行命令。\n", "k")
        self.detail.configure(state="disabled")
        self._current = None
        self.ent_cmd.configure(state="disabled")
        self.btn_run.configure(state="disabled")

    def _on_select(self) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        alias = sel[0]
        host = self.rt.cfg.hosts.get(alias)
        if host is None:
            return
        self._current = alias
        self.ent_cmd.configure(state="normal")
        self.btn_run.configure(state="normal")
        self._show_detail(host)

    def _show_detail(self, host: Host) -> None:
        d = host.describe()
        auth = d["auth"]
        lines: list[tuple[str, str]] = [
            ("别名", f"{d['alias']}    {d['description'] or ''}\n"),
            ("目标", f"{d['host']}:{d['port']}    用户 {d['user'] or '-'}    shell {d['shell_kind']}\n"),
            (
                "认证",
                f"{AUTH_LABEL.get(auth['method'], auth['method'])}"
                + (f"    私钥 {auth['key_file']}" if auth.get("key_file") else "")
                + (f"    密码来源 {auth['password_source']}" if auth.get("password_source") else "")
                + "\n",
            ),
        ]
        if d.get("sudo"):
            sudo = d["sudo"]
            lines.append(
                ("sudo", f"已启用    用户 {sudo.get('user') or 'root'}    密码来源 {sudo.get('password_source') or '无(NOPASSWD)'}\n")
            )
        lines.append(
            (
                "其它",
                f"标签 {', '.join(d['tags']) or '-'}    "
                f"超时 {d['command_timeout']}s    host key {d['host_key_policy']}    "
                f"并发 {d['max_concurrency']}"
                + (f"    跳板 {d['proxy_jump']}" if d.get("proxy_jump") else "")
                + "\n",
            )
        )
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        for key, value in lines:
            self.detail.insert("end", f"{key}：", "k")
            self.detail.insert("end", value, "v")
        self.detail.configure(state="disabled")

    # --------------------------------------------------------------- actions
    def _require_selection(self) -> str | None:
        if not self._current:
            messagebox.showinfo("提示", "先在左侧选一台主机", parent=self)
            return None
        return self._current

    def on_add(self) -> None:
        dlg = HostDialog(self, None)
        self.wait_window(dlg)
        if dlg.result:
            self._after_save(dlg.result["alias"])

    def on_edit(self) -> None:
        alias = self._require_selection()
        if not alias:
            return
        host = self.rt.cfg.hosts[alias].describe()
        dlg = HostDialog(self, host)
        self.wait_window(dlg)
        if dlg.result:
            self._after_save(dlg.result["alias"])

    def _after_save(self, alias: str) -> None:
        self._current = alias
        self._reload("已保存 " + alias)

    def on_delete(self) -> None:
        alias = self._require_selection()
        if not alias:
            return
        if not messagebox.askyesno(
            "删除主机",
            f"删除 {alias}？\n\n会断开它的连接并改写配置文件（原文件已备份 .bak）。",
            parent=self,
        ):
            return
        try:
            delete_host(self.cfg_path, alias)
        except (ConfigError, KeyError) as exc:
            messagebox.showerror("删除失败", str(exc), parent=self)
            return
        self._current = None
        self._reload("已删除 " + alias)

    def on_open_config(self) -> None:
        try:
            os.startfile(str(self.cfg_path))  # noqa: S606 - Windows 打开编辑器
        except Exception as exc:  # noqa: BLE001
            self.show_error(exc)

    def on_open_creds(self) -> None:
        target = cred_dir()
        target.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(target))  # noqa: S606
        except Exception as exc:  # noqa: BLE001
            self.show_error(exc)

    def on_open_log(self) -> None:
        try:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            LOG_PATH.touch(exist_ok=True)
            os.startfile(str(LOG_PATH))  # noqa: S606
        except Exception as exc:  # noqa: BLE001
            self.show_error(exc)

    def on_open_service_log(self) -> None:
        try:
            SERVICE_LOG.parent.mkdir(parents=True, exist_ok=True)
            SERVICE_LOG.touch(exist_ok=True)
            os.startfile(str(SERVICE_LOG))  # noqa: S606
        except Exception as exc:  # noqa: BLE001
            self.show_error(exc)

    def on_copy_config_path(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(str(self.cfg_path))
        self._term(f"# 已复制配置路径：{self.cfg_path}", "dim")

    def on_test(self) -> None:
        alias = self._require_selection()
        if not alias:
            return
        host = self.rt.cfg.hosts[alias]
        if host.shell_kind == "posix":
            probe = "uname -a; id; date"
        elif host.shell_kind == "cmd":
            probe = "ver && whoami"
        else:  # powershell 5.1 不支持 &&，用分号
            probe = "Write-Output ('PowerShell ' + $PSVersionTable.PSVersion.ToString()); whoami; hostname"
        self._term(f"# 测试 {alias} → {host.host}:{host.port} …", "dim")
        self.runner.submit(
            self.rt.pool.exec(alias, probe, timeout=20),
            on_done=lambda res: self._test_done(alias, res),
            on_error=lambda exc: self._term(f"[失败] {type(exc).__name__}: {exc}", "err"),
        )

    def _test_done(self, alias: str, res: Any) -> None:
        payload = res.to_dict()
        if payload["ok"]:
            self._term(f"# 连接成功 {alias}  {payload['duration_ms']}ms", "cmd")
            self._term((payload["stdout"] or payload["stderr"]).strip(), "out")
            self.rt.audit.write("desktop_test", alias=alias, exit_code=payload["exit_code"])
        elif payload["exit_code"] not in (None, -1) and payload["stdout"]:
            # SSH 通了，只是探测命令返回非零（比如远端 shell 不支持某条命令）
            self._term(f"# 已连通 {alias}，探测命令返回 {payload['exit_code']}  {payload['duration_ms']}ms", "cmd")
            body = (payload["stdout"] + payload["stderr"]).strip()
            self._term(body, "out")
        else:
            self._term(f"# 连接失败 {alias}\n{payload['stderr'].strip()}", "err")
        self._refresh_hosts()

    def on_run(self) -> None:
        alias = self._require_selection()
        if not alias:
            return
        cmd = self.var_cmd.get().strip()
        if not cmd:
            return
        self.var_cmd.set("")
        self._term(f"$ {cmd}" + ("    (sudo)" if self.var_sudo_exec.get() else ""), "cmd")
        self.btn_run.configure(state="disabled")
        self.runner.submit(
            self.rt.pool.exec(alias, cmd, sudo=bool(self.var_sudo_exec.get())),
            on_done=self._run_done,
            on_error=self._run_failed,
        )

    def _run_done(self, res: Any) -> None:
        payload = res.to_dict()
        if payload["stdout"]:
            self._term(payload["stdout"].rstrip(), "out")
        if payload["stderr"]:
            self._term(payload["stderr"].rstrip(), "err")
        suffix = " 已截断" if payload["truncated"] else ""
        tag = "dim" if payload["ok"] else "err"
        self._term(f"[exit {payload['exit_code']}] {payload['duration_ms']}ms{suffix}", tag)
        self.rt.audit.write(
            "desktop_exec",
            alias=payload["alias"],
            command=payload["command"],
            exit_code=payload["exit_code"],
            ok=payload["ok"],
        )
        self.btn_run.configure(state="normal")
        self.ent_cmd.focus_set()
        self._refresh_hosts()

    def _run_failed(self, exc: BaseException) -> None:
        self._term(f"[错误] {type(exc).__name__}: {exc}", "err")
        self.btn_run.configure(state="normal")

    # ---------------------------------------------------------------- reload
    def _reload(self, message: str | None = None) -> None:
        async def job() -> None:
            await self.rt.pool.close()
            self.rt.reload()

        def done(_: Any) -> None:
            self._refresh_hosts()
            if message:
                self._term(f"# {message}", "dim")
            self._update_status()

        self.runner.submit(job(), on_done=done)

    # --------------------------------------------------------------- service
    def build_http_config(self) -> str:
        port = self.var_port.get().strip() or "8765"
        token = self.var_token.get().strip()
        entry: dict[str, Any] = {"type": "streamable-http", "url": f"http://127.0.0.1:{port}/mcp"}
        if token:
            entry["headers"] = {"Authorization": f"Bearer {token}"}
        return json.dumps({"mcpServers": {"ssh": entry}}, indent=2, ensure_ascii=False)

    def build_stdio_config(self) -> str:
        exe = Path(sys.executable)
        if exe.name.lower() == "pythonw.exe":
            # stdio 要往 stdout 写协议，必须是带控制台的 python.exe
            exe = exe.with_name("python.exe")
        entry = {
            "command": str(exe),
            "args": ["-m", "ssh_mcp", "--transport", "stdio", "--config", str(self.cfg_path)],
        }
        return json.dumps({"mcpServers": {"ssh": entry}}, indent=2, ensure_ascii=False)

    def show_mcp_config(self) -> None:
        dlg = McpConfigDialog(self)
        self.wait_window(dlg)

    def show_about(self) -> None:
        dlg = AboutDialog(self)
        self.wait_window(dlg)

    def _load_ui_state(self) -> None:
        """记住端口与 token，省得每次开窗口重填。"""
        loaded = False
        try:
            data = json.loads(UI_STATE_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            data = None
        if isinstance(data, dict):
            loaded = True
            if data.get("port"):
                self.var_port.set(str(data["port"]))
            if data.get("token"):
                self.var_token.set(str(data["token"]))
        if not loaded:
            # 沿用命令行 / start-http.ps1 生成过的 token
            token_file = Path.home() / ".ssh-mcp" / "token"
            try:
                if token_file.is_file():
                    self.var_token.set(token_file.read_text(encoding="utf-8").strip())
            except Exception:  # noqa: BLE001
                pass

    def _save_ui_state(self) -> None:
        try:
            UI_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            payload = {"port": self.var_port.get().strip(), "token": self.var_token.get().strip()}
            UI_STATE_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            try:
                os.chmod(UI_STATE_PATH, 0o600)
            except OSError:
                pass
        except Exception:  # noqa: BLE001
            pass

    def toggle_service(self) -> None:
        st = self.service.status()
        if st["running"]:
            if not st["managed"]:
                messagebox.showinfo(
                    "服务由外部启动",
                    f"127.0.0.1:{st['port']} 上的 MCP 服务不是这个窗口起的（没有 pid 记录），"
                    "请到启动它的地方关闭（比如命令行或计划任务）。",
                    parent=self,
                )
                return
            res = self.service.stop()
            if res.get("ok"):
                self._term(f"# MCP 服务已停止（pid {res.get('pid')}）", "dim")
                log_line(f"service stopped pid={res.get('pid')}")
            else:
                self._term(f"[停止失败] {res.get('error')}", "err")
            self._sync_service_ui()
            return

        try:
            port = int(self.var_port.get())
        except ValueError:
            messagebox.showerror("端口非法", "端口必须是整数", parent=self)
            return
        token = self.var_token.get().strip()

        res = self.service.start(port, token)
        if not res.get("ok"):
            self._term(f"[MCP 服务启动失败] {res.get('error')}", "err")
            self._term(f"# 详情：{SERVICE_LOG}", "dim")
            messagebox.showerror("启动失败", f"{res.get('error')}\n\n日志：{SERVICE_LOG}", parent=self)
            return

        self._starting = True
        self.lbl_service.configure(text=f"● 启动中 :{port}", foreground="#b45309")
        self._term(f"# 正在启动 MCP 服务 127.0.0.1:{port}（独立进程 pid {res.get('pid')}）…", "dim")
        self._term("# 关掉桌面端也不影响它；日志见 更多 → 打开服务日志", "dim")
        log_line(f"service spawn pid={res.get('pid')} port={port} token={'yes' if token else 'no'}")
        self.after(300, lambda: self._wait_ready(port, token, 0))

    def _wait_ready(self, port: int, token: str, attempt: int) -> None:
        if _port_open(port):
            self._starting = False
            self._sync_service_ui()
            self._term(f"# MCP 端点已就绪：http://127.0.0.1:{port}/mcp", "cmd")
            if token:
                self._term("# 会话端需要带：Authorization: Bearer " + token, "dim")
            self._term("# 点工具栏「MCP 配置」可直接复制客户端 JSON", "dim")
            log_line(f"service ready on {port}")
            return
        if attempt >= 40:  # 约 10 秒
            self._starting = False
            self._sync_service_ui()
            self._term(f"# 服务没能在 :{port} 起来，看日志：{SERVICE_LOG}", "err")
            tail = self.service.log_tail(12).strip()
            if tail:
                self._term(tail, "err")
            return
        self.after(250, lambda: self._wait_ready(port, token, attempt + 1))

    def _sync_service_ui(self) -> None:
        st = self.service.status()
        if st["running"] and st["managed"]:
            self.lbl_service.configure(text=f"● 运行中 :{st['port']}  pid {st['pid']}", foreground="#15803d")
            self.btn_service.configure(text="停止 MCP 服务")
        elif st["running"]:
            self.lbl_service.configure(text=f"● 运行中（外部）:{st['port']}", foreground="#15803d")
            self.btn_service.configure(text="启动 MCP 服务")
        else:
            self.lbl_service.configure(text="● 未启动", foreground="#6b7280")
            self.btn_service.configure(text="启动 MCP 服务")

    def _poll_service(self) -> None:
        """外部启停服务时，窗口状态自动跟上。"""
        try:
            if not self._starting:
                self._sync_service_ui()
        except Exception:  # noqa: BLE001
            pass
        self.after(3000, self._poll_service)

    # ----------------------------------------------------------------- close
    def on_close(self) -> None:
        self._save_ui_state()
        st = self.service.status()
        if st["running"] and st["managed"]:
            stop_it = messagebox.askyesno(
                "MCP 服务正在后台运行",
                f"MCP 服务在后台跑着（127.0.0.1:{st['port']}，pid {st['pid']}）。\n"
                "关掉这个窗口不会影响它，客户端可以继续用。\n\n"
                "要顺便把服务也停掉吗？",
                parent=self,
            )
            if stop_it:
                self.service.stop()
                log_line("service stopped on exit")
        try:
            self.runner.submit(self.rt.pool.close())
        except Exception:  # noqa: BLE001
            pass
        self.after(120, self._finish_close)

    def _finish_close(self) -> None:
        self.runner.shutdown()
        self.destroy()


def resolve_config(arg: str | None) -> Path:
    if arg:
        return Path(os.path.expandvars(os.path.expanduser(arg)))
    env = os.environ.get("SSH_MCP_CONFIG")
    if env:
        return Path(os.path.expandvars(os.path.expanduser(env)))
    for cand in (Path.cwd() / "servers.yaml", Path.home() / ".ssh-mcp" / "servers.yaml"):
        if cand.is_file():
            return cand
    return Path.home() / ".ssh-mcp" / "servers.yaml"


def enable_dpi_awareness() -> None:
    try:
        from ctypes import windll

        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ssh-mcp-desktop", description="ssh-mcp 桌面管理端")
    parser.add_argument("--config", default=None, help="servers.yaml 路径（缺省自动查找/创建）")
    parser.add_argument("--print-config-path", action="store_true", help="只打印解析到的配置路径")
    args = parser.parse_args(argv)

    cfg_path = resolve_config(args.config)
    if args.print_config_path:
        print(cfg_path)
        return 0

    ensure_stdio()
    enable_dpi_awareness()
    try:
        app = App(cfg_path)
    except ConfigError as exc:
        log_line(f"config error: {exc}")
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("配置错误", str(exc))
        root.destroy()
        return 2
    except Exception as exc:  # noqa: BLE001
        log_line("startup failed:\n" + traceback.format_exc())
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("启动失败", f"{type(exc).__name__}: {exc}\n\n详情见 {LOG_PATH}")
        root.destroy()
        return 3
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

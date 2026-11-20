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
from tkinter import font as tkfont
from tkinter import messagebox, ttk

from . import server as srv
from . import __author__, __author_email__, __github__, __version__
from . import update as updater
from .config import ConfigError, Host
from .config_store import cred_dir, delete_host, ensure_config, upsert_host
from .pool import strip_ansi

APP_TITLE = "ssh-mcp 桌面端"
FONT = ("Microsoft YaHei UI", 9)
FONT_SM = ("Microsoft YaHei UI", 8)
FONT_BOLD = ("Microsoft YaHei UI", 9, "bold")
FONT_BRAND = ("Microsoft YaHei UI", 13, "bold")
MONO = ("Consolas", 9)

# 配色（浅色、克制、接近 GitHub 的观感）
C = {
    "bg": "#f2f4f7",
    "card": "#ffffff",
    "stripe": "#fafbfc",
    "line": "#e3e6ea",
    "line2": "#cfd5dd",
    "hover": "#eef1f5",
    "head": "#f6f8fa",
    "txt": "#1f2328",
    "dim": "#6b7280",
    "accent": "#2f6feb",
    "accent_dark": "#1f5bd0",
    "accent_soft": "#e8f0ff",
    "ok": "#1a7f37",
    "sel": "#e8f0fe",
    "chip_off": "#eceff3",
    "chip_on": "#e6f4ea",
    "chip_warn": "#fff4e5",
    "warn": "#8a5300",
    "term_bg": "#0d1117",
    "term_fg": "#c9d1d9",
}
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
        ttk.Label(bar, text="保存前会先校验配置，原文件备份为 .bak", foreground=C["dim"]).pack(side="left")
        ttk.Button(bar, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(bar, text="保存", style="Primary.TButton", command=self.save).pack(side="right", padx=(0, 8))

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
            ttk.Label(parent, text=tip, foreground=C["dim"]).grid(row=row, column=2, sticky="w", padx=(8, 0))
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

        # 私钥来源：文件路径 / 直接粘贴 PEM（不落地成文件）
        ttk.Label(self.frm_key, text="私钥来源").grid(row=0, column=0, sticky="w", pady=(0, 4))
        src_row = ttk.Frame(self.frm_key)
        src_row.grid(row=0, column=1, columnspan=2, sticky="w", pady=(0, 4))
        self.var_key_source = tk.StringVar(value="file")
        self.vars["key_source"] = self.var_key_source
        for value, text in (("file", "文件路径"), ("inline", "直接粘贴内容")):
            ttk.Radiobutton(
                src_row, text=text, value=value, variable=self.var_key_source, command=self._sync_key_source
            ).pack(side="left", padx=(0, 14))

        self.ent_key_file = self._field(self.frm_key, 1, "私钥路径", "key_file", "如 ~/.ssh/id_ed25519", 40)

        self.lbl_key_content = ttk.Label(self.frm_key, text="私钥内容")
        self.lbl_key_content.grid(row=2, column=0, sticky="nw", pady=(0, 4))
        box = ttk.Frame(self.frm_key)
        box.grid(row=2, column=1, columnspan=2, sticky="we", pady=(0, 2))
        box.columnconfigure(0, weight=1)
        self.txt_key_content = tk.Text(
            box, height=7, wrap="none", font=("Consolas", 9), relief="flat",
            bg=C["bg"], fg=C["txt"], padx=8, pady=6, highlightthickness=1,
            highlightbackground=C["line"], undo=True,
        )
        self.txt_key_content.grid(row=0, column=0, sticky="we")
        bar = ttk.Scrollbar(box, orient="vertical", command=self.txt_key_content.yview)
        bar.grid(row=0, column=1, sticky="ns")
        self.txt_key_content.configure(yscrollcommand=bar.set)
        ttk.Label(
            self.frm_key, text="把 -----BEGIN ... PRIVATE KEY----- 整段贴进来", foreground=C["dim"]
        ).grid(row=3, column=1, columnspan=2, sticky="w", pady=(0, 8))

        self._field(self.frm_key, 4, "私钥口令", "key_passphrase", "留空 = 不修改", 40)
        ttk.Label(self.frm_key, text="口令存储").grid(row=5, column=0, sticky="w", pady=(0, 4))
        self.vars["key_passphrase_mode_label"] = tk.StringVar(value=STORE_VALUES[0])
        ttk.Combobox(
            self.frm_key,
            textvariable=self.vars["key_passphrase_mode_label"],
            values=STORE_VALUES,
            state="readonly",
            width=38,
        ).grid(row=5, column=1, sticky="we", pady=(0, 4))
        self._field(self.frm_key, 6, "口令环境变量名", "key_passphrase_env", "选环境变量时填", 40)
        self._sync_key_source()

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
        self.lbl_pw_state = ttk.Label(self.frm_pw, text="", foreground=C["dim"])
        self.lbl_pw_state.grid(row=3, column=0, columnspan=2, sticky="w")

        self.frm_agent = ttk.Frame(f)
        self.frm_agent.grid(row=1, column=0, columnspan=2, sticky="we", pady=(0, 8))
        ttk.Label(
            self.frm_agent,
            text="使用本机 ssh-agent 里已加载的私钥（服务进程需要能看到该 agent）。",
            foreground=C["dim"],
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
        self.lbl_sudo_state = ttk.Label(sudo_box, text="", foreground=C["dim"])
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
        # 密钥正文不回显；编辑已有内联私钥时输入框留空即保持原值
        inline = bool(auth.get("has_key_content")) or str(auth.get("key_source") or "").startswith("inline")
        self.var_key_source.set("inline" if inline else "file")
        self.txt_key_content.delete("1.0", "end")
        if inline:
            self.txt_key_content.insert("1.0", "（已保存的内联私钥，留空保存则保持不变）")
            self.txt_key_content.configure(fg=C["dim"])
            self.txt_key_content.bind("<Key>", self._clear_key_placeholder, add="+")
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
        self._sync_key_source()

    def _clear_key_placeholder(self, _event=None) -> None:
        """编辑已保存的内联私钥时，第一次敲键盘就把占位提示清掉。"""
        text = self.txt_key_content.get("1.0", "end-1c")
        if text.startswith("（已保存的内联私钥"):
            self.txt_key_content.delete("1.0", "end")
            self.txt_key_content.configure(fg=C["txt"])

    def _sync_key_source(self) -> None:
        """文件路径 / 直接粘贴，二选一，切换时把另一块收起来。"""
        inline = self.var_key_source.get() == "inline"
        if inline:
            self.ent_key_file.configure(state="disabled")
            self.lbl_key_content.grid()
            self.txt_key_content.master.grid()
        else:
            self.ent_key_file.configure(state="normal")
            self.lbl_key_content.grid_remove()
            self.txt_key_content.master.grid_remove()

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
            "key_source": self.var_key_source.get(),
            "key_file": str(self.vars["key_file"].get()).strip(),
            "key_content": ("" if self.txt_key_content.get("1.0", "end-1c").startswith("（已保存的内联私钥") else self.txt_key_content.get("1.0", "end-1c")),
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
            ttk.Label(f_http, text=f"服务运行中：127.0.0.1:{port}", foreground=C["ok"]).pack(anchor="w", pady=(0, 6))
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
        self.lbl_http_copied = ttk.Label(row, text="", foreground=C["ok"])
        self.lbl_http_copied.pack(side="left")
        ttk.Button(row, text="复制 HTTP 配置", style="Primary.TButton",
                   command=lambda: self._copy(self.txt_http, self.lbl_http_copied)).pack(side="right")
        ttk.Label(
            f_http,
            text="如果客户端把类型写成 http / streamable_http，把 \"type\" 的值改成它认的写法即可；"
                 "url 与 headers 不用动。",
            foreground=C["dim"],
            wraplength=620,
        ).pack(anchor="w", pady=(8, 0))

        # ---- stdio --------------------------------------------------------
        f_stdio = ttk.Frame(nb, padding=10)
        nb.add(f_stdio, text="  stdio（不用常驻服务）  ")
        ttk.Label(
            f_stdio,
            text="由客户端自己拉起一个子进程，适合单会话；不需要桌面端开着服务。",
            foreground=C["dim"],
            wraplength=620,
        ).pack(anchor="w", pady=(0, 6))
        self.txt_stdio = self._code_box(f_stdio, app.build_stdio_config())
        row2 = ttk.Frame(f_stdio)
        row2.pack(fill="x", pady=(8, 0))
        self.lbl_stdio_copied = ttk.Label(row2, text="", foreground=C["ok"])
        self.lbl_stdio_copied.pack(side="left")
        ttk.Button(row2, text="复制 stdio 配置", command=lambda: self._copy(self.txt_stdio, self.lbl_stdio_copied)).pack(side="right")

        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(12, 0))
        ttk.Label(
            bar,
            text=f"凭据全部留在本机：配置在 {app.cfg_path}，token 在 ~/.ssh-mcp/token",
            foreground=C["dim"],
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
            foreground=C["dim"],
        ).pack(anchor="w", pady=(2, 14))

        grid = ttk.Frame(self)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)

        ttk.Label(grid, text="作者", foreground=C["dim"]).grid(row=0, column=0, sticky="w", pady=(0, 6))
        ttk.Label(grid, text=__author__).grid(row=0, column=1, sticky="w", padx=(12, 0), pady=(0, 6))

        ttk.Label(grid, text="邮箱", foreground=C["dim"]).grid(row=1, column=0, sticky="w", pady=(0, 6))
        ttk.Label(grid, text=__author_email__).grid(row=1, column=1, sticky="w", padx=(12, 0), pady=(0, 6))

        ttk.Label(grid, text="GitHub", foreground=C["dim"]).grid(row=2, column=0, sticky="w", pady=(0, 6))
        link = tk.Label(grid, text=__github__, fg=C["accent"], cursor="hand2", font=("Microsoft YaHei UI", 9, "underline"))
        link.grid(row=2, column=1, sticky="w", padx=(12, 0), pady=(0, 6))
        link.bind("<Button-1>", lambda e: self.open_github())

        ttk.Label(
            self,
            text="凭据只保存在本机；会话端只拿到别名。",
            foreground=C["dim"],
        ).pack(anchor="w", pady=(10, 0))

        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(16, 0))
        ttk.Button(bar, text="复制地址", command=self.copy_link).pack(side="left")
        ttk.Button(bar, text="打开 GitHub", style="Primary.TButton", command=self.open_github).pack(side="left", padx=(8, 0))
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
# 交互式终端窗口：真 PTY，能跑 top / vi / less
# --------------------------------------------------------------------------- #
class ShellWindow(tk.Toplevel):
    """一个真实的交互终端。按键按 xterm 语义翻译成字节发给远端 PTY。"""

    KEY_SEQ = {
        "Return": "\r",
        "KP_Enter": "\r",
        "BackSpace": "\x7f",
        "Tab": "\t",
        "ISO_Left_Tab": "\x1b[Z",
        "Escape": "\x1b",
        "Delete": "\x1b[3~",
        "Insert": "\x1b[2~",
        "Home": "\x1b[H",
        "End": "\x1b[F",
        "Prior": "\x1b[5~",
        "Next": "\x1b[6~",
        "Up": "\x1b[A",
        "Down": "\x1b[B",
        "Right": "\x1b[C",
        "Left": "\x1b[D",
        "F1": "\x1bOP",
        "F2": "\x1bOQ",
        "F3": "\x1bOR",
        "F4": "\x1bOS",
        "F5": "\x1b[15~",
        "F6": "\x1b[17~",
    }
    MAX_LINES = 5000

    def __init__(self, app: "App", alias: str) -> None:
        super().__init__(app)
        self.app = app
        self.alias = alias
        self.rt = app.rt
        self.sid: str | None = None
        self._closed = False
        self._pending = 0
        self._cols = 100
        self._rows = 30
        self._resize_job: str | None = None

        self.title(f"交互终端 — {alias}")
        self.geometry("980x620")
        self.minsize(640, 400)
        self.configure(bg=C["bg"])

        head = tk.Frame(self, bg=C["card"])
        head.pack(fill="x")
        row = tk.Frame(head, bg=C["card"])
        row.pack(fill="x", padx=12, pady=8)
        tk.Label(row, text=alias, bg=C["card"], fg=C["txt"], font=FONT_BOLD).pack(side="left")
        self.lbl_state = tk.Label(row, text="正在连接…", bg=C["chip_warn"], fg=C["warn"],
                                  font=FONT_SM, padx=9, pady=2)
        self.lbl_state.pack(side="left", padx=(10, 0))
        self.lbl_size = tk.Label(row, text="", bg=C["card"], fg=C["dim"], font=FONT_SM)
        self.lbl_size.pack(side="left", padx=(10, 0))

        ttk.Button(row, text="清屏", style="Ghost.TButton", command=self.clear).pack(side="right")
        ttk.Button(row, text="结束会话", style="Ghost.TButton", command=self.close_session).pack(
            side="right", padx=(0, 6)
        )
        tk.Frame(head, height=1, bg=C["line"]).pack(fill="x")

        wrap = tk.Frame(self, bg=C["term_bg"])
        wrap.pack(fill="both", expand=True, padx=12, pady=12)
        self.text = tk.Text(
            wrap, wrap="char", font=MONO, relief="flat", bd=0, undo=False,
            bg=C["term_bg"], fg=C["term_fg"], insertbackground=C["term_fg"],
            padx=10, pady=8, highlightthickness=0,
        )
        self.text.pack(fill="both", expand=True)
        self.text.tag_configure("sys", foreground="#7d8590")
        self.text.configure(state="disabled")

        tip = tk.Label(
            self, text="按键直接发到远端 · Ctrl+Shift+C 复制选中 · Ctrl+V 粘贴 · Ctrl+C 发送中断",
            bg=C["card"], fg=C["dim"], font=FONT_SM, anchor="w", padx=12, pady=5,
        )
        tip.pack(fill="x", side="bottom")

        self.text.bind("<Key>", self._on_key)
        self.text.bind("<Control-Shift-C>", self._copy)
        self.text.bind("<Control-Shift-c>", self._copy)
        self.text.bind("<Control-v>", self._paste)
        self.text.bind("<Control-V>", self._paste)
        self.text.bind("<Button-3>", self._menu)
        self.bind("<Configure>", self._on_configure)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(120, self._open)
        self.after(160, self._poll)

    # ------------------------------------------------------------ 生命周期
    def _open(self) -> None:
        self.app.runner.submit(
            self.rt.pool.open_shell(self.alias, cols=self._cols, rows=self._rows),
            on_done=self._opened,
            on_error=self._open_failed,
        )

    def _opened(self, payload: dict) -> None:
        self.sid = payload["session_id"]
        self._cols, self._rows = (int(x) for x in payload["info"]["size"].split("x"))
        self.lbl_state.configure(text=f"● 已连接 {self.sid}", bg=C["chip_on"], fg=C["ok"])
        self.lbl_size.configure(text=f"{self._cols}x{self._rows}")
        self._write(payload.get("plain") or payload.get("output") or "")
        self.text.focus_set()
        self._apply_geometry()

    def _open_failed(self, exc: BaseException) -> None:
        self.lbl_state.configure(text="● 连接失败", bg="#fde8e8", fg="#b91c1c")
        self._write(f"\n[打开失败] {type(exc).__name__}: {exc}\n", "sys")

    def close_session(self) -> None:
        if not self.sid:
            return
        sid = self.sid
        self.sid = None
        self.app.runner.submit(self.rt.pool.shell_close(sid))
        self.lbl_state.configure(text="● 已结束", bg=C["chip_off"], fg=C["dim"])
        self._write("\n[会话已结束]\n", "sys")

    def on_close(self) -> None:
        self._closed = True
        self.close_session()
        self.destroy()

    # ---------------------------------------------------------------- 输出
    def _write(self, text: str, tag: str | None = None) -> None:
        if not text:
            return
        self.text.configure(state="normal")
        self.text.insert("end", text, tag or ())
        lines = int(self.text.index("end-1c").split(".")[0])
        if lines > self.MAX_LINES:
            self.text.delete("1.0", f"{lines - self.MAX_LINES}.0")
        self.text.see("end")
        self.text.configure(state="disabled")

    def _poll(self) -> None:
        if self._closed:
            return
        if self.sid and self._pending < 2:
            self._pending += 1
            self.app.runner.submit(
                self.rt.pool.shell_read(self.sid, timeout=0.25),
                on_done=self._polled,
                on_error=self._poll_failed,
            )
        self.after(120, self._poll)

    def _polled(self, payload: dict) -> None:
        self._pending -= 1
        text = payload.get("plain") or payload.get("output") or ""
        if text:
            self._write(text)
        if payload.get("closed"):
            self.lbl_state.configure(text="● 远端已关闭", bg=C["chip_off"], fg=C["dim"])
            self.sid = None

    def _poll_failed(self, exc: BaseException) -> None:
        self._pending = max(0, self._pending - 1)
        if not self.sid:
            return
        self._write(f"\n[读取失败] {type(exc).__name__}: {exc}\n", "sys")

    def clear(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")

    # ---------------------------------------------------------------- 输入
    def _send(self, data: str) -> None:
        if not self.sid:
            return
        # 回显由远端负责（PTY 会把输入 echo 回来），这里只发
        self.app.runner.submit(self.rt.pool.shell_send(self.sid, data, wait=0.0, timeout=0.2))

    def _on_key(self, event: tk.Event) -> str:
        if not self.sid:
            return "break"

        # Ctrl+Shift+C / Ctrl+V 由单独的绑定处理
        if event.keysym in ("C", "c") and (event.state & 0x4) and (event.state & 0x1):
            return "break"

        if event.keysym == "v" and (event.state & 0x4):
            return "break"

        seq = self.KEY_SEQ.get(event.keysym)
        if seq is not None:
            self._send(seq)
            return "break"

        # Ctrl+字母 → 控制字符
        if event.state & 0x4 and len(event.keysym) == 1 and event.keysym.isalpha():
            self._send(chr(ord(event.keysym.lower()) - 96))
            return "break"

        if event.char and event.char >= " ":
            self._send(event.char)
            return "break"
        return "break"

    def _copy(self, _event: tk.Event | None = None) -> str:
        try:
            sel = self.text.get("sel.first", "sel.last")
        except tk.TclError:
            return "break"
        self.clipboard_clear()
        self.clipboard_append(sel)
        return "break"

    def _paste(self, _event: tk.Event | None = None) -> str:
        try:
            data = self.clipboard_get()
        except tk.TclError:
            return "break"
        self._send(data.replace("\n", "\r"))
        return "break"

    def _menu(self, event: tk.Event) -> None:
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="复制选中", command=self._copy)
        menu.add_command(label="粘贴", command=self._paste)
        menu.add_separator()
        menu.add_command(label="清屏", command=self.clear)
        menu.add_command(label="结束会话", command=self.close_session)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    # ---------------------------------------------------------------- 尺寸
    def _apply_geometry(self) -> None:
        """按当前字号算出行列数，让远端 PTY 和窗口一致。"""
        try:
            f = tkfont.Font(font=MONO)
            char_w = max(1, f.measure("M"))
            line_h = max(1, f.metrics("linespace"))
        except Exception:  # noqa: BLE001
            return
        w = max(1, self.text.winfo_width())
        h = max(1, self.text.winfo_height())
        cols = max(10, min(500, (w - 20) // char_w))
        rows = max(5, min(200, (h - 16) // line_h))
        if (cols, rows) == (self._cols, self._rows):
            return
        self._cols, self._rows = cols, rows
        self.lbl_size.configure(text=f"{cols}x{rows}")
        if self.sid:
            self.app.runner.submit(self.rt.pool.shell_resize(self.sid, cols, rows))

    def _on_configure(self, event: tk.Event) -> None:
        if event.widget is not self:
            return
        if self._resize_job is not None:
            try:
                self.after_cancel(self._resize_job)
            except Exception:  # noqa: BLE001
                pass
        self._resize_job = self.after(250, self._apply_geometry)


# --------------------------------------------------------------------------- #
# 检查更新 / 升级
# --------------------------------------------------------------------------- #
def build_http_config(port: str, token: str) -> str:
    """「MCP 配置」里的 HTTP 那段 JSON（纯函数，方便测试，不依赖 Tk）。"""
    port = (port or "8765").strip() or "8765"
    token = (token or "").strip()
    entry: dict[str, Any] = {"type": "streamable-http", "url": f"http://127.0.0.1:{port}/mcp"}
    if token:
        entry["headers"] = {"Authorization": f"Bearer {token}"}
    return json.dumps({"mcpServers": {"ssh": entry}}, indent=2, ensure_ascii=False)


def build_stdio_config(cfg_path: Path | str, python: str | None = None) -> str:
    """stdio 那段 JSON。pythonw 下必须换成 python.exe——stdio 要往 stdout 写协议。"""
    exe = Path(python or sys.executable)
    if exe.name.lower() == "pythonw.exe":
        exe = exe.with_name("python.exe")
    entry = {
        "command": str(exe),
        "args": ["-m", "ssh_mcp", "--transport", "stdio", "--config", str(cfg_path)],
    }
    return json.dumps({"mcpServers": {"ssh": entry}}, indent=2, ensure_ascii=False)


def _plain_markdown(text: str) -> str:
    """把 release 正文里的 markdown 简化成能在 Text 里直接读的样子（不做完整渲染）。"""
    out: list[str] = []
    for line in (text or "").splitlines():
        s = line.rstrip()
        if s.strip().startswith("```"):
            continue
        if s.strip().startswith("|") and set(s.strip()) <= set("|-: "):
            continue  # 表格分隔行
        s = s.replace("**", "").replace("`", "")
        if s.lstrip().startswith("#"):
            s = s.lstrip("#").strip()
        if s.strip().startswith("|"):
            cells = [c.strip() for c in s.strip().strip("|").split("|")]
            s = "  |  ".join(c for c in cells if c)
        out.append(s)
    # 压缩多余空行
    cleaned: list[str] = []
    for line in out:
        if not line.strip() and cleaned and not cleaned[-1].strip():
            continue
        cleaned.append(line)
    return "\n".join(cleaned).strip()


class UpdateDialog(tk.Toplevel):
    def __init__(self, app: "App", result: dict) -> None:
        super().__init__(app)
        self.app = app
        self.result = result
        info = result.get("info") or {}
        self.info = info

        self.title("发现新版本")
        self.transient(app)
        self.resizable(True, True)
        self.configure(bg=C["card"], padx=18, pady=16)

        tk.Label(self, text=f"ssh-mcp {info.get('version', '?')}", bg=C["card"], fg=C["txt"],
                 font=FONT_BRAND).pack(anchor="w")
        tk.Label(
            self,
            text=f"当前 {result.get('current')}  ·  最新 {result.get('latest')}  ·  "
                 f"发布 {(info.get('published_at') or '')[:10]}",
            bg=C["card"], fg=C["dim"], font=FONT_SM,
        ).pack(anchor="w", pady=(2, 10))

        tk.Label(self, text="更新说明", bg=C["card"], fg=C["dim"], font=FONT_SM).pack(anchor="w")
        notes = tk.Text(self, height=12, width=78, wrap="word", font=FONT, relief="flat",
                        bg=C["bg"], fg=C["txt"], padx=10, pady=8, highlightthickness=1,
                        highlightbackground=C["line"])
        notes.insert("1.0", _plain_markdown(info.get("notes") or "") or "（这个 release 没有写说明）")
        notes.configure(state="disabled")
        notes.pack(fill="both", expand=True, pady=(4, 10))

        hint = ""
        if updater.is_editable_install():
            hint = "当前是源码可编辑安装，建议 git pull 后在 .venv 里 pip install -e . 手动更新"
        elif not info.get("has_wheel"):
            hint = "这个 release 没有附带 wheel，只能打开发布页手动下载"
        if hint:
            tk.Label(self, text=hint, bg=C["card"], fg=C["warn"], font=FONT_SM, wraplength=620,
                     justify="left").pack(anchor="w", pady=(0, 8))

        bar = tk.Frame(self, bg=C["card"])
        bar.pack(fill="x")
        ttk.Button(bar, text="打开发布页", style="Ghost.TButton", command=self.open_page).pack(side="left")
        ttk.Button(bar, text="稍后", style="Ghost.TButton", command=self.destroy).pack(side="right")
        self.btn_install = ttk.Button(
            bar, text="立即更新", style="Primary.TButton",
            command=self.install,
            state=("normal" if info.get("has_wheel") and not updater.is_editable_install() else "disabled"),
        )
        self.btn_install.pack(side="right", padx=(0, 8))

        self.bind("<Escape>", lambda e: self.destroy())
        self.geometry("680x520")
        self.update_idletasks()
        px = app.winfo_rootx() + (app.winfo_width() - 680) // 2
        py = app.winfo_rooty() + (app.winfo_height() - 520) // 2
        self.geometry(f"680x520+{max(px, 0)}+{max(py, 0)}")

    def open_page(self) -> None:
        try:
            import webbrowser

            webbrowser.open(self.info.get("url") or updater.RELEASES_PAGE)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("打开失败", str(exc), parent=self)

    def install(self) -> None:
        self.btn_install.configure(state="disabled", text="更新中…")
        self.app.begin_update(self.info, on_finish=lambda ok: self._finish(ok))

    def _finish(self, ok: bool) -> None:
        if not self.winfo_exists():
            return
        self.btn_install.configure(text=("已安装" if ok else "重试"), state=("disabled" if ok else "normal"))
        if ok:
            self.after(600, self.destroy)


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
        self._checking_update = False
        self._update_result: dict | None = None
        self._last_pct = 0
        self._last_update_check = 0.0
        self._update_dialog: UpdateDialog | None = None

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
        # 启动后静默查一次更新（有网就查，失败只在终端留一行，不打扰）
        self.after(4000, lambda: self.check_updates(silent=True))
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        # tkinter 默认把回调异常打到 stderr，pythonw 下等于丢进黑洞
        self.report_callback_exception = self._on_tk_error
        if created:
            self._term(f"# 已创建新配置：{config_path}", "dim")
            self._term("# 点「新增主机」开始；凭据只保存在本机，界面不会回显密码。", "dim")
        else:
            self._term("# 选择左侧主机后，在下方输入命令、回车执行（需要 root 时勾选 sudo）", "dim")

    # ----------------------------------------------------------------- style
    def _init_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")  # clam 才能完全自定义配色；vista 会忽略大部分颜色
        except tk.TclError:
            pass

        self.option_add("*Font", FONT)
        style.configure(".", font=FONT, background=C["bg"], foreground=C["txt"])

        # ---- 按钮 ----------------------------------------------------
        style.configure(
            "TButton",
            font=FONT,
            padding=(12, 6),
            background=C["card"],
            foreground=C["txt"],
            bordercolor=C["line"],
            lightcolor=C["card"],
            darkcolor=C["card"],
            focuscolor=C["card"],
            relief="flat",
        )
        style.map(
            "TButton",
            background=[("pressed", C["line"]), ("active", C["hover"])],
            bordercolor=[("active", C["line2"])],
        )
        style.configure(
            "Primary.TButton",
            background=C["accent"],
            foreground="#ffffff",
            bordercolor=C["accent"],
            lightcolor=C["accent"],
            darkcolor=C["accent"],
            focuscolor=C["accent"],
            font=FONT_BOLD,
        )
        style.map(
            "Primary.TButton",
            background=[("disabled", C["line2"]), ("pressed", C["accent_dark"]), ("active", C["accent_dark"])],
            foreground=[("disabled", C["dim"])],
            bordercolor=[("disabled", C["line2"]), ("active", C["accent_dark"])],
            lightcolor=[("active", C["accent_dark"])],
            darkcolor=[("active", C["accent_dark"])],
        )
        style.configure("Ghost.TButton", background=C["bg"], bordercolor=C["bg"])
        style.map("Ghost.TButton", background=[("active", C["hover"])], bordercolor=[("active", C["line"])])

        style.configure("TMenubutton", font=FONT, padding=(12, 6), background=C["card"],
                        bordercolor=C["line"], relief="flat", arrowcolor=C["dim"])
        style.map("TMenubutton", background=[("active", C["hover"])])

        # ---- 输入 ----------------------------------------------------
        style.configure(
            "TEntry",
            fieldbackground="#ffffff",
            bordercolor=C["line"],
            lightcolor=C["line"],
            darkcolor=C["line"],
            padding=(6, 5),
            insertcolor=C["txt"],
        )
        style.map("TEntry", bordercolor=[("focus", C["accent"])], lightcolor=[("focus", C["accent"])])
        style.configure(
            "TCombobox",
            fieldbackground="#ffffff",
            background="#ffffff",
            bordercolor=C["line"],
            lightcolor=C["line"],
            darkcolor=C["line"],
            padding=(6, 4),
            arrowcolor=C["dim"],
        )
        style.map("TCombobox", fieldbackground=[("readonly", "#ffffff")], bordercolor=[("focus", C["accent"])])
        style.configure("TCheckbutton", background=C["card"], foreground=C["txt"], focuscolor=C["card"])
        style.map("TCheckbutton", background=[("active", C["card"])])

        # ---- 表格 ----------------------------------------------------
        style.configure(
            "Treeview",
            background=C["card"],
            fieldbackground=C["card"],
            foreground=C["txt"],
            rowheight=30,
            borderwidth=0,
            font=FONT,
        )
        style.map(
            "Treeview",
            background=[("selected", C["sel"])],
            foreground=[("selected", C["txt"])],
        )
        style.configure(
            "Treeview.Heading",
            background=C["head"],
            foreground=C["dim"],
            font=("Microsoft YaHei UI", 9, "bold"),
            relief="flat",
            borderwidth=0,
            padding=(8, 9),
        )
        style.map("Treeview.Heading", background=[("active", C["hover"])])
        # 去掉 treeview 外边那圈 3D 边框
        style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])

        style.configure("TSeparator", background=C["line"])
        style.configure("TPanedwindow", background=C["bg"])
        style.configure("Sash", sashthickness=8, gripcount=0, background=C["bg"])

        # ---- 容器（对话框里的分页与分组框）------------------------------
        style.configure("TNotebook", background=C["bg"], borderwidth=0, tabmargins=(0, 4, 0, 0))
        style.configure(
            "TNotebook.Tab",
            padding=(16, 7),
            font=FONT,
            background=C["bg"],
            foreground=C["dim"],
            borderwidth=0,
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", C["card"])],
            foreground=[("selected", C["accent"])],
            font=[("selected", FONT_BOLD)],
        )
        style.configure(
            "TLabelframe",
            background=C["bg"],
            bordercolor=C["line"],
            lightcolor=C["line"],
            darkcolor=C["line"],
            relief="solid",
            borderwidth=1,
        )
        style.configure(
            "TLabelframe.Label", background=C["bg"], foreground=C["dim"], font=FONT_SM
        )

        # ---- 文字 ----------------------------------------------------
        style.configure("Hint.TLabel", background=C["card"], foreground=C["dim"])
        style.configure("Status.TLabel", background=C["card"], foreground=C["dim"], padding=(0, 0))
        style.configure("CardTitle.TLabel", background=C["card"], foreground=C["txt"], font=FONT_BOLD)
        style.configure("CardHint.TLabel", background=C["card"], foreground=C["dim"])
        style.configure("Chip.TLabel", background=C["accent_soft"], foreground=C["accent_dark"], padding=(7, 1))

    # -------------------------------------------------------------------- ui
    def _card(self, parent: tk.Widget, title: str = "", hint: str = "") -> tuple[tk.Frame, tk.Frame, tk.Frame]:
        """白底细边框的卡片，返回 (卡片, 内容区, 标题栏)。"""
        card = tk.Frame(parent, bg=C["card"], highlightbackground=C["line"], highlightthickness=1, bd=0)
        head = tk.Frame(card, bg=C["card"])
        head.pack(fill="x", padx=14, pady=(11, 8))
        if title:
            tk.Label(head, text=title, bg=C["card"], fg=C["txt"], font=FONT_BOLD).pack(side="left")
        if hint:
            tk.Label(head, text=hint, bg=C["card"], fg=C["dim"], font=FONT_SM).pack(side="left", padx=(8, 0))
        tk.Frame(card, height=1, bg=C["line"]).pack(fill="x")
        body = tk.Frame(card, bg=C["card"])
        body.pack(fill="both", expand=True, padx=14, pady=12)
        return card, body, head

    def _build_ui(self) -> None:
        # ============================================================ 顶部品牌区
        head = tk.Frame(self, bg=C["card"])
        head.pack(fill="x")
        row = tk.Frame(head, bg=C["card"])
        row.pack(fill="x", padx=16, pady=(11, 10))

        brand = tk.Frame(row, bg=C["card"])
        brand.pack(side="left")
        tk.Label(brand, text="ssh-mcp", bg=C["card"], fg=C["txt"], font=FONT_BRAND).pack(side="left")
        tk.Label(
            brand, text=f"v{__version__}", bg=C["accent_soft"], fg=C["accent_dark"], font=FONT_SM, padx=7, pady=1
        ).pack(side="left", padx=(8, 10))
        tk.Label(
            brand, text="凭据留在服务端的 MCP 中间层", bg=C["card"], fg=C["dim"], font=FONT_SM
        ).pack(side="left")

        # 服务控制（右侧）。注意 pack(side="right") 是「先 pack 的靠最右」，
        # 所以这里按视觉上从右往左的顺序摆放。
        svc = tk.Frame(row, bg=C["card"])
        svc.pack(side="right")

        self.lbl_service = tk.Label(
            svc, text="● 未启动", bg=C["chip_off"], fg=C["dim"], font=FONT_SM, padx=9, pady=3
        )
        self.lbl_service.pack(side="right")

        self.btn_service = ttk.Button(svc, text="启动 MCP 服务", style="Primary.TButton", command=self.toggle_service)
        self.btn_service.pack(side="right", padx=(10, 8))

        self.var_token = tk.StringVar(value="")
        ttk.Entry(svc, textvariable=self.var_token, width=13, show="•").pack(side="right", padx=(0, 10))
        tk.Label(svc, text="Token", bg=C["card"], fg=C["dim"], font=FONT_SM).pack(side="right", padx=(0, 5))

        self.var_port = tk.StringVar(value="8765")
        ttk.Entry(svc, textvariable=self.var_port, width=6, justify="center").pack(side="right")
        tk.Label(svc, text="端口", bg=C["card"], fg=C["dim"], font=FONT_SM).pack(side="right", padx=(0, 5))

        tk.Frame(self, height=1, bg=C["line"]).pack(fill="x")

        # ============================================================ 工具栏
        bar = tk.Frame(self, bg=C["bg"])
        bar.pack(fill="x", padx=14, pady=(12, 8))

        ttk.Button(bar, text="＋ 新增主机", style="Primary.TButton", command=self.on_add).pack(side="left")
        ttk.Button(bar, text="编辑", style="Ghost.TButton", command=self.on_edit).pack(side="left", padx=(10, 0))
        ttk.Button(bar, text="删除", style="Ghost.TButton", command=self.on_delete).pack(side="left", padx=(2, 0))
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=12, pady=6)
        ttk.Button(bar, text="测试连接", style="Ghost.TButton", command=self.on_test).pack(side="left")
        self.btn_shell = ttk.Button(bar, text="交互终端", style="Ghost.TButton", command=self.on_open_shell)
        self.btn_shell.pack(side="left", padx=(2, 0))
        ttk.Button(bar, text="刷新", style="Ghost.TButton", command=self._refresh_hosts).pack(side="left", padx=(2, 0))
        ttk.Button(bar, text="MCP 配置", style="Ghost.TButton", command=self.show_mcp_config).pack(side="left", padx=(2, 0))

        more = ttk.Menubutton(bar, text="更多 ▾", style="Ghost.TButton")
        self.more_btn = more
        menu = tk.Menu(more, tearoff=0)
        self.more_menu = menu
        menu.add_command(label="打开 servers.yaml", command=self.on_open_config)
        menu.add_command(label="打开凭据目录", command=self.on_open_creds)
        menu.add_command(label="打开桌面端日志", command=self.on_open_log)
        menu.add_command(label="打开服务日志", command=self.on_open_service_log)
        menu.add_separator()
        menu.add_command(label="复制当前配置路径", command=self.on_copy_config_path)
        menu.add_separator()
        menu.add_command(label="检查更新…", command=self.show_update_dialog)
        self.update_menu_index = menu.index("end")  # 有新版本时改这一项的文案
        menu.add_separator()
        menu.add_command(label=f"关于 ssh-mcp  v{__version__}", command=self.show_about)
        more["menu"] = menu
        more.pack(side="left", padx=(6, 0))

        # ============================================================ 主体
        body = tk.Frame(self, bg=C["bg"])
        body.pack(fill="both", expand=True, padx=14, pady=(0, 6))

        pw = ttk.PanedWindow(body, orient="horizontal")
        pw.pack(fill="both", expand=True)

        # ---------------- 左：主机列表 ----------------
        left_wrap = tk.Frame(pw, bg=C["bg"])
        card, inner, chead = self._card(left_wrap, "主机列表")
        card.pack(fill="both", expand=True)
        inner.pack_configure(padx=0, pady=(0, 0))
        self.lbl_host_count = tk.Label(chead, text="0 台", bg=C["card"], fg=C["dim"], font=FONT_SM)
        self.lbl_host_count.pack(side="right")

        tree_wrap = tk.Frame(inner, bg=C["card"])
        tree_wrap.pack(fill="both", expand=True, pady=(2, 10), padx=(12, 12))
        cols = ("alias", "target", "user", "auth", "tags", "state")
        self.tree = ttk.Treeview(tree_wrap, columns=cols, show="headings", selectmode="browse")
        for key, text, width, anchor in (
            ("alias", "别名", 140, "w"),
            ("target", "目标", 160, "w"),
            ("user", "用户", 95, "w"),
            ("auth", "认证", 72, "w"),
            ("tags", "标签", 130, "w"),
            ("state", "状态", 84, "w"),
        ):
            self.tree.heading(key, text=text, anchor=anchor)
            self.tree.column(key, width=width, anchor=anchor, stretch=(key in ("target", "tags")))
        # 斑马纹 + 状态色（tk 的 tag 谁先定义谁优先，所以按「状态_斑马」组合定义）
        for i, bg in enumerate((C["card"], C["stripe"])):
            self.tree.tag_configure(f"on_{i}", background=bg, foreground=C["ok"])
            self.tree.tag_configure(f"off_{i}", background=bg, foreground=C["dim"])
        self.tree.tag_configure("state_on", foreground=C["ok"])
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._on_select())
        self.tree.bind("<Double-1>", lambda e: self.on_edit())
        sb = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        pw.add(left_wrap, weight=3)

        # ---------------- 右：详情 + 终端 ----------------
        right = tk.Frame(pw, bg=C["bg"])
        pw.add(right, weight=4)

        dcard, dinner, _dhead = self._card(right, "主机详情")
        dcard.pack(fill="x", padx=(10, 0))
        dinner.pack_configure(padx=14, pady=(10, 12))
        self.detail = tk.Text(
            dinner, height=10, wrap="word", font=FONT, relief="flat", bd=0,
            bg=C["card"], fg=C["txt"], padx=0, pady=0, cursor="arrow",
            highlightthickness=0,
        )
        self.detail.pack(fill="x")
        self.detail.tag_configure("k", foreground=C["dim"])
        self.detail.tag_configure("v", foreground=C["txt"])
        self.detail.tag_configure("alias", foreground=C["txt"], font=("Microsoft YaHei UI", 11, "bold"))
        self.detail.tag_configure("sub", foreground=C["dim"])
        self.detail.configure(state="disabled")

        tcard, tinner, thead = self._card(right, "命令终端")
        tcard.pack(fill="both", expand=True, padx=(10, 0), pady=(10, 0))
        tinner.pack_configure(padx=0, pady=0)
        self.lbl_term_target = tk.Label(
            thead, text="未选择主机", bg=C["accent_soft"], fg=C["accent_dark"], font=FONT_SM, padx=8, pady=1
        )
        self.lbl_term_target.pack(side="right")

        self.terminal = tk.Text(
            tinner, height=8, wrap="word", font=MONO, relief="flat", bd=0,
            bg=C["term_bg"], fg=C["term_fg"], padx=12, pady=10,
            insertbackground=C["term_fg"], highlightthickness=0,
        )
        self.terminal.pack(fill="both", expand=True, pady=(10, 0), padx=12)
        self.terminal.tag_configure("cmd", foreground="#7ee787")
        self.terminal.tag_configure("err", foreground="#ff7b72")
        self.terminal.tag_configure("dim", foreground="#7d8590")
        self.terminal.tag_configure("out", foreground="#c9d1d9")
        self.terminal.configure(state="disabled")

        cmdbar = tk.Frame(tinner, bg=C["card"])
        cmdbar.pack(fill="x", padx=12, pady=10)
        self.var_sudo_exec = tk.BooleanVar(value=False)
        ttk.Checkbutton(cmdbar, text="sudo", variable=self.var_sudo_exec).pack(side="left")
        self.var_cmd = tk.StringVar()
        self.ent_cmd = ttk.Entry(cmdbar, textvariable=self.var_cmd, font=MONO)
        self.ent_cmd.pack(side="left", fill="x", expand=True, padx=(10, 8))
        self.ent_cmd.bind("<Return>", lambda e: self.on_run())
        self.ent_cmd.configure(state="disabled")
        self.btn_run = ttk.Button(cmdbar, text="执行", style="Primary.TButton", width=7,
                                  command=self.on_run, state="disabled")
        self.btn_run.pack(side="left")

        # ============================================================ 状态栏
        tk.Frame(self, height=1, bg=C["line"]).pack(fill="x", side="bottom")
        sbar = tk.Frame(self, bg=C["card"])
        sbar.pack(fill="x", side="bottom")

        self.credit = tk.Label(
            sbar,
            text=f"{__author__}  ·  {__github__.replace('https://', '')}",
            bg=C["card"], fg=C["accent"], font=FONT_SM, cursor="hand2", padx=14, pady=6,
        )
        self.credit.pack(side="right")
        self.credit.bind("<Button-1>", lambda e: self.show_about())
        self.credit.bind("<Enter>", lambda e: self.credit.configure(font=(FONT_SM[0], FONT_SM[1], "underline")))
        self.credit.bind("<Leave>", lambda e: self.credit.configure(font=FONT_SM))

        self.status = tk.Label(sbar, text="", bg=C["card"], fg=C["dim"], font=FONT_SM,
                               padx=14, pady=6, anchor="w")
        self.status.pack(side="left", fill="x", expand=True)

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
        for i, (alias, host) in enumerate(sorted(cfg.hosts.items())):
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
                    "● 在线" if connected else "○ 未连接",
                ),
                tags=((f"on_{i % 2}" if connected else f"off_{i % 2}"),),
            )
        if hasattr(self, "lbl_host_count"):
            online = sum(1 for c in conns.values() if c.get("connected"))
            self.lbl_host_count.configure(text=f"{len(cfg.hosts)} 台 · 在线 {online}")
        if keep and self.tree.exists(keep):
            self.tree.selection_set(keep)
            self._on_select()
        else:
            self._show_detail_empty()
        self._update_status()

    def _update_status(self) -> None:
        rt = self.rt
        online = sum(1 for c in rt.pool.status() if c["connected"])
        audit = "开" if rt.cfg.audit.enabled else "关"
        sudo = "开" if rt.cfg.policy.allow_sudo else "关"
        deny = len(rt.cfg.policy.deny_commands)
        self.status.configure(
            text=(
                f"{self.cfg_path}   ·   主机 {len(rt.cfg.hosts)} / 在线 {online}"
                f"   ·   审计 {audit}   ·   sudo {sudo}   ·   黑名单 {deny} 条"
            )
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
        if hasattr(self, "lbl_term_target"):
            self.lbl_term_target.configure(text=f"目标：{alias}")
        self._show_detail(host)

    def _show_detail(self, host: Host) -> None:
        d = host.describe()
        auth = d["auth"]
        lines: list[tuple[str, str]] = [
            ("__alias__", d["alias"]),
            ("__sub__", d["description"] or "（无说明）"),
            ("地址", f"{d['host']}:{d['port']}"),
            ("用户", d["user"] or "-"),
            ("远端 shell", d["shell_kind"] + (f" / {d['posix_shell']}" if d["shell_kind"] == "posix" else "")),
            (
                "认证",
                AUTH_LABEL.get(auth["method"], auth["method"])
                + (f"    私钥 {auth['key_file']}" if auth.get("key_file") else "")
                + (f"    密码来源 {auth['password_source']}" if auth.get("password_source") else ""),
            ),
        ]
        if d.get("sudo"):
            sudo = d["sudo"]
            lines.append(
                ("sudo", f"已启用    用户 {sudo.get('user') or 'root'}    密码来源 {sudo.get('password_source') or '无(NOPASSWD)'}")
            )
        lines.append(("标签", ", ".join(d["tags"]) or "-"))
        lines.append(("超时 / 并发", f"{d['command_timeout']}s / {d['max_concurrency']}"))
        lines.append(("host key", d["host_key_policy"] + (f"    跳板 {d['proxy_jump']}" if d.get("proxy_jump") else "")))

        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        for key, value in lines:
            if key == "__alias__":
                self.detail.insert("end", value + "\n", "alias")
            elif key == "__sub__":
                self.detail.insert("end", value + "\n\n", "sub")
            else:
                self.detail.insert("end", f"{key}  ", "k")
                self.detail.insert("end", value + "\n", "v")
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
                if self.service.status()["running"]:
                    self._term("# 常驻 MCP 服务会在下次工具调用时自动读到新配置，无需重启", "dim")
            self._update_status()

        self.runner.submit(job(), on_done=done)

    # --------------------------------------------------------------- service
    def build_http_config(self) -> str:
        return build_http_config(self.var_port.get().strip(), self.var_token.get().strip())

    def build_stdio_config(self) -> str:
        return build_stdio_config(self.cfg_path)

    def on_open_shell(self) -> None:
        alias = self._require_selection()
        if not alias:
            return
        if not self.rt.cfg.policy.allow_shell:
            messagebox.showinfo("已禁用", "policy.allow_shell=false，交互式终端被策略关闭", parent=self)
            return
        ShellWindow(self, alias)
        self._term(f"# 已打开交互终端：{alias}（可跑 top / vi / less）", "dim")

    def show_mcp_config(self) -> None:
        dlg = McpConfigDialog(self)
        self.wait_window(dlg)

    def show_about(self) -> None:
        dlg = AboutDialog(self)
        self.wait_window(dlg)

    # ------------------------------------------------------------- 检查更新
    AUTO_CHECK_INTERVAL = 6 * 3600  # 静默自检的间隔，避免刷 GitHub API 限额

    def check_updates(self, silent: bool = True) -> None:
        if self._checking_update:
            return
        if silent and time.time() - self._last_update_check < self.AUTO_CHECK_INTERVAL:
            return
        self._checking_update = True
        self._last_update_check = time.time()
        self._save_ui_state()
        if not silent:
            self._term("# 正在检查更新 …", "dim")

        def work() -> None:
            try:
                res = updater.check(__version__)
            except Exception as exc:  # noqa: BLE001 - 兜底，绝不让后台线程炸掉
                res = {"ok": False, "current": __version__, "error": f"{type(exc).__name__}: {exc}"}
            try:
                self.after(0, lambda: self._update_checked(res, silent))
            except Exception:  # noqa: BLE001 - 窗口已经关了
                pass

        threading.Thread(target=work, daemon=True, name="ssh-mcp-update").start()

    def _update_checked(self, res: dict, silent: bool) -> None:
        self._checking_update = False
        if not res.get("ok"):
            self._term(f"# 检查更新失败：{res.get('error')}", "dim")
            if not silent:
                messagebox.showwarning(
                    "检查更新失败",
                    f"{res.get('error')}\n\n可以打开发布页手动查看：\n{updater.RELEASES_PAGE}",
                    parent=self,
                )
            return

        self._update_result = res
        if res.get("has_update"):
            info = res["info"]
            self._set_update_badge(True, info)
            self._term(f"# 发现新版本 {info['version']}（当前 {res['current']}）→ {info['url']}", "cmd")
            self._open_update_dialog(res)
        else:
            self._set_update_badge(False, None)
            self._term(f"# 已是最新版本 {res['current']}", "dim")
            if not silent:
                messagebox.showinfo("检查更新", f"已是最新版本（{res['current']}）", parent=self)

    def _set_update_badge(self, has_update: bool, info: dict | None) -> None:
        if has_update and info:
            self.more_btn.configure(text=f"更多 ▾  ⬆{info['version']}")
            self.more_menu.entryconfigure(
                self.update_menu_index, label=f"有新版本 {info['version']}，查看"
            )
        else:
            self.more_btn.configure(text="更多 ▾")
            self.more_menu.entryconfigure(self.update_menu_index, label="检查更新…")

    def _open_update_dialog(self, res: dict) -> None:
        """同一时间只留一个更新对话框（自动检查和手动检查可能撞车）。"""
        existing = getattr(self, "_update_dialog", None)
        if existing is not None:
            try:
                if existing.winfo_exists():
                    existing.lift()
                    existing.focus_set()
                    return
            except tk.TclError:
                pass
        dlg = UpdateDialog(self, res)
        self._update_dialog = dlg
        self.wait_window(dlg)
        self._update_dialog = None

    def show_update_dialog(self) -> None:
        if self._update_result and self._update_result.get("ok"):
            res = self._update_result
            if res.get("has_update"):
                self._open_update_dialog(res)
                return
            messagebox.showinfo("检查更新", f"已是最新版本（{res['current']}）", parent=self)
            return
        self.check_updates(silent=False)

    def begin_update(self, info: dict, on_finish: Callable[[bool], None] | None = None) -> None:
        """后台下载 wheel + pip 升级，完成后提示重启。"""
        if updater.is_editable_install():
            messagebox.showinfo(
                "当前是源码安装",
                "这个环境是 `pip install -e .` 装的，别用 wheel 覆盖。\n"
                "更新方式：git pull 后在 .venv 里重新 pip install -e .",
                parent=self,
            )
            if on_finish:
                on_finish(False)
            return

        self._term(f"# 开始下载 {info.get('wheel_name') or 'wheel'} …", "dim")

        def work() -> None:
            ok = False
            try:
                release = updater.ReleaseInfo(
                    tag=f"v{info['version']}",
                    version=info["version"],
                    url=info.get("url", ""),
                    wheel_name=info.get("wheel_name"),
                    wheel_url=info.get("wheel_url") or self._wheel_url_from(info),
                    wheel_size=int(info.get("wheel_size") or 0),
                )
                if not release.wheel_url:
                    raise ValueError("拿不到 wheel 下载地址")
                wheel = updater.download_wheel(
                    release,
                    progress=lambda done, total: self._update_progress(done, total),
                )
                self._term(f"# 已下载 {wheel.name}（{wheel.stat().st_size} 字节），开始安装", "dim")
                res = updater.install_wheel(wheel)
                ok = bool(res.get("ok"))
                tail = (res.get("output") or res.get("error") or "").strip().splitlines()[-6:]
                for line in tail:
                    self._term("  " + line, "dim" if ok else "err")
            except Exception as exc:  # noqa: BLE001
                self._term(f"[更新失败] {type(exc).__name__}: {exc}", "err")
            self.after(0, lambda: self._update_finished(ok, info, on_finish))

        threading.Thread(target=work, daemon=True, name="ssh-mcp-upgrade").start()

    @staticmethod
    def _wheel_url_from(info: dict) -> str | None:
        name = info.get("wheel_name")
        if not name:
            return None
        return f"{__github__}/releases/download/v{info['version']}/{name}"

    def _update_progress(self, done: int, total: int) -> None:
        if total <= 0:
            return
        pct = int(done * 100 / total)
        step = 25
        if pct // step != self._last_pct // step:  # 每 25% 打一行，别刷屏
            self._last_pct = pct
            self._term(f"# 下载进度 {pct}%（{done}/{total} 字节）", "dim")

    def _update_finished(self, ok: bool, info: dict, on_finish: Callable[[bool], None] | None) -> None:
        if on_finish:
            on_finish(ok)
        if not ok:
            messagebox.showerror(
                "更新失败",
                f"安装 {info.get('version')} 没成功，详情见下方终端面板。\n"
                f"也可以从发布页手动下载：\n{info.get('url')}",
                parent=self,
            )
            return
        self._term(f"# 已安装 {info.get('version')}，重启后生效", "cmd")
        if messagebox.askyesno(
            "更新完成",
            f"{info.get('version')} 已经装好，重启程序后生效。\n\n现在重启吗？",
            parent=self,
        ):
            self.restart_app()

    def restart_app(self) -> None:
        exe = Path(sys.executable)
        python = exe.with_name("python.exe") if exe.name.lower() == "pythonw.exe" else exe
        try:
            subprocess.Popen(
                [str(python), "-m", "ssh_mcp.desktop", "--config", str(self.cfg_path)],
                cwd=str(Path.cwd()),
                creationflags=(subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
                if os.name == "nt"
                else 0,
            )
        except Exception as exc:  # noqa: BLE001
            self.show_error(exc)
            return
        self.after(200, self.on_close)

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
            if data.get("last_update_check"):
                try:
                    self._last_update_check = float(data["last_update_check"])
                except (TypeError, ValueError):
                    pass
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
            payload = {
                "port": self.var_port.get().strip(),
                "token": self.var_token.get().strip(),
                "last_update_check": self._last_update_check,
            }
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
        self.lbl_service.configure(text=f"● 启动中 :{port}", foreground=C["warn"], bg=C["chip_warn"])
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
            self.lbl_service.configure(
                text=f"● 运行中 :{st['port']}  pid {st['pid']}", foreground=C["ok"], bg=C["chip_on"]
            )
            self.btn_service.configure(text="停止 MCP 服务")
        elif st["running"]:
            self.lbl_service.configure(
                text=f"● 运行中（外部）:{st['port']}", foreground=C["ok"], bg=C["chip_on"]
            )
            self.btn_service.configure(text="启动 MCP 服务")
        else:
            self.lbl_service.configure(text="● 未启动", foreground=C["dim"], bg=C["chip_off"])
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

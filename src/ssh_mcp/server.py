"""MCP 工具层：会话端只能看到「别名」，看不到任何主机地址之外的凭据。"""

from __future__ import annotations

import platform
import threading
import time
from pathlib import Path
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from .audit import AuditLog
from .config import ConfigError, ServerConfig, load_config
from .pool import SSHPool

INSTRUCTIONS = """\
你通过本服务操作远程 Linux/Windows 主机。**本服务已经内置了 SSH 连接与凭据**：
你只需要使用 alias（别名），不要、也无法获得用户名 / 密码 / 私钥。

标准流程：
1. 先 `ssh_list_hosts` 看有哪些可用别名（含说明与标签）；不确定某台机是否通时用 `ssh_check`；
2. 用 `ssh_exec(alias, command)` 在目标机执行命令，返回值里带 exit_code / stdout / stderr；
3. 一次要在多台机跑同一条命令时用 `ssh_exec_batch`；
4. 传/取文件用 `ssh_upload` / `ssh_download` / `ssh_write_file` / `ssh_read_file`；
5. 命令输出默认会按上限截断，返回 truncated=true 时请用更精确的命令或重定向到文件再分段读取；
6. `ssh_status` 看连接池与生效策略，`ssh_close` 回收连接，`ssh_reload_config` 热加载配置；

需要真终端（top / vi / less / tail -f 这类）时用交互式 shell：
`ssh_shell_open` → `ssh_shell_send`（回车是 \\r）→ 用完 `ssh_shell_close`。
普通命令优先用 ssh_exec，交互 shell 会一直占着一条 channel。
"""


class Runtime:
    """配置 + 审计 + 连接池的持有者。

    支持**自动热加载**：每次工具调用前比一下 servers.yaml 的 (mtime, size)，
    变了就重读并在后台关掉旧连接。这样桌面端改完主机，常驻服务不用重启也能跟上。
    """

    def __init__(self, config_path: str | None = None, *, autoload: bool = True) -> None:
        self.config_path = config_path
        self.cfg: ServerConfig = load_config(config_path)
        self.audit = AuditLog(self.cfg.audit)
        self.pool = SSHPool(self.cfg)
        self.started_at = time.time()
        self.autoload = autoload
        self.reload_count = 0
        self.last_reload_at: float | None = None
        self.last_reload_error: str | None = None
        self._path = Path(self.cfg.source) if self.cfg.source else None
        self._stamp = self._config_stamp()
        self._lock = threading.Lock()

    # ----------------------------------------------------------- 变更检测
    def _config_stamp(self) -> tuple[int, int] | None:
        if self._path is None:
            return None
        try:
            st = self._path.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def maybe_reload(self) -> bool:
        """配置文件变了就重载；返回是否真的重载了。调用成本 = 一次 stat。"""
        if not self.autoload:
            return False
        stamp = self._config_stamp()
        if stamp is None or stamp == self._stamp:
            return False
        return self.reload()

    # --------------------------------------------------------------- 重载
    def reload(self) -> bool:
        """重新读取配置。旧连接交给后台线程关闭，不阻塞当前调用。

        配置非法（比如正被半个编辑器写）时保留旧配置，并记住这次的状态，
        避免同一个坏文件被反复解析刷日志。
        """
        with self._lock:
            target = str(self._path) if self._path else self.config_path
            try:
                new_cfg = load_config(target)
            except ConfigError as exc:
                self._stamp = self._config_stamp()
                self.last_reload_error = str(exc)
                return False

            old_pool = self.pool
            self.cfg = new_cfg
            self.audit = AuditLog(new_cfg.audit)
            self.pool = SSHPool(new_cfg)
            self._path = Path(new_cfg.source) if new_cfg.source else self._path
            self._stamp = self._config_stamp()
            self.reload_count += 1
            self.last_reload_at = time.time()
            self.last_reload_error = None

        threading.Thread(
            target=self._close_pool_sync,
            args=(old_pool,),
            daemon=True,
            name="ssh-mcp-reload-close",
        ).start()
        return True

    @staticmethod
    def _close_pool_sync(pool: SSHPool) -> None:
        try:
            pool.close_sync_all()
        except Exception:  # noqa: BLE001 - 关连接失败不影响新配置生效
            pass


_rt: Runtime | None = None


def init_runtime(config_path: str | None = None, *, autoload: bool = True) -> Runtime:
    global _rt
    _rt = Runtime(config_path, autoload=autoload)
    return _rt


def runtime() -> Runtime:
    """所有工具的统一入口——自动热加载就挂在这里，12 个工具都白拿。"""
    if _rt is None:
        return init_runtime()
    _rt.maybe_reload()
    return _rt


mcp = MCPServer("ssh-mcp", instructions=INSTRUCTIONS)


def _audit(event: str, **fields: Any) -> None:
    runtime().audit.write(event, **fields)


@mcp.tool()
def ssh_list_hosts(
    tag: Annotated[str | None, Field(description="按标签过滤，例如 prod")] = None,
    keyword: Annotated[str | None, Field(description="按别名/主机/说明模糊过滤")] = None,
) -> dict[str, Any]:
    """列出所有已配置的 SSH 目标（只返回别名等元数据，不含任何凭据）。"""
    rt = runtime()
    policy = rt.cfg.policy
    items: list[dict[str, Any]] = []
    for alias, host in sorted(rt.cfg.hosts.items()):
        if tag and tag not in host.tags:
            continue
        if keyword:
            kw = keyword.lower()
            blob = f"{alias} {host.host} {host.description} {' '.join(host.tags)}".lower()
            if kw not in blob:
                continue
        allowed = not policy.allowed_hosts or alias in policy.allowed_hosts
        items.append({**host.describe(), "allowed_by_policy": allowed})
    return {
        "count": len(items),
        "hosts": items,
        "hint": "用 ssh_exec(alias, command) 执行命令；无需提供任何账号密码。",
    }


@mcp.tool()
async def ssh_exec(
    alias: Annotated[str, Field(description="ssh_list_hosts 返回的主机别名，例如 prod-web-1")],
    command: Annotated[str, Field(description="要在远端执行的命令，例如 'systemctl status nginx --no-pager'")],
    cwd: Annotated[str | None, Field(description="执行前切换到的远端目录")] = None,
    timeout: Annotated[float | None, Field(description="超时秒数，默认取该主机配置")] = None,
    sudo: Annotated[bool, Field(description="是否用 sudo 提权执行（需服务端已配置 sudo 凭据）")] = False,
    max_output_bytes: Annotated[int | None, Field(description="stdout/stderr 各自最多返回的字节数")] = None,
) -> dict[str, Any]:
    """在一台已配置的主机上执行 shell 命令，返回 exit_code / stdout / stderr。

    凭据由服务端持有，本工具不接受也不需要 host/user/password 参数。
    """
    rt = runtime()
    started = time.perf_counter()
    try:
        res = await rt.pool.exec(
            alias,
            command,
            cwd=cwd,
            timeout=timeout,
            max_output_bytes=max_output_bytes,
            sudo=sudo,
        )
        payload = res.to_dict()
        _audit(
            "exec",
            alias=alias,
            command=command,
            cwd=cwd,
            sudo=sudo,
            exit_code=payload["exit_code"],
            ok=payload["ok"],
            duration_ms=payload["duration_ms"],
            truncated=payload["truncated"],
        )
        return payload
    except Exception as exc:  # noqa: BLE001
        _audit("exec_error", alias=alias, command=command, error=f"{type(exc).__name__}: {exc}")
        return {
            "alias": alias,
            "ok": False,
            "exit_code": None,
            "error": f"{type(exc).__name__}: {exc}",
            "duration_ms": int((time.perf_counter() - started) * 1000),
        }


@mcp.tool()
async def ssh_exec_batch(
    command: Annotated[str, Field(description="在多台主机上执行的同一条命令")],
    aliases: Annotated[list[str] | None, Field(description="主机别名列表；留空表示所有（策略允许的）主机")] = None,
    cwd: Annotated[str | None, Field(description="远端工作目录")] = None,
    timeout: Annotated[float | None, Field(description="单机超时秒数")] = None,
    sudo: Annotated[bool, Field(description="是否 sudo 提权")] = False,
    max_output_bytes: Annotated[int | None, Field(description="单机 stdout/stderr 上限字节数")] = None,
) -> dict[str, Any]:
    """并发在多台主机执行同一条命令，逐台返回结果（单台失败不影响其他主机）。"""
    rt = runtime()
    targets = aliases or [a for a in sorted(rt.cfg.hosts) if not rt.cfg.policy.allowed_hosts or a in rt.cfg.policy.allowed_hosts]
    if not targets:
        return {"ok": False, "error": "没有可执行的目标主机"}
    results = await rt.pool.exec_many(
        targets,
        command,
        cwd=cwd,
        timeout=timeout,
        sudo=sudo,
        max_output_bytes=max_output_bytes,
    )
    _audit("exec_batch", command=command, aliases=targets, ok=all(r.get("ok") for r in results))
    return {
        "command": command,
        "targets": targets,
        "ok_count": sum(1 for r in results if r.get("ok")),
        "fail_count": sum(1 for r in results if not r.get("ok")),
        "results": results,
    }


@mcp.tool()
async def ssh_check(
    alias: Annotated[str, Field(description="要探测的主机别名")],
) -> dict[str, Any]:
    """握手测试：确认该别名能连上，并返回远端 uname / 用户 / 时间等基本信息。"""
    rt = runtime()
    probes = "uname -a 2>/dev/null || ver; id 2>/dev/null || whoami; date"
    try:
        res = await rt.pool.exec(alias, probes, timeout=30)
        _audit("check", alias=alias, ok=res.exit_code == 0)
        host = rt.cfg.get(alias)
        return {
            "alias": alias,
            "ok": res.exit_code == 0,
            "reachable": True,
            "target": f"{host.host}:{host.port}",
            "auth_method": host.auth.method,
            "output": res.stdout.strip(),
            "stderr": res.stderr.strip(),
            "duration_ms": res.duration_ms,
        }
    except Exception as exc:  # noqa: BLE001
        _audit("check_error", alias=alias, error=f"{type(exc).__name__}: {exc}")
        return {"alias": alias, "ok": False, "reachable": False, "error": f"{type(exc).__name__}: {exc}"}


@mcp.tool()
async def ssh_upload(
    alias: Annotated[str, Field(description="目标主机别名")],
    local_path: Annotated[str, Field(description="本机（MCP 服务所在机器）文件绝对路径")],
    remote_path: Annotated[str, Field(description="远端目标绝对路径，父目录会自动创建")],
) -> dict[str, Any]:
    """通过 SFTP 上传本机文件到远端，返回字节数与 sha256。"""
    rt = runtime()
    try:
        out = await rt.pool.upload(alias, local_path, remote_path)
        _audit("upload", alias=alias, local_path=local_path, remote_path=remote_path, ok=out["ok"])
        return out
    except Exception as exc:  # noqa: BLE001
        _audit("upload_error", alias=alias, local_path=local_path, remote_path=remote_path, error=str(exc))
        return {"alias": alias, "ok": False, "error": f"{type(exc).__name__}: {exc}"}


@mcp.tool()
async def ssh_download(
    alias: Annotated[str, Field(description="目标主机别名")],
    remote_path: Annotated[str, Field(description="远端文件绝对路径")],
    local_path: Annotated[str, Field(description="保存到 MCP 服务所在机器的路径，父目录自动创建")],
) -> dict[str, Any]:
    """通过 SFTP 从远端下载文件到本机。"""
    rt = runtime()
    try:
        out = await rt.pool.download(alias, remote_path, local_path)
        _audit("download", alias=alias, remote_path=remote_path, local_path=local_path, ok=out["ok"])
        return out
    except Exception as exc:  # noqa: BLE001
        _audit("download_error", alias=alias, remote_path=remote_path, error=str(exc))
        return {"alias": alias, "ok": False, "error": f"{type(exc).__name__}: {exc}"}


@mcp.tool()
async def ssh_write_file(
    alias: Annotated[str, Field(description="目标主机别名")],
    remote_path: Annotated[str, Field(description="远端文件绝对路径")],
    content: Annotated[str, Field(description="文件内容（UTF-8 文本）")],
    mode: Annotated[str | None, Field(description="八进制权限，例如 644 / 755")] = None,
) -> dict[str, Any]:
    """直接把文本内容写到远端文件（适合小配置文件，避免本机中转）。"""
    rt = runtime()
    try:
        out = await rt.pool.write_remote_file(alias, remote_path, content, mode)
        _audit("write_file", alias=alias, remote_path=remote_path, bytes=out["bytes_written"])
        return out
    except Exception as exc:  # noqa: BLE001
        _audit("write_file_error", alias=alias, remote_path=remote_path, error=str(exc))
        return {"alias": alias, "ok": False, "error": f"{type(exc).__name__}: {exc}"}


@mcp.tool()
async def ssh_read_file(
    alias: Annotated[str, Field(description="目标主机别名")],
    remote_path: Annotated[str, Field(description="远端文件绝对路径")],
    max_bytes: Annotated[int, Field(description="最多读取字节数，默认 256KB")] = 262144,
) -> dict[str, Any]:
    """读取远端文本文件内容（超过 max_bytes 会截断并标记 truncated）。"""
    rt = runtime()
    try:
        out = await rt.pool.read_remote_file(alias, remote_path, max_bytes)
        _audit("read_file", alias=alias, remote_path=remote_path, size=out["size"])
        return out
    except Exception as exc:  # noqa: BLE001
        _audit("read_file_error", alias=alias, remote_path=remote_path, error=str(exc))
        return {"alias": alias, "ok": False, "error": f"{type(exc).__name__}: {exc}"}


@mcp.tool()
async def ssh_shell_open(
    alias: Annotated[str, Field(description="主机别名")],
    cols: Annotated[int, Field(description="终端列数，默认 120")] = 120,
    rows: Annotated[int, Field(description="终端行数，默认 32")] = 32,
    term: Annotated[str, Field(description="TERM 值，影响颜色与全屏程序行为")] = "xterm-256color",
    command: Annotated[str | None, Field(description="可选：登录后立刻执行的命令，例如 'top'")] = None,
) -> dict[str, Any]:
    """开一个 PTY 交互式 shell（top / vi / less / tail -f 这类需要终端的程序可用）。

    返回 session_id 和首屏输出。后续用 ssh_shell_send 发按键、ssh_shell_read 取输出、
    ssh_shell_resize 改窗口大小、ssh_shell_close 关闭。别忘了关，否则会一直占着连接。
    """
    rt = runtime()
    try:
        out = await rt.pool.open_shell(alias, cols=cols, rows=rows, term=term, command=command)
        _audit("shell_open", alias=alias, session_id=out["session_id"], size=f"{cols}x{rows}", command=command)
        return {"ok": True, **out}
    except Exception as exc:  # noqa: BLE001
        _audit("shell_open_error", alias=alias, error=f"{type(exc).__name__}: {exc}")
        return {"ok": False, "alias": alias, "error": f"{type(exc).__name__}: {exc}"}


@mcp.tool()
async def ssh_shell_send(
    session_id: Annotated[str, Field(description="ssh_shell_open 返回的 session_id")],
    data: Annotated[str, Field(description="要发送的按键内容；回车用 \\r，Ctrl-C 用 \\u0003")],
    wait_ms: Annotated[int, Field(description="发完等多久没新输出就返回，默认 350ms")] = 350,
    timeout_ms: Annotated[int, Field(description="本次最多等多久，默认 8000ms")] = 8000,
) -> dict[str, Any]:
    """往交互式 shell 发送按键/文本，并返回这段时间内的输出。

    常见按键：回车 `\\r`、Ctrl-C `\\u0003`、Ctrl-D `\\u0004`、Esc `\\u001b`、
    Tab `\\t`、退格 `\\u007f`、方向键 `\\u001b[A`/`[B`/`[C`/`[D`。
    """
    rt = runtime()
    try:
        out = await rt.pool.shell_send(
            session_id, data, wait=max(0.05, wait_ms / 1000), timeout=max(0.2, timeout_ms / 1000)
        )
        _audit("shell_send", session_id=session_id, bytes=out["bytes"], closed=out["closed"])
        return {"ok": True, **out}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "session_id": session_id, "error": f"{type(exc).__name__}: {exc}"}


@mcp.tool()
async def ssh_shell_read(
    session_id: Annotated[str, Field(description="ssh_shell_open 返回的 session_id")],
    timeout_ms: Annotated[int, Field(description="最多等多久新输出，默认 1000ms")] = 1000,
) -> dict[str, Any]:
    """读交互式 shell 的新输出（不发送任何内容），适合观察 tail -f / 长任务的进展。"""
    rt = runtime()
    try:
        out = await rt.pool.shell_read(session_id, timeout=max(0.1, timeout_ms / 1000))
        return {"ok": True, **out}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "session_id": session_id, "error": f"{type(exc).__name__}: {exc}"}


@mcp.tool()
async def ssh_shell_resize(
    session_id: Annotated[str, Field(description="session_id")],
    cols: Annotated[int, Field(description="新的列数")],
    rows: Annotated[int, Field(description="新的行数")],
) -> dict[str, Any]:
    """调整交互式 shell 的终端大小（等价于拖拽终端窗口，全屏程序会重排）。"""
    rt = runtime()
    try:
        return {"ok": True, **await rt.pool.shell_resize(session_id, cols, rows)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "session_id": session_id, "error": f"{type(exc).__name__}: {exc}"}


@mcp.tool()
async def ssh_shell_close(
    session_id: Annotated[str, Field(description="要关闭的 session_id")],
) -> dict[str, Any]:
    """关闭交互式 shell 会话，释放对应的 channel。"""
    rt = runtime()
    out = await rt.pool.shell_close(session_id)
    _audit("shell_close", session_id=session_id, existed=out.get("existed"))
    return {"ok": True, **out}


@mcp.tool()
async def ssh_status() -> dict[str, Any]:
    """查看服务状态：已建立的连接、配置来源、策略与运行环境。"""
    rt = runtime()
    return {
        "server": rt.cfg.name,
        "config_source": rt.cfg.source,
        "python": platform.python_version(),
        "uptime_seconds": int(time.time() - rt.started_at),
        "hosts_configured": sorted(rt.cfg.hosts),
        "policy": rt.cfg.policy.describe(),
        "audit": {"enabled": rt.cfg.audit.enabled, "path": rt.cfg.audit.path},
        "autoload": {
            "enabled": rt.autoload,
            "config_path": str(rt._path) if rt._path else None,
            "reload_count": rt.reload_count,
            "last_reload_at": (
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(rt.last_reload_at))
                if rt.last_reload_at
                else None
            ),
            "last_error": rt.last_reload_error,
        },
        "connections": rt.pool.status(),
        "shells": rt.pool.shell_list(),
    }


@mcp.tool()
async def ssh_close(
    alias: Annotated[str | None, Field(description="只关闭某台主机的连接；留空关闭全部")] = None,
) -> dict[str, Any]:
    """主动断开连接池里的 SSH 连接（下次调用会自动重连）。"""
    rt = runtime()
    closed = await rt.pool.close(alias)
    _audit("close", closed=closed)
    return {"closed": closed, "remaining": [r["alias"] for r in rt.pool.status()]}


@mcp.tool()
async def ssh_reload_config() -> dict[str, Any]:
    """重新读取 servers.yaml（改完配置不用重启 MCP 进程），会断开旧连接。

    配置变化通常已经被自动热加载接管，这个工具留给「想立刻生效 / 强制重连」的场景。
    """
    rt = runtime()
    closed = await rt.pool.close()
    reloaded = rt.reload()
    _audit("reload_config", config=rt.cfg.source, closed=closed, reloaded=reloaded)
    return {
        "ok": True,
        "reloaded": reloaded,
        "config_source": rt.cfg.source,
        "hosts": sorted(rt.cfg.hosts),
        "closed_connections": closed,
        "last_error": rt.last_reload_error,
    }


@mcp.tool()
async def ssh_audit_tail(
    limit: Annotated[int, Field(description="返回最近多少条记录")] = 20,
) -> dict[str, Any]:
    """查看最近的审计记录（谁在什么时候对哪台机跑了什么命令）。"""
    rt = runtime()
    return {"path": rt.cfg.audit.path, "enabled": rt.cfg.audit.enabled, "records": rt.audit.tail(limit)}

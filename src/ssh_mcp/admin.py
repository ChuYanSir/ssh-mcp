"""Web 管理页 + REST API：在浏览器里增删改主机、测连通性、跑临时命令。

页面本身是静态壳（不含任何数据），所有数据接口都要 Bearer token。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from . import server as srv
from .config import ConfigError
from .config_store import cred_dir, delete_host, upsert_host

UI_FILE = Path(__file__).resolve().parent / "static" / "admin.html"

# 页面上的「查看 YAML」只给结构看，凭据字段一律打码
_SECRET_LINE = re.compile(r"(?im)^(\s*(?:password|key_passphrase|passphrase)\s*:\s*)(\S.*)$")


def redact_yaml(text: str) -> str:
    return _SECRET_LINE.sub(r'\1"***"', text)


def _err(exc: Exception, status: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=status)


async def index(request: Request) -> Response:
    if not UI_FILE.is_file():
        return JSONResponse({"ok": False, "error": "管理页面文件缺失"}, status_code=500)
    return HTMLResponse(UI_FILE.read_text(encoding="utf-8"))


async def list_hosts(request: Request) -> Response:
    rt = srv.runtime()
    conns = {c["alias"]: c for c in rt.pool.status()}
    hosts = []
    for alias, host in sorted(rt.cfg.hosts.items()):
        item = host.describe()
        item["connected"] = bool(conns.get(alias, {}).get("connected"))
        item["exec_count"] = conns.get(alias, {}).get("exec_count", 0)
        item["allowed_by_policy"] = not rt.cfg.policy.allowed_hosts or alias in rt.cfg.policy.allowed_hosts
        hosts.append(item)
    return JSONResponse(
        {
            "ok": True,
            "config_source": rt.cfg.source,
            "creds_dir": str(cred_dir()),
            "hosts": hosts,
            "policy": rt.cfg.policy.describe(),
            "audit": {"enabled": rt.cfg.audit.enabled, "path": rt.cfg.audit.path},
            "raw_yaml": redact_yaml(Path(rt.cfg.source).read_text(encoding="utf-8")) if rt.cfg.source else "",
        }
    )


async def create_or_update(request: Request) -> Response:
    rt = srv.runtime()
    try:
        payload = await request.json()
    except json.JSONDecodeError as exc:
        return _err(exc)
    alias = str(payload.get("alias") or "").strip()
    if not alias:
        return _err(ConfigError("缺少 alias"))
    try:
        info = upsert_host(Path(rt.cfg.source), alias, payload)
        await _reload(rt)
    except ConfigError as exc:
        return _err(exc)
    except KeyError as exc:
        return _err(exc, 404)
    rt.audit.write("admin_upsert", alias=alias, host=info["host"])
    return JSONResponse({"ok": True, **info, "reloaded": True})


async def remove_host(request: Request) -> Response:
    rt = srv.runtime()
    alias = request.path_params["alias"]
    try:
        await rt.pool.close(alias)
        delete_host(Path(rt.cfg.source), alias)
        await _reload(rt)
    except KeyError as exc:
        return _err(exc, 404)
    except ConfigError as exc:
        return _err(exc)
    rt.audit.write("admin_delete", alias=alias)
    return JSONResponse({"ok": True, "alias": alias, "reloaded": True})


async def test_host(request: Request) -> Response:
    rt = srv.runtime()
    alias = request.path_params["alias"]
    try:
        host = rt.cfg.get(alias)
    except KeyError as exc:
        return _err(exc, 404)
    cmd = "uname -a; id; date" if host.shell_kind == "posix" else "ver && whoami"
    try:
        res = await rt.pool.exec(alias, cmd, timeout=20)
        rt.audit.write("admin_test", alias=alias, exit_code=res.exit_code)
        return JSONResponse(
            {
                "ok": res.exit_code == 0,
                "alias": alias,
                "target": f"{host.host}:{host.port}",
                "exit_code": res.exit_code,
                "output": (res.stdout or res.stderr).strip()[:4000],
                "duration_ms": res.duration_ms,
            }
        )
    except Exception as exc:  # noqa: BLE001
        rt.audit.write("admin_test_error", alias=alias, error=str(exc))
        return JSONResponse({"ok": False, "alias": alias, "error": f"{type(exc).__name__}: {exc}"})


async def quick_exec(request: Request) -> Response:
    rt = srv.runtime()
    alias = request.path_params["alias"]
    try:
        payload = await request.json()
    except json.JSONDecodeError as exc:
        return _err(exc)
    command = str(payload.get("command") or "").strip()
    if not command:
        return _err(ConfigError("命令为空"))
    try:
        res = await rt.pool.exec(
            alias,
            command,
            cwd=payload.get("cwd") or None,
            sudo=bool(payload.get("sudo")),
            timeout=float(payload["timeout"]) if payload.get("timeout") else None,
        )
        return JSONResponse({"ok": True, **res.to_dict()})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"})


async def status(request: Request) -> Response:
    rt = srv.runtime()
    return JSONResponse(
        {
            "ok": True,
            "config_source": rt.cfg.source,
            "hosts": sorted(rt.cfg.hosts),
            "policy": rt.cfg.policy.describe(),
            "connections": rt.pool.status(),
        }
    )


async def _reload(rt) -> None:
    await rt.pool.close()
    rt.reload()


def build_admin_routes() -> list[Route]:
    return [
        Route("/", index, methods=["GET"]),
        Route("/favicon.ico", lambda r: Response(status_code=204), methods=["GET"]),
        Route("/api/hosts", list_hosts, methods=["GET"]),
        Route("/api/hosts", create_or_update, methods=["POST"]),
        Route("/api/hosts/{alias}", remove_host, methods=["DELETE"]),
        Route("/api/hosts/{alias}/test", test_host, methods=["POST"]),
        Route("/api/hosts/{alias}/exec", quick_exec, methods=["POST"]),
        Route("/api/status", status, methods=["GET"]),
    ]


def mount_admin(app: Any) -> Any:
    """把管理路由插到 MCP 路由前面，避免被 MCP 的 catch-all 抢先。"""
    routes = build_admin_routes()
    app.router.routes[:0] = routes
    return app

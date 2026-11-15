"""命令行入口。

用法：
  ssh-mcp --transport stdio                      # 单会话：由客户端拉起子进程
  ssh-mcp --transport http --port 8765           # 多会话共享：一份凭据、一个连接池
"""

from __future__ import annotations

import argparse
import hmac
import os
import sys
from typing import Any

from .config import CONFIG_ENV, ConfigError, load_config
from .server import init_runtime, mcp


class BearerGuard:
    """极简 ASGI 中间件：HTTP 传输模式下校验 Bearer token。

    管理页的静态壳（`/`）放行，因为它不含任何数据；所有 `/api/*` 与 `/mcp` 必须带 token。
    """

    def __init__(self, app: Any, token: str, public_paths: Any = frozenset({"/", "/favicon.ico"})) -> None:
        self.app = app
        self.token = token
        self.public_paths = public_paths

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] in ("http", "websocket"):
            if scope["type"] == "http" and scope.get("method") in ("GET", "HEAD") and scope.get("path") in self.public_paths:
                await self.app(scope, receive, send)
                return
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
            provided = headers.get("authorization", "")
            expected = f"Bearer {self.token}"
            if not hmac.compare_digest(provided, expected):
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 4401})
                    return
                body = b'{"error":"unauthorized","hint":"Authorization: Bearer <token>"}'
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ssh-mcp",
        description="SSH 中间商 MCP 服务：凭据留在服务端，会话端只用别名执行命令。",
    )
    p.add_argument("--config", default=None, help=f"servers.yaml 路径（也可用环境变量 {CONFIG_ENV}）")
    p.add_argument(
        "--transport",
        default=os.environ.get("SSH_MCP_TRANSPORT", "stdio"),
        choices=["stdio", "http", "streamable-http", "sse"],
        help="stdio=客户端拉起子进程；http=常驻服务多会话共享（默认 streamable-http）",
    )
    p.add_argument("--host", default=os.environ.get("SSH_MCP_HOST", "127.0.0.1"), help="HTTP 监听地址")
    p.add_argument("--port", type=int, default=int(os.environ.get("SSH_MCP_PORT", "8765")), help="HTTP 监听端口")
    p.add_argument("--path", default=os.environ.get("SSH_MCP_PATH", "/mcp"), help="HTTP 端点路径")
    p.add_argument("--token", default=None, help="HTTP Bearer token（也可用 SSH_MCP_TOKEN / 配置文件 http.token）")
    p.add_argument(
        "--allow-any-host",
        action="store_true",
        help="放开 DNS rebinding 的 Host 头校验（监听非回环地址时自动放开）",
    )
    p.add_argument("--no-ui", action="store_true", help="不挂载 Web 管理页（只跑纯 MCP 端点）")
    p.add_argument("--print-config", action="store_true", help="只打印解析后的脱敏配置并退出")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"[ssh-mcp] 配置错误: {exc}", file=sys.stderr)
        return 2

    transport = "streamable-http" if args.transport == "http" else args.transport
    init_runtime(args.config)

    if args.print_config:
        import json

        print(
            json.dumps(
                {
                    "source": cfg.source,
                    "hosts": [h.describe() for h in cfg.hosts.values()],
                    "policy": cfg.policy.describe(),
                    "audit": {"enabled": cfg.audit.enabled, "path": cfg.audit.path},
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    print(
        f"[ssh-mcp] config={cfg.source} hosts={len(cfg.hosts)} transport={transport}",
        file=sys.stderr,
    )

    if transport == "stdio":
        mcp.run(transport="stdio")
        return 0

    token = args.token or os.environ.get("SSH_MCP_TOKEN")
    loopback = args.host in ("127.0.0.1", "localhost", "::1")

    from mcp.server.transport_security import TransportSecuritySettings

    if args.allow_any_host or not loopback:
        security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    else:
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[
                "127.0.0.1",
                "127.0.0.1:*",
                "localhost",
                "localhost:*",
                "[::1]",
                "[::1]:*",
            ],
        )

    if transport == "sse":
        app = mcp.sse_app()
    else:
        app = mcp.streamable_http_app(streamable_http_path=args.path, transport_security=security)

    if not args.no_ui:
        from .admin import mount_admin

        app = mount_admin(app)
        print(f"[ssh-mcp] 管理页: http://{args.host}:{args.port}/", file=sys.stderr)

    if token:
        app = BearerGuard(app, token)
        print(f"[ssh-mcp] bearer auth enabled (token len={len(token)})", file=sys.stderr)
    elif not loopback:
        print(
            "[ssh-mcp] 警告：监听非回环地址但没有设置 token，"
            "任何能访问该端口的人都能借用你的 SSH 凭据。建议加 --token。",
            file=sys.stderr,
        )

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

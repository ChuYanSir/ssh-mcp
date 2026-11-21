"""配置加载：所有 SSH 连接凭据只在服务端解析，绝不经 MCP 工具参数传递。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

CONFIG_ENV = "SSH_MCP_CONFIG"
DEFAULT_HOST_KEY_POLICY = "auto_add"
VALID_HOST_KEY_POLICY = {"strict", "auto_add", "ignore"}
VALID_AUTH_METHODS = {"key", "password", "agent", "none"}
VALID_SHELL_KINDS = {"posix", "powershell", "cmd"}


class ConfigError(RuntimeError):
    """配置非法。启动或热加载时直接抛出，避免带着错误配置去连生产机。"""


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return os.path.expandvars(os.path.expanduser(value))
    return value


def _expand_path(value: Any) -> Any:
    if isinstance(value, str) and value:
        return Path(os.path.expandvars(os.path.expanduser(value))).as_posix()
    return value


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


@dataclass
class Auth:
    """凭据解析顺序：显式字面量 > *_env 环境变量 > *_file 文件内容。"""

    method: str = "agent"
    username: str | None = None
    password: str | None = None
    password_env: str | None = None
    password_file: str | None = None
    key_file: str | None = None
    # 直接把私钥内容贴在配置里（不想落地成文件时用），二选一
    key_content: str | None = None
    key_content_env: str | None = None
    key_passphrase: str | None = None
    key_passphrase_env: str | None = None
    key_passphrase_file: str | None = None
    allow_agent: bool = True
    look_for_keys: bool = False

    @classmethod
    def from_mapping(cls, raw: Any) -> "Auth":
        if raw is None:
            return cls(method="agent")
        if isinstance(raw, str):
            # 简写：auth: key  /  auth: password
            return cls(method=raw.strip().lower())
        if not isinstance(raw, dict):
            raise ConfigError(f"auth 段必须是 map 或字符串，收到 {type(raw).__name__}")
        # 私钥正文不能被 expandvars/expanduser 动过（PEM 里出现 $ 也不会，但没必要冒险）
        inline_keys = {"key_content"}
        data = {k: (v if k in inline_keys else _expand(v)) for k, v in raw.items()}
        if isinstance(data.get("key_content"), str):
            data["key_content"] = data["key_content"].strip() + "\n"
        data["key_file"] = _expand_path(data.get("key_file"))
        data["password_file"] = _expand_path(data.get("password_file"))
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(data) - known
        if unknown:
            raise ConfigError(f"auth 段存在未知字段: {sorted(unknown)}")
        auth = cls(**data)
        auth.method = (auth.method or "agent").strip().lower()
        if auth.method not in VALID_AUTH_METHODS:
            raise ConfigError(f"auth.method 只支持 {sorted(VALID_AUTH_METHODS)}，收到 {auth.method!r}")
        return auth

    def _secret(self, literal: str | None, env_name: str | None, file_path: str | None) -> str | None:
        if literal:
            return literal
        if env_name:
            val = os.environ.get(env_name)
            if val is not None:
                return val
            raise ConfigError(f"环境变量 {env_name} 未设置，无法取得凭据")
        if file_path:
            p = Path(file_path)
            if not p.is_file():
                raise ConfigError(f"凭据文件不存在: {file_path}")
            return p.read_text(encoding="utf-8").rstrip("\r\n")
        return None

    def resolve_password(self) -> str | None:
        if self.method not in ("password",):
            return None
        pw = self._secret(self.password, self.password_env, self.password_file)
        if not pw:
            raise ConfigError("auth.method=password 但没有任何可用密码来源")
        return pw

    def resolve_passphrase(self) -> str | None:
        return self._secret(self.key_passphrase, self.key_passphrase_env, self.key_passphrase_file)

    def resolve_key_content(self) -> str | None:
        """内联私钥正文：key_content 优先，其次 key_content_env。都没配就返回 None。"""
        return self._secret(self.key_content, self.key_content_env, None)

    def describe(self) -> dict[str, Any]:
        """脱敏描述：只暴露认证方式，不暴露任何凭据材料。"""
        return {
            "method": self.method,
            "username": self.username,
            "key_file": self.key_file,
            "has_key_content": bool(self.key_content or self.key_content_env),
            "key_source": (
                "inline" if self.key_content
                else f"env:{self.key_content_env}" if self.key_content_env
                else f"file:{self.key_file}" if self.key_file
                else None
            ),
            "password_source": (
                "literal" if self.password
                else f"env:{self.password_env}" if self.password_env
                else f"file:{self.password_file}" if self.password_file
                else None
            ),
        }


@dataclass
class Sudo:
    password: str | None = None
    password_env: str | None = None
    password_file: str | None = None
    user: str | None = None

    @classmethod
    def from_mapping(cls, raw: Any) -> "Sudo | None":
        if raw in (None, False):
            return None
        if raw is True:
            return cls()
        if not isinstance(raw, dict):
            raise ConfigError("sudo 段必须是 map / true / false")
        data = {k: _expand(v) for k, v in raw.items()}
        data["password_file"] = _expand_path(data.get("password_file"))
        return cls(**data)

    def resolve_password(self) -> str | None:
        return Auth()._secret(self.password, self.password_env, self.password_file)

    def describe(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "user": self.user,
            "password_source": (
                "literal" if self.password
                else f"env:{self.password_env}" if self.password_env
                else f"file:{self.password_file}" if self.password_file
                else None
            ),
        }


@dataclass
class Host:
    alias: str
    host: str
    port: int = 22
    user: str | None = None
    description: str = ""
    tags: list[str] = field(default_factory=list)
    auth: Auth = field(default_factory=Auth)
    sudo: Sudo | None = None
    shell_kind: str = "posix"
    posix_shell: str = "bash"
    default_cwd: str | None = None
    connect_timeout: float = 15.0
    command_timeout: float = 120.0
    max_output_bytes: int = 262144
    keepalive: int = 30
    host_key_policy: str = DEFAULT_HOST_KEY_POLICY
    known_hosts: str | None = None
    encoding: str = "utf-8"
    max_concurrency: int = 4
    proxy_jump: str | None = None  # 预留：另一台已配置 mainframe 的 alias

    def describe(self) -> dict[str, Any]:
        return {
            "alias": self.alias,
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "description": self.description,
            "tags": self.tags,
            "auth": self.auth.describe(),
            "sudo": self.sudo.describe() if self.sudo else None,
            "shell_kind": self.shell_kind,
            "posix_shell": self.posix_shell,
            "default_cwd": self.default_cwd,
            "command_timeout": self.command_timeout,
            "max_output_bytes": self.max_output_bytes,
            "host_key_policy": self.host_key_policy,
            "max_concurrency": self.max_concurrency,
            "proxy_jump": self.proxy_jump,
        }


@dataclass
class Policy:
    allow_commands: list[re.Pattern[str]] = field(default_factory=list)
    deny_commands: list[re.Pattern[str]] = field(default_factory=list)
    max_command_timeout: float = 3600.0
    max_output_bytes: int = 4 * 1024 * 1024
    allow_sudo: bool = True
    allow_upload: bool = True
    allow_download: bool = True
    allow_write_file: bool = True
    # 单次文件传输的空闲超时上限（秒）：多久没数据就认定连接废了
    max_transfer_timeout: float = 600.0
    # 交互式 shell / PTY
    allow_shell: bool = True
    shell_idle_timeout: float = 900.0  # 无操作多久自动关闭会话（秒）
    max_shell_sessions: int = 8
    # 连接自愈：空闲多久先探活一次 / 后台巡检间隔 / 单次探测超时（<=0 关闭）
    idle_probe_after: float = 240.0
    heal_interval: float = 120.0
    probe_timeout: float = 10.0
    # stdio 模式下：连续多久没有任何工具调用就自行退出（让客户端重启一个干净进程）
    exit_if_idle: float = 1800.0
    allowed_hosts: list[str] = field(default_factory=list)  # 空 = 允许配置里的全部
    dry_run: bool = False

    @classmethod
    def from_mapping(cls, raw: Any) -> "Policy":
        raw = raw or {}
        if not isinstance(raw, dict):
            raise ConfigError("policy 段必须是 map")
        data = dict(raw)
        data["allow_commands"] = [re.compile(p) for p in _as_list(data.get("allow_commands"))]
        data["deny_commands"] = [re.compile(p) for p in _as_list(data.get("deny_commands"))]
        data["allowed_hosts"] = _as_list(data.get("allowed_hosts"))
        known = set(cls.__dataclass_fields__)
        unknown = set(data) - known
        if unknown:
            raise ConfigError(f"policy 段存在未知字段: {sorted(unknown)}")
        return cls(**data)

    def check_command(self, command: str) -> None:
        if not command.strip():
            raise PermissionError("命令为空")
        for pat in self.deny_commands:
            if pat.search(command):
                raise PermissionError(f"命令命中禁止规则 /{pat.pattern}/")
        if self.allow_commands and not any(p.search(command) for p in self.allow_commands):
            raise PermissionError("命令未命中任何白名单规则（policy.allow_commands 非空）")

    def check_host(self, alias: str) -> None:
        if self.allowed_hosts and alias not in self.allowed_hosts:
            raise PermissionError(f"主机 {alias!r} 不在 policy.allowed_hosts 内")

    def describe(self) -> dict[str, Any]:
        return {
            "allow_commands": [p.pattern for p in self.allow_commands],
            "deny_commands": [p.pattern for p in self.deny_commands],
            "max_command_timeout": self.max_command_timeout,
            "max_output_bytes": self.max_output_bytes,
            "allow_sudo": self.allow_sudo,
            "allow_upload": self.allow_upload,
            "allow_download": self.allow_download,
            "allow_write_file": self.allow_write_file,
            "max_transfer_timeout": self.max_transfer_timeout,
            "allow_shell": self.allow_shell,
            "shell_idle_timeout": self.shell_idle_timeout,
            "max_shell_sessions": self.max_shell_sessions,
            "idle_probe_after": self.idle_probe_after,
            "heal_interval": self.heal_interval,
            "probe_timeout": self.probe_timeout,
            "exit_if_idle": self.exit_if_idle,
            "allowed_hosts": self.allowed_hosts or "all-configured",
            "dry_run": self.dry_run,
        }


@dataclass
class Audit:
    enabled: bool = True
    path: str = "~/.ssh-mcp/audit.jsonl"
    max_bytes: int = 0  # 0 = 不轮转


@dataclass
class ServerConfig:
    hosts: dict[str, Host]
    policy: Policy = field(default_factory=Policy)
    audit: Audit = field(default_factory=Audit)
    source: str | None = None
    name: str = "ssh-mcp"

    def get(self, alias: str) -> Host:
        host = self.hosts.get(alias)
        if host is None:
            known = ", ".join(sorted(self.hosts)) or "(空)"
            raise KeyError(f"未配置主机别名 {alias!r}；已配置: {known}")
        self.policy.check_host(alias)
        return host


_DEFAULT_KEYS = {
    "port", "user", "connect_timeout", "command_timeout", "max_output_bytes",
    "keepalive", "host_key_policy", "known_hosts", "encoding", "shell_kind",
    "default_cwd", "max_concurrency", "proxy_jump", "posix_shell",
}


def _default_config_paths() -> list[Path]:
    candidates: list[Path] = []
    env = os.environ.get(CONFIG_ENV)
    if env:
        candidates.append(Path(os.path.expandvars(os.path.expanduser(env))))
    candidates.append(Path.cwd() / "servers.yaml")
    candidates.append(Path.cwd() / ".ssh-mcp" / "servers.yaml")
    candidates.append(Path.home() / ".ssh-mcp" / "servers.yaml")
    return candidates


def find_config_path(explicit: str | None = None) -> Path:
    if explicit:
        p = Path(os.path.expandvars(os.path.expanduser(explicit)))
        if not p.is_file():
            raise ConfigError(f"指定的配置文件不存在: {p}")
        return p
    tried = _default_config_paths()
    for p in tried:
        if p.is_file():
            return p
    listed = "\n  ".join(str(p) for p in tried)
    raise ConfigError(
        "没有找到配置文件。请创建 servers.yaml，或用 ssh-mcp --config <path> / 环境变量 "
        f"{CONFIG_ENV} 指定。已尝试:\n  {listed}"
    )


def load_config(path: str | None = None) -> ServerConfig:
    cfg_path = find_config_path(path)
    try:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"配置文件 YAML 解析失败: {cfg_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("配置文件顶层必须是 map")

    defaults = raw.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise ConfigError("defaults 段必须是 map")
    unknown_defaults = set(defaults) - _DEFAULT_KEYS
    if unknown_defaults:
        raise ConfigError(f"defaults 段存在未知字段: {sorted(unknown_defaults)}")

    hosts_raw = raw.get("hosts")
    if hosts_raw is None:
        hosts_raw = {}
    if not isinstance(hosts_raw, dict):
        raise ConfigError("hosts 段必须是 map: alias -> host 配置")

    hosts: dict[str, Host] = {}
    for alias, entry in hosts_raw.items():
        alias = str(alias)
        if not isinstance(entry, dict):
            raise ConfigError(f"hosts.{alias} 必须是 map")
        merged: dict[str, Any] = {**{k: _expand(defaults[k]) for k in defaults}, **entry}
        host_field = merged.pop("host", None)
        if not host_field:
            raise ConfigError(f"hosts.{alias} 缺少 host")
        merged.setdefault("host_key_policy", DEFAULT_HOST_KEY_POLICY)
        merged["host_key_policy"] = str(merged["host_key_policy"]).lower()
        if merged["host_key_policy"] not in VALID_HOST_KEY_POLICY:
            raise ConfigError(f"hosts.{alias}.host_key_policy 非法: {merged['host_key_policy']}")
        merged["shell_kind"] = str(merged.get("shell_kind", "posix")).lower()
        if merged["shell_kind"] not in VALID_SHELL_KINDS:
            raise ConfigError(f"hosts.{alias}.shell_kind 非法: {merged['shell_kind']}")
        merged["known_hosts"] = _expand_path(merged.get("known_hosts"))
        auth = Auth.from_mapping(merged.pop("auth", None))
        sudo = Sudo.from_mapping(merged.pop("sudo", None))
        tags = _as_list(merged.pop("tags", None))
        allowed = set(Host.__dataclass_fields__) - {"alias", "host", "auth", "sudo", "tags"}
        unknown = set(merged) - allowed
        if unknown:
            raise ConfigError(f"hosts.{alias} 存在未知字段: {sorted(unknown)}")
        merged.pop("description", None)
        hosts[alias] = Host(
            alias=alias,
            host=str(host_field),
            auth=auth,
            sudo=sudo,
            tags=tags,
            description=str(entry.get("description", "") or ""),
            **merged,
        )

    policy = Policy.from_mapping(raw.get("policy"))
    audit_raw = raw.get("audit") or {}
    if isinstance(audit_raw, bool):
        audit_raw = {"enabled": audit_raw}
    audit = Audit(**{k: v for k, v in audit_raw.items() if k in Audit.__dataclass_fields__})
    audit.path = _expand_path(audit.path) or audit.path
    policy.max_output_bytes = max(policy.max_output_bytes, 4096)

    return ServerConfig(
        hosts=hosts,
        policy=policy,
        audit=audit,
        source=str(cfg_path),
        name=str(raw.get("name") or "ssh-mcp"),
    )

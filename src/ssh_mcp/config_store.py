"""servers.yaml 的读写层：给 Web 管理页用。

原则：
1. 先在临时文件上跑一遍 `load_config()` 校验，通过了才覆盖正式文件；
2. 覆盖前留 `.bak` 备份；
3. 密码不落明文（默认写 `~/.ssh-mcp/creds/<alias>.pass`，yaml 里只留 `password_file`）。
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any

import yaml

from .config import ConfigError, load_config

def cred_dir() -> Path:
    """凭据文件目录；可用 SSH_MCP_CRED_DIR 覆盖（测试与多实例隔离用）。"""
    return Path(os.environ.get("SSH_MCP_CRED_DIR") or (Path.home() / ".ssh-mcp" / "creds")).expanduser()


def _write_cred(alias: str, kind: str, secret: str) -> str:
    """把密钥写到独立文件，返回写入的绝对路径（给 password_file / *_file 用）。"""
    target_dir = cred_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{alias}.{kind}"
    target.write_text(secret, encoding="utf-8")
    try:
        os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return str(target)

_HOST_SCALARS = (
    "host", "port", "user", "description", "shell_kind", "posix_shell", "default_cwd",
    "connect_timeout", "command_timeout", "keepalive", "host_key_policy", "known_hosts",
    "max_concurrency", "proxy_jump", "encoding",
)


def _read_raw(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"name": "ssh-mcp", "hosts": {}}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError("配置文件顶层必须是 map")
    raw.setdefault("hosts", {})
    if not isinstance(raw["hosts"], dict):
        raise ConfigError("hosts 段必须是 map")
    return raw


TEMPLATE = """\
# ssh-mcp 配置（桌面端 / Web 管理页写入的都是这个文件）
name: ssh-mcp

defaults:
  port: 22
  connect_timeout: 15
  command_timeout: 120
  max_output_bytes: 262144
  keepalive: 30
  host_key_policy: auto_add
  known_hosts: ~/.ssh/known_hosts
  encoding: utf-8
  shell_kind: posix
  posix_shell: bash
  max_concurrency: 4

policy:
  deny_commands:
    - 'rm\\s+-rf\\s+/(\\s|$)'
    - ':\\(\\)\\s*\\{\\s*:\\|:&\\s*\\};:'
    - 'mkfs\\.'
    - 'dd\\s+.*of=/dev/[sh]d[a-z]'
  allow_commands: []
  max_command_timeout: 3600
  max_output_bytes: 4194304
  allow_sudo: true
  allow_upload: true
  allow_download: true
  allow_write_file: true
  allowed_hosts: []
  dry_run: false

audit:
  enabled: true
  path: ~/.ssh-mcp/audit.jsonl
  max_bytes: 0

hosts: {}
"""


def ensure_config(path: Path) -> bool:
    """配置文件不存在时创建一个空壳（带默认值与注释），返回是否新建。"""
    if path.is_file():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TEMPLATE, encoding="utf-8")
    return True


def _default_env(alias: str, suffix: str) -> str:
    slug = "".join(ch if ch.isalnum() else "_" for ch in alias).upper()
    return f"SSH_MCP_{slug}_{suffix}"


def _existing_auth(entry: dict[str, Any]) -> dict[str, Any]:
    auth = entry.get("auth")
    if isinstance(auth, str):
        return {"method": auth.strip().lower()}
    if isinstance(auth, dict):
        return dict(auth)
    return {}


def _apply_secret(
    target: dict[str, Any],
    *,
    existing: dict[str, Any],
    alias: str,
    kind: str,
    value: str | None,
    mode: str | None,
    env_name: str | None,
    file_keys: tuple[str, ...],
    env_keys: tuple[str, ...],
    plain_keys: tuple[str, ...],
) -> None:
    """把「新输入的凭据 + 存储方式」落到目标 dict；value 为空时沿用 existing 的凭据字段。"""
    mode = (mode or "keep").strip().lower()
    value = (value or "").strip()

    if not value:
        for k in (*file_keys, *env_keys, *plain_keys):
            if existing.get(k):
                target[k] = existing[k]
        return

    if mode == "file":
        for k in file_keys:
            target.pop(k, None)
        target[file_keys[0]] = _write_cred(alias, kind, value)
        for k in env_keys + plain_keys:
            target.pop(k, None)
    elif mode == "env":
        name = (env_name or "").strip() or _default_env(alias, kind.upper())
        for k in file_keys + plain_keys:
            target.pop(k, None)
        target[env_keys[0]] = name
    else:  # plain
        for k in (*file_keys, *env_keys):
            target.pop(k, None)
        target[plain_keys[0]] = value


def build_entry(payload: dict[str, Any], existing: dict[str, Any], alias: str) -> dict[str, Any]:
    entry: dict[str, Any] = {}

    for key in _HOST_SCALARS:
        if key not in payload:
            continue
        value = payload[key]
        if value is None or value == "":
            continue
        if key in ("port", "connect_timeout", "command_timeout", "keepalive", "max_concurrency"):
            try:
                entry[key] = int(value)
            except (TypeError, ValueError) as exc:
                raise ConfigError(f"{key} 必须是整数") from exc
        else:
            entry[key] = value

    if not entry.get("host"):
        raise ConfigError("host 不能为空")

    tags = payload.get("tags")
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.replace("，", ",").split(",") if t.strip()]
    if tags:
        entry["tags"] = list(tags)

    # ------------------------------------------------------------- auth
    method = str(payload.get("auth_method") or "agent").lower()
    if method not in ("key", "password", "agent"):
        raise ConfigError("auth_method 只支持 key / password / agent")
    auth: dict[str, Any] = {"method": method}
    old_auth = _existing_auth(existing)

    if method == "key":
        key_file = (payload.get("key_file") or old_auth.get("key_file") or "").strip()
        if not key_file:
            raise ConfigError("私钥认证需要填 key_file")
        auth["key_file"] = key_file
        _apply_secret(
            auth,
            existing=old_auth,
            alias=alias,
            kind="keypass",
            value=payload.get("key_passphrase"),
            mode=payload.get("key_passphrase_mode"),
            env_name=payload.get("key_passphrase_env"),
            file_keys=("key_passphrase_file",),
            env_keys=("key_passphrase_env",),
            plain_keys=("key_passphrase",),
        )
        # key 模式下丢掉密码凭据，避免两种凭据并存混淆
    elif method == "password":
        _apply_secret(
            auth,
            existing=old_auth,
            alias=alias,
            kind="pass",
            value=payload.get("password"),
            mode=payload.get("password_mode"),
            env_name=payload.get("password_env"),
            file_keys=("password_file",),
            env_keys=("password_env",),
            plain_keys=("password",),
        )
        if not any(auth.get(k) for k in ("password", "password_env", "password_file")):
            raise ConfigError("密码认证需要填写密码（或环境变量名 / 凭据文件）")
    else:
        auth["allow_agent"] = True

    entry["auth"] = auth

    # ------------------------------------------------------------- sudo
    if payload.get("sudo_enabled"):
        sudo: dict[str, Any] = {}
        old_sudo = existing.get("sudo")
        if not isinstance(old_sudo, dict):
            old_sudo = {}
        if payload.get("sudo_user"):
            sudo["user"] = str(payload["sudo_user"]).strip()
        _apply_secret(
            sudo,
            existing=old_sudo,
            alias=alias,
            kind="sudo",
            value=payload.get("sudo_password"),
            mode=payload.get("sudo_password_mode"),
            env_name=payload.get("sudo_password_env"),
            file_keys=("password_file",),
            env_keys=("password_env",),
            plain_keys=("password",),
        )
        entry["sudo"] = sudo

    return entry


def upsert_host(config_path: Path, alias: str, payload: dict[str, Any]) -> dict[str, Any]:
    alias = alias.strip()
    if not alias:
        raise ConfigError("别名不能为空")
    if not all(ch.isalnum() or ch in "-_.@" for ch in alias):
        raise ConfigError("别名只能包含字母、数字、- _ . @")

    raw = _read_raw(config_path)
    hosts = raw["hosts"]
    existing = hosts.get(alias) or {}
    if not isinstance(existing, dict):
        raise ConfigError(f"hosts.{alias} 结构异常，请先手工修正配置文件")

    entry = build_entry(payload, existing, alias)
    hosts[alias] = entry

    _validate_and_write(config_path, raw)
    return {"alias": alias, "host": entry["host"], "port": entry.get("port", 22)}


def delete_host(config_path: Path, alias: str) -> None:
    raw = _read_raw(config_path)
    hosts = raw["hosts"]
    if alias not in hosts:
        raise KeyError(f"没有别名 {alias!r}")
    if len(hosts) <= 1:
        raise ConfigError("至少要保留一台主机；如果要清空，请直接编辑配置文件")
    hosts.pop(alias)
    _validate_and_write(config_path, raw)


def _validate_and_write(config_path: Path, raw: dict[str, Any]) -> None:
    text = yaml.safe_dump(raw, allow_unicode=True, sort_keys=False, default_flow_style=False)

    fd, tmp_name = tempfile.mkstemp(prefix="servers.", suffix=".yaml", dir=str(config_path.parent))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        tmp.write_text(text, encoding="utf-8")
        try:
            load_config(str(tmp))  # 校验：schema 不合法就不落盘
        except ConfigError as exc:
            raise ConfigError(f"配置校验失败: {exc}") from exc
    finally:
        tmp.unlink(missing_ok=True)

    if config_path.is_file():
        backup = config_path.with_suffix(config_path.suffix + ".bak")
        shutil.copy2(config_path, backup)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(text, encoding="utf-8")

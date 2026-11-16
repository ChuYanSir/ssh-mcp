"""检查更新：查 GitHub Releases，按需下载 wheel 并升级。

只做两件很窄的事，避免把"自动更新"做成风险点：
1. 读 `releases/latest`，比版本号；
2. 用户点了才下载 + 调 `pip install --upgrade`，装完提示重启。

网络失败、API 限流、JSON 异常统统返回 `{ok: False}`，绝不抛到界面上。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import __github__, __version__

API_LATEST = "https://api.github.com/repos/ChuYanSir/ssh-mcp/releases/latest"
RELEASES_PAGE = f"{__github__}/releases/latest"
USER_AGENT = f"ssh-mcp/{__version__}"


def github_owner_repo() -> str:
    return __github__.rstrip("/").split("github.com/")[-1]


# --------------------------------------------------------------------------- #
# 版本比较
# --------------------------------------------------------------------------- #
_VERSION_RE = re.compile(r"^v?(\d+(?:\.\d+)*)")


def parse_version(text: str) -> tuple[int, ...]:
    """`v1.2.3` / `1.2.3-beta` → (1, 2, 3)；解析不出来就当 0.0.0。"""
    m = _VERSION_RE.match((text or "").strip())
    if not m:
        return (0,)
    return tuple(int(part) for part in m.group(1).split("."))


def is_newer(candidate: str, current: str) -> bool:
    return parse_version(candidate) > parse_version(current)


def is_editable_install() -> bool:
    """源码目录里 `pip install -e .` 装的话，升级 wheel 会和源码打架，得走 git pull。"""
    try:
        import importlib.metadata as md

        dist = md.distribution("ssh-mcp")
        raw = dist.read_text("direct_url.json")
        if raw:
            data = json.loads(raw)
            return bool((data.get("dir_info") or {}).get("editable"))
    except Exception:  # noqa: BLE001
        pass
    return False


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #
@dataclass
class ReleaseInfo:
    tag: str
    version: str
    url: str = RELEASES_PAGE
    published_at: str = ""
    notes: str = ""
    wheel_name: str | None = None
    wheel_url: str | None = None
    wheel_size: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "tag": self.tag,
            "version": self.version,
            "url": self.url,
            "published_at": self.published_at,
            "notes": self.notes,
            "wheel_name": self.wheel_name,
            "wheel_size": self.wheel_size,
            "has_wheel": bool(self.wheel_url),
        }


def _http_json(url: str, timeout: float) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _pick_wheel(assets: list[dict[str, Any]]) -> dict[str, Any] | None:
    wheels = [a for a in assets if str(a.get("name", "")).endswith(".whl")]
    if not wheels:
        return None
    # 优先 py3-none-any
    wheels.sort(key=lambda a: ("py3-none-any" not in a.get("name", ""), a.get("name", "")))
    return wheels[0]


def fetch_latest(api: str = API_LATEST, timeout: float = 8.0) -> ReleaseInfo:
    data = _http_json(api, timeout)
    tag = str(data.get("tag_name") or "")
    wheel = _pick_wheel(list(data.get("assets") or []))
    return ReleaseInfo(
        tag=tag,
        version=tag.lstrip("v") or "0",
        url=str(data.get("html_url") or RELEASES_PAGE),
        published_at=str(data.get("published_at") or ""),
        notes=str(data.get("body") or ""),
        wheel_name=(wheel or {}).get("name"),
        wheel_url=(wheel or {}).get("browser_download_url"),
        wheel_size=int((wheel or {}).get("size") or 0),
    )


def check(current: str = __version__, *, timeout: float = 8.0, api: str = API_LATEST) -> dict[str, Any]:
    """返回 {ok, current, latest, has_update, ...}；任何失败都 ok=False 而不是抛异常。"""
    try:
        info = fetch_latest(api=api, timeout=timeout)
    except urllib.error.HTTPError as exc:
        hint = "（API 限流，稍后再试）" if exc.code == 403 else ""
        return {"ok": False, "current": current, "error": f"HTTP {exc.code}{hint}"}
    except urllib.error.URLError as exc:
        return {"ok": False, "current": current, "error": f"网络不可用：{exc.reason}"}
    except (TimeoutError, json.JSONDecodeError, OSError) as exc:
        return {"ok": False, "current": current, "error": f"{type(exc).__name__}: {exc}"}

    if not info.tag:
        return {"ok": False, "current": current, "error": "release 里没有 tag_name"}

    return {
        "ok": True,
        "current": current,
        "latest": info.version,
        "has_update": is_newer(info.version, current),
        "info": info.to_dict(),
    }


# --------------------------------------------------------------------------- #
# 下载 + 安装
# --------------------------------------------------------------------------- #
def download_wheel(
    info: ReleaseInfo,
    dest_dir: Path | None = None,
    *,
    timeout: float = 60.0,
    progress: Callable[[int, int], None] | None = None,
) -> Path:
    """下载 release 里的 wheel，返回本地路径。"""
    if not info.wheel_url or not info.wheel_name:
        raise ValueError("这个 release 没有 wheel 资产")
    target_dir = Path(dest_dir or tempfile.mkdtemp(prefix="ssh-mcp-update-"))
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / Path(info.wheel_name).name

    req = urllib.request.Request(info.wheel_url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        total = int(resp.headers.get("Content-Length") or info.wheel_size or 0)
        done = 0
        with dest.open("wb") as fh:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    if dest.stat().st_size <= 0:
        raise OSError("下载到的文件是空的")
    return dest


def install_wheel(wheel: Path, *, python: str | None = None, timeout: float = 300.0) -> dict[str, Any]:
    """用当前解释器的 pip 升级安装；返回 {ok, output}。"""
    py = python or sys.executable
    cmd = [py, "-m", "pip", "install", "--upgrade", "--disable-pip-version-check", str(wheel)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "cmd": cmd}
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return {"ok": proc.returncode == 0, "returncode": proc.returncode, "output": output[-4000:], "cmd": cmd}


def main(argv: list[str] | None = None) -> int:
    """命令行自检：python -m ssh_mcp.update"""
    import argparse

    parser = argparse.ArgumentParser(prog="ssh-mcp-update", description="检查 ssh-mcp 是否有新版本")
    parser.add_argument("--current", default=__version__, help="当前版本，默认取包内版本")
    parser.add_argument("--timeout", type=float, default=8.0)
    args = parser.parse_args(argv)

    result = check(args.current, timeout=args.timeout)
    if not result["ok"]:
        print(f"检查失败：{result['error']}")
        return 2
    info = result["info"]
    print(f"当前版本：{result['current']}")
    print(f"最新版本：{result['latest']}（{info['tag']}，发布于 {info['published_at']}）")
    print(f"下载地址：{info['url']}")
    if result["has_update"]:
        print(f"有新版本，可用 wheel：{info['wheel_name']}（{info['wheel_size']} bytes）")
        return 0
    print("已是最新版本")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

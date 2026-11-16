#!/usr/bin/env python3
"""提交前自检：仓库里不许出现真实的公网 IP、私钥或明显的凭据。

背景：README 的截图曾经带上过真实服务器 IP。文本可以用这个脚本卡住，
截图则只能在生成时就用 `examples/demo-servers.yaml` 里的假主机（见 README「截图规范」）。

用法（CI 和本地都能跑）：
    python scripts/check_no_secrets.py [--all-files]

默认只检查 git 跟踪的文本文件；--all-files 检查工作区里的同类文件。
退出码 0 = 干净，1 = 有问题（会打印文件、行号和命中内容）。
"""

from __future__ import annotations

import ipaddress
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 允许出现的地址：内网、回环、链路本地、文档保留段（RFC 5737）
ALLOWED_NETS = [
    ipaddress.ip_network(n)
    for n in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "192.0.2.0/24",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "0.0.0.0/32",
        "255.255.255.255/32",
    )
]

IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
SECRETS = [
    (re.compile(r"-----BEGIN (?:RSA|OPENSSH|EC|DSA|PGP) PRIVATE KEY-----"), "私钥"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}"), "GitHub token"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "GitHub token"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key"),
    (
        re.compile(
            r"(?i)\bpassword\s*:\s*[\"']?"
            r"(?!\*|env\b|file\b|DEMO|CHANGE|your|xxx|testpass|test|dummy|example)"
            r"[^\s\"'{}]{8,}"
        ),
        "明文密码",
    ),
]
TEXT_SUFFIXES = {
    ".py", ".md", ".yml", ".yaml", ".toml", ".txt", ".json", ".cfg", ".ini",
    ".ps1", ".cmd", ".bat", ".sh", ".html", ".css", ".js", ".gitignore", "",
}


def tracked_files() -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout
        return [ROOT / line for line in out.splitlines() if line.strip()]
    except Exception:  # noqa: BLE001 - 没有 git 时退回扫工作区
        return [p for p in ROOT.rglob("*") if p.is_file()]


def worktree_files() -> list[Path]:
    skip = {".git", ".venv", "build", "dist", "__pycache__", "node_modules", ".pytest_cache"}
    return [p for p in ROOT.rglob("*") if p.is_file() and not (skip & set(p.parts))]


def is_public(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not any(addr in net for net in ALLOWED_NETS)


def _rel(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def scan(paths: list[Path]) -> list[str]:
    problems: list[str] = []
    for path in paths:
        if not path.is_file():
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in ("LICENSE", "Makefile"):
            continue
        if path.name == "check_no_secrets.py":
            continue  # 本脚本自己就带着这些正则
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for match in IPV4.finditer(line):
                ip = match.group(1)
                # 版本号/时间戳之类会误报，只保留看起来像合法地址的
                if not all(0 <= int(part) <= 255 for part in ip.split(".")):
                    continue
                if is_public(ip):
                    problems.append(f"{_rel(path)}:{lineno}: 公网 IP {ip}  —— {line.strip()[:90]}")
            for pattern, label in SECRETS:
                if pattern.search(line):
                    problems.append(f"{_rel(path)}:{lineno}: 疑似{label} —— {line.strip()[:90]}")
    return problems


def main() -> int:
    use_all = "--all-files" in sys.argv
    paths = worktree_files() if use_all else tracked_files()
    problems = scan(paths)
    if problems:
        print(f"发现 {len(problems)} 处需要处理：\n")
        for item in problems:
            print("  " + item)
        print("\n提示：真实地址请写成 10.x / 192.168.x 或文档保留段 203.0.113.x；")
        print("      截图请用 examples/demo-servers.yaml 里的假主机重新拍。")
        return 1
    print(f"检查通过：{len(paths)} 个文件，没有公网 IP / 私钥 / 明文凭据")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""探测 servers.yaml 里每台主机的 SSH 是否真的能连上（只做只读探测）。

跑法：python -m tests.probe_hosts  或  python tests/probe_hosts.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import anyio  # noqa: E402

from ssh_mcp import server as srv  # noqa: E402


def main() -> int:
    cfg_path = ROOT / "servers.yaml"
    rt = srv.init_runtime(str(cfg_path))
    cfg = rt.cfg

    print(f"配置：{cfg.source}")
    print(f"主机：{len(cfg.hosts)}\n")

    rows: list[tuple[str, str, str, str, str]] = []

    async def probe_all() -> None:
        for alias, host in sorted(cfg.hosts.items()):
            if host.shell_kind == "posix":
                probe = "uname -a; id"
            elif host.shell_kind == "cmd":
                probe = "ver && whoami"
            else:  # powershell 5.1 不支持 &&
                probe = "Write-Output ('PowerShell ' + $PSVersionTable.PSVersion.ToString()); whoami; hostname"
            t0 = time.perf_counter()
            status = "OK"
            detail = ""
            try:
                res = await rt.pool.exec(alias, probe, timeout=15)
                ms = int((time.perf_counter() - t0) * 1000)
                if res.exit_code == 0:
                    detail = (res.stdout or res.stderr).strip().splitlines()[0][:70]
                else:
                    status = f"SSH通/命令{res.exit_code}"
                    detail = (res.stderr or res.stdout).strip().replace("\n", " ")[:70]
                detail = f"{ms}ms  {detail}"
            except Exception as exc:  # noqa: BLE001
                ms = int((time.perf_counter() - t0) * 1000)
                status = "FAIL"
                detail = f"{ms}ms  {type(exc).__name__}: {exc}"[:110]
            rows.append((alias, f"{host.host}:{host.port}", host.auth.method, status, detail))

    anyio.run(probe_all)

    width = max(len(r[0]) for r in rows) if rows else 4
    print(f"{'别名'.ljust(width)}  {'目标':<24} {'认证':<9} {'结果':<16} 说明")
    print("-" * 110)
    for alias, target, auth, status, detail in rows:
        print(f"{alias.ljust(width)}  {target:<24} {auth:<9} {status:<16} {detail}")

    ok = sum(1 for r in rows if r[3] == "OK")
    print("-" * 110)
    print(f"可达 {ok}/{len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

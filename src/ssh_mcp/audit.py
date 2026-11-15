"""审计日志：每次 SSH 动作落一行 JSONL，便于事后追查谁在哪台机跑了什么。"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from .config import Audit


class AuditLog:
    def __init__(self, cfg: Audit) -> None:
        self.cfg = cfg
        self._lock = threading.Lock()
        self._path = Path(cfg.path).expanduser() if cfg.enabled else None
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, **fields: Any) -> None:
        if not self.cfg.enabled or self._path is None:
            return
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event, **fields}
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        with self._lock:
            try:
                self._rotate()
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(line)
            except OSError:
                # 审计写不进去不该把业务请求打死，静默降级
                pass

    def _rotate(self) -> None:
        if not self.cfg.max_bytes or self._path is None:
            return
        try:
            if self._path.stat().st_size < self.cfg.max_bytes:
                return
            backup = self._path.with_suffix(self._path.suffix + ".1")
            if backup.exists():
                backup.unlink()
            os.replace(self._path, backup)
        except OSError:
            pass

    def tail(self, limit: int = 20) -> list[dict[str, Any]]:
        if self._path is None or not self._path.is_file():
            return []
        rows: list[dict[str, Any]] = []
        with self._path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return rows[-limit:]

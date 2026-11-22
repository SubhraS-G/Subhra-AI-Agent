"""Structured logging for agent operations.

Every session appends to logs/YYYY-MM-DD.log so that operation history is
auditable. Secrets are never written to logs.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

from system.paths import LOGS_DIR

_LEVELS = {"debug": 10, "info": 20, "warning": 30, "error": 40}
_MIN_LEVEL = _LEVELS["info"]


def _log_path() -> Path:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    return LOGS_DIR / f"{_dt.date.today().isoformat()}.log"


def log(event: str, **fields) -> None:
    """Append one structured JSON line to today's log."""
    record = {
        "ts": _dt.datetime.now().isoformat(timespec="seconds"),
        "event": event,
        **fields,
    }
    line = json.dumps(record, ensure_ascii=False)
    print(f"[log] {line}")
    with _log_path().open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def log_task(session: str, task: str, status: str, detail: str = "") -> None:
    log("task", session=session, task=task, status=status, detail=detail)

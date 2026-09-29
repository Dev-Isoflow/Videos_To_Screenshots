"""Per-session state, persisted as a plain session.json file inside each
session's data directory. No database — at this scale (one user at a time,
occasional use) a JSON file per session is simpler to run, inspect, and
debug than standing up SQLite or Postgres for v0.
"""

import json
from pathlib import Path
from typing import Any, Optional

SESSION_FILE = "session.json"


def create_session(session_dir: Path, **fields: Any) -> None:
    fields.setdefault("frames", [])
    _write(session_dir, fields)


def read_session(session_dir: Path) -> Optional[dict]:
    path = session_dir / SESSION_FILE
    if not path.exists():
        return None
    return json.loads(path.read_text())


def update_session(session_dir: Path, **fields: Any) -> dict:
    data = read_session(session_dir) or {}
    data.update(fields)
    _write(session_dir, data)
    return data


def _write(session_dir: Path, data: dict) -> None:
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / SESSION_FILE).write_text(json.dumps(data, indent=2))

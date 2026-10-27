"""Container health check.

Healthy means: SQLite is readable, the schema exists, and the scheduler has
written a heartbeat recently. A stale heartbeat catches a hung scan loop that
is still accepting signals.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    base = Path(__file__).resolve().parent.parent
    database_path = Path(os.environ.get("DATABASE_PATH") or base / "data" / "scanner.db")
    heartbeat_path = Path(
        os.environ.get("HEARTBEAT_PATH") or database_path.parent / "heartbeat.json"
    )
    max_age = _int_env("HEALTH_MAX_AGE_SECONDS", 900)

    if not database_path.exists():
        print(f"unhealthy: database not found at {database_path}", file=sys.stderr)
        return 1
    try:
        connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=5)
        try:
            version = connection.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        print(f"unhealthy: database error: {exc}", file=sys.stderr)
        return 1
    if version is None:
        print("unhealthy: schema is not initialized", file=sys.stderr)
        return 1

    if not heartbeat_path.exists():
        print(f"unhealthy: heartbeat not found at {heartbeat_path}", file=sys.stderr)
        return 1
    try:
        payload = json.loads(heartbeat_path.read_text(encoding="utf-8"))
        timestamp = datetime.fromisoformat(
            str(payload["updated_at"]).replace("Z", "+00:00")
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"unhealthy: heartbeat cannot be read: {exc}", file=sys.stderr)
        return 1
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - timestamp).total_seconds()
    if age > max_age:
        print(
            f"unhealthy: heartbeat is {int(age)}s old (limit {max_age}s)",
            file=sys.stderr,
        )
        return 1
    print(
        f"healthy: schema {version[0]}, heartbeat {int(age)}s old, "
        f"shadow={payload.get('shadow_mode')}"
    )
    return 0


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


if __name__ == "__main__":
    raise SystemExit(main())

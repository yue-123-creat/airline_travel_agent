"""SQLite stand-in for unit tests only. Never import from runtime app paths."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SqliteTestDatabase:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _init(self) -> None:
        with self.conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    meta TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                """
            )
            c.commit()

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path)
        try:
            yield c
        finally:
            c.close()

    def upsert_session(self, session_id: str, user_id: str) -> None:
        now = _now()
        with self.conn() as c:
            c.execute(
                """
                INSERT INTO sessions(session_id, user_id, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET updated_at=excluded.updated_at
                """,
                (session_id, user_id, now, now),
            )
            c.commit()

    def add_message(self, session_id: str, role: str, content: str, meta: dict | None = None) -> None:
        now = _now()
        with self.conn() as c:
            c.execute(
                "INSERT INTO messages(session_id, role, content, meta, created_at) VALUES (?,?,?,?,?)",
                (session_id, role, content, json.dumps(meta or {}), now),
            )
            c.commit()

    def list_messages(self, session_id: str, limit: int | None = None) -> list[dict[str, Any]]:
        with self.conn() as c:
            if limit:
                rows = c.execute(
                    """
                    SELECT role, content, meta, created_at FROM (
                      SELECT role, content, meta, created_at, id
                      FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?
                    ) t ORDER BY id
                    """,
                    (session_id, limit),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT role, content, meta, created_at FROM messages WHERE session_id=? ORDER BY id",
                    (session_id,),
                ).fetchall()
        return [
            {"role": r[0], "content": r[1], "meta": json.loads(r[2] or "{}"), "created_at": r[3]}
            for r in rows
        ]

from __future__ import annotations

import json
import logging
import threading
from contextlib import contextmanager
from typing import Any, Iterator

import psycopg

from app.config import Settings

logger = logging.getLogger(__name__)


class Database:
    """Runtime storage is Postgres only. SQLite is reserved for unit tests."""

    def __init__(self, settings: Settings):
        if not settings.database_url.startswith("postgresql"):
            raise RuntimeError(
                "DATABASE_URL must be postgresql://... for runtime. "
                "SQLite is only allowed in unit tests."
            )
        self.url = settings.database_url

    @contextmanager
    def conn(self) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self.url) as c:
            yield c

    def upsert_session(self, session_id: str, user_id: str) -> None:
        with self.conn() as c:
            c.execute(
                """
                INSERT INTO sessions(session_id, user_id)
                VALUES (%s, %s)
                ON CONFLICT (session_id) DO UPDATE SET updated_at = NOW()
                """,
                (session_id, user_id),
            )
            c.commit()

    def list_sessions(self, user_id: str) -> list[dict[str, Any]]:
        """Non-empty sessions only; preview = first user message (truncated)."""
        with self.conn() as c:
            rows = c.execute(
                """
                SELECT
                  s.session_id,
                  s.created_at,
                  s.updated_at,
                  (
                    SELECT m.content
                    FROM messages m
                    WHERE m.session_id = s.session_id AND m.role = 'user'
                    ORDER BY m.id ASC
                    LIMIT 1
                  ) AS preview
                FROM sessions s
                WHERE s.user_id = %s
                  AND EXISTS (
                    SELECT 1 FROM messages m WHERE m.session_id = s.session_id
                  )
                ORDER BY s.updated_at DESC
                """,
                (user_id,),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            raw = (r[3] or "").strip().replace("\n", " ")
            preview = raw if len(raw) <= 40 else raw[:40].rstrip() + "…"
            out.append(
                {
                    "session_id": r[0],
                    "created_at": r[1].isoformat(),
                    "updated_at": r[2].isoformat(),
                    "preview": preview,
                }
            )
        return out

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self.conn() as c:
            row = c.execute(
                "SELECT session_id, user_id, created_at, updated_at, stm_state FROM sessions WHERE session_id=%s",
                (session_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "session_id": row[0],
            "user_id": row[1],
            "created_at": row[2].isoformat(),
            "updated_at": row[3].isoformat(),
            "stm_state": row[4] or {},
        }

    def get_stm_state(self, session_id: str) -> dict[str, Any]:
        with self.conn() as c:
            row = c.execute(
                "SELECT stm_state FROM sessions WHERE session_id=%s",
                (session_id,),
            ).fetchone()
        if not row:
            return {"messages": [], "summary": "", "slots": {}}
        state = row[0] or {}
        if isinstance(state, str):
            state = json.loads(state)
        return {
            "messages": list(state.get("messages") or []),
            "summary": state.get("summary") or "",
            "slots": dict(state.get("slots") or {}),
        }

    def save_stm_state(self, session_id: str, state: dict[str, Any]) -> None:
        payload = {
            "messages": state.get("messages") or [],
            "summary": state.get("summary") or "",
            "slots": state.get("slots") or {},
        }
        with self.conn() as c:
            c.execute(
                """
                UPDATE sessions
                SET stm_state = %s::jsonb, updated_at = NOW()
                WHERE session_id = %s
                """,
                (json.dumps(payload, ensure_ascii=False), session_id),
            )
            c.commit()

    def add_message(self, session_id: str, role: str, content: str, meta: dict | None = None) -> None:
        with self.conn() as c:
            c.execute(
                "INSERT INTO messages(session_id, role, content, meta) VALUES (%s,%s,%s,%s::jsonb)",
                (session_id, role, content, json.dumps(meta or {})),
            )
            c.execute("UPDATE sessions SET updated_at=NOW() WHERE session_id=%s", (session_id,))
            c.commit()

    def list_messages(self, session_id: str) -> list[dict[str, Any]]:
        with self.conn() as c:
            rows = c.execute(
                "SELECT role, content, meta, created_at FROM messages WHERE session_id=%s ORDER BY id",
                (session_id,),
            ).fetchall()
        return [
            {"role": r[0], "content": r[1], "meta": r[2], "created_at": r[3].isoformat()}
            for r in rows
        ]

    def delete_session(self, session_id: str) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM sessions WHERE session_id=%s", (session_id,))
            c.commit()

    def _write_usage_sync(self, row: dict[str, Any]) -> None:
        with self.conn() as c:
            c.execute(
                """
                INSERT INTO usage_ledger(
                  request_id, reserve_id, user_id, session_id,
                  est_tokens, actual_tokens, quota_op, route, llm_calls, abnormal_tags
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                """,
                (
                    row.get("request_id"),
                    row.get("reserve_id"),
                    row.get("user_id"),
                    row.get("session_id"),
                    row.get("est_tokens", 0),
                    row.get("actual_tokens", 0),
                    row.get("quota_op", "unknown"),
                    row.get("route"),
                    row.get("llm_calls", 0),
                    json.dumps(row.get("abnormal_tags") or []),
                ),
            )
            c.commit()

    def write_usage_async(self, row: dict[str, Any]) -> None:
        """配额 Redis 已 commit 后，PG usage_ledger 后台写入，不挡回包。"""
        payload = dict(row)

        def _run() -> None:
            try:
                self._write_usage_sync(payload)
            except Exception as e:
                logger.warning("usage_ledger async write failed: %s", e)

        threading.Thread(target=_run, name="usage-ledger", daemon=True).start()

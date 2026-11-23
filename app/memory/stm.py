from __future__ import annotations

from typing import Any

from app.db import Database


class SessionStore:
    """
    Short-term memory backed by Postgres (sessions.stm_state).
    Process memory is only a cache; Docker/runtime must use Postgres.
    """

    def __init__(self, db: Database) -> None:
        self.db = db
        self._cache: dict[str, dict[str, Any]] = {}

    def ensure(self, session_id: str, user_id: str) -> dict[str, Any]:
        self.db.upsert_session(session_id, user_id)
        if session_id not in self._cache:
            state = self.db.get_stm_state(session_id)
            self._cache[session_id] = {
                "session_id": session_id,
                "user_id": user_id,
                "messages": list(state.get("messages") or []),
                "summary": state.get("summary") or "",
                "slots": dict(state.get("slots") or {}),
            }
        else:
            self._cache[session_id]["user_id"] = user_id
        return self._cache[session_id]

    def get(self, session_id: str) -> dict[str, Any] | None:
        if session_id in self._cache:
            return self._cache[session_id]
        sess = self.db.get_session(session_id)
        if not sess:
            return None
        state = self.db.get_stm_state(session_id)
        entry = {
            "session_id": session_id,
            "user_id": sess["user_id"],
            "messages": list(state.get("messages") or []),
            "summary": state.get("summary") or "",
            "slots": dict(state.get("slots") or {}),
        }
        self._cache[session_id] = entry
        return entry

    def _persist(self, session_id: str) -> None:
        entry = self._cache[session_id]
        self.db.save_stm_state(
            session_id,
            {
                "messages": entry.get("messages") or [],
                "summary": entry.get("summary") or "",
                "slots": entry.get("slots") or {},
            },
        )

    def append_message(self, session_id: str, role: str, content: str) -> None:
        sess = self._cache[session_id]
        sess["messages"].append({"role": role, "content": content})
        self._persist(session_id)

    def window(self, session_id: str, n: int = 8) -> list[dict[str, str]]:
        """n = 轮数（user+assistant 一对）；返回最近 2n 条 message。"""
        from app.memory.context import stm_recent_messages

        sess = self._cache[session_id]
        return stm_recent_messages(list(sess["messages"] or []), turns=n)

    def save_slots(self, session_id: str) -> None:
        self._persist(session_id)

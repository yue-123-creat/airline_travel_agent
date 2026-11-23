"""Prove SQLite helper is for tests; runtime Database rejects non-postgres URLs."""

from pathlib import Path

import pytest

from app.config import Settings
from app.db import Database
from tests.support.sqlite_db import SqliteTestDatabase


def test_runtime_database_rejects_sqlite(tmp_path: Path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'x.db'}")
    with pytest.raises(RuntimeError, match="postgresql"):
        Database(settings)


def test_sqlite_helper_roundtrip(tmp_path: Path):
    db = SqliteTestDatabase(tmp_path / "unit.db")
    db.upsert_session("s1", "u1")
    db.add_message("s1", "user", "hi")
    db.add_message("s1", "assistant", "hello")
    rows = db.list_messages("s1", limit=10)
    assert len(rows) == 2
    assert rows[0]["content"] == "hi"

"""
Unit-test only SQLite helper.

Runtime (Docker / local server) MUST use Postgres via app.db.Database.
This file intentionally does NOT import app.db.Database for writes.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def test_sqlite_scratch_for_unit_isolation(tmp_path: Path):
    db_path = tmp_path / "unit.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO t(v) VALUES ('ok')")
    conn.commit()
    row = conn.execute("SELECT v FROM t").fetchone()
    conn.close()
    assert row[0] == "ok"

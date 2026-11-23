from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class Telemetry:
    def __init__(self, data_dir: str):
        self.dir = Path(data_dir) / "logs"
        self.dir.mkdir(parents=True, exist_ok=True)

    def emit(self, event: dict[str, Any]) -> None:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = self.dir / f"{day}.jsonl"
        event = {"ts": datetime.now(timezone.utc).isoformat(), **event}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def tail(self, n: int = 50) -> list[dict[str, Any]]:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = self.dir / f"{day}.jsonl"
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
        out = []
        for line in lines[-n:]:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

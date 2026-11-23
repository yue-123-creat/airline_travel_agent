from __future__ import annotations

import json
import threading
from pathlib import Path

from app.models import MemoryItem

WHITELIST_TYPES = {"preference", "companion", "forbidden", "frequent_city", "frequent_airport"}
SENSITIVE_KEYS = {"id_card", "passport", "password", "pay", "bank"}


class LtmVersionConflict(Exception):
    pass


class MockLtmAdapter:
    """Mode A: incremental patch + optimistic version. Hard gate before write."""

    def __init__(self, data_dir: str):
        self.path = Path(data_dir) / "ltm_mock.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        if not self.path.exists():
            self.path.write_text("{}", encoding="utf-8")

    def _load(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8") or "{}")

    def _save(self, data: dict) -> None:
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def get(self, user_id: str) -> dict:
        with self._lock:
            data = self._load()
            entry = data.get(user_id) or {"version": 0, "items": {}}
            items = [
                {"type": v["type"], "key": k, "value": v["value"], "confidence": v["confidence"]}
                for k, v in entry.get("items", {}).items()
            ]
            return {"user_id": user_id, "version": entry.get("version", 0), "items": items}

    def filter_patch(self, patch: list[MemoryItem]) -> tuple[list[MemoryItem], list[str]]:
        kept: list[MemoryItem] = []
        tags: list[str] = []
        for item in patch:
            if item.type not in WHITELIST_TYPES:
                tags.append("ltm_filtered_whitelist")
                continue
            if any(s in item.key.lower() for s in SENSITIVE_KEYS):
                tags.append("ltm_filtered_sensitive")
                continue
            if item.confidence < 0.7:
                continue
            kept.append(item)
        return kept, tags

    def patch(self, user_id: str, version: int, patch: list[MemoryItem]) -> dict:
        kept, _ = self.filter_patch(patch)
        with self._lock:
            data = self._load()
            entry = data.get(user_id) or {"version": 0, "items": {}}
            if int(entry.get("version", 0)) != version:
                raise LtmVersionConflict(f"expected {entry.get('version')}, got {version}")
            items = dict(entry.get("items") or {})
            for item in kept:
                prev = items.get(item.key)
                if prev is None or item.confidence >= float(prev.get("confidence", 0)):
                    items[item.key] = {
                        "type": item.type,
                        "value": item.value,
                        "confidence": item.confidence,
                    }
            entry["items"] = items
            entry["version"] = version + 1
            data[user_id] = entry
            self._save(data)
            # Do not call get() while holding Lock (non-reentrant).
            out_items = [
                {"type": v["type"], "key": k, "value": v["value"], "confidence": v["confidence"]}
                for k, v in items.items()
            ]
            return {"user_id": user_id, "version": entry["version"], "items": out_items}

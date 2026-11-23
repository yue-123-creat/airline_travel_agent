#!/usr/bin/env python3
"""Create travel_rag + aviation_rag collections if missing."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env", override=True)

from app.config import get_settings
from app.rag.collections import ensure_collections


def main() -> None:
    get_settings.cache_clear()
    s = get_settings()
    print(f"qdrant_url={s.qdrant_url}")
    print(f"vector_size={s.qdrant_vector_size}")
    result = ensure_collections(s)
    for name, status in result.items():
        print(f"  {name}: {status}")


if __name__ == "__main__":
    main()

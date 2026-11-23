"""P1 smoke: structured guide without coordinates."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from app.config import get_settings

get_settings.cache_clear()

from app.llm.qwen import QwenClient
from app.tools.amap import amap_route_by_coords
from app.travel.knowledge import search_attractions
from app.travel.service import answer_travel


def test_sql() -> None:
    hits = search_attractions("成都亲子游推荐", limit=5, source="enriched")
    print("sql_hits", len(hits), [h.get("name") for h in hits])
    assert hits
    print("PASS sql")


def test_route() -> None:
    r = amap_route_by_coords(
        origin_lng=121.490317,
        origin_lat=31.242176,
        dest_lng=121.541358,
        dest_lat=31.218114,
        mode="driving",
        origin_name="外滩",
        dest_name="上海科技馆",
    )
    print("route", json.dumps(r, ensure_ascii=False))
    assert r.get("ok") and r.get("approx_time")
    print("PASS route")


async def test_guide() -> None:
    qwen = QwenClient(get_settings())
    if not qwen.configured:
        print("SKIP guide")
        return
    result = await answer_travel("上海一日游行程怎么安排", source="enriched", qwen=qwen)
    print("flags", result.domain_flags)
    # write utf-8 snippet for inspection
    out = ROOT / "data" / "_smoke_guide_snippet.txt"
    out.write_text(result.answer, encoding="utf-8")
    assert "【上午】" in result.answer
    assert "【下午】" in result.answer
    assert "【交通】" in result.answer
    assert "【注意】" in result.answer
    low = result.answer.lower()
    assert "lng" not in low and "lat" not in low
    assert "经度" not in result.answer and "纬度" not in result.answer
    assert "structured_itinerary" in result.domain_flags
    print("PASS guide structure; snippet ->", out)


def main() -> None:
    test_sql()
    test_route()
    asyncio.run(test_guide())
    print("done")


if __name__ == "__main__":
    main()

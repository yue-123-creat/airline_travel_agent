from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from app.config import get_settings
from app.travel.pg_search import count_attraction_kb, search_attraction_kb
from app.travel.query_intent import parse_travel_intent

KnowledgeSource = Literal["legacy", "enriched"]

_STOP = re.compile(
    r"我想去|想去|帮我|请问|一下|有哪些|哪里好玩|怎么玩|好玩吗|值得去吗|"
    r"景点|景区|旅游|旅行|攻略|推荐|介绍|路线|安排|情侣|亲子|美食|拍照|省力|轻松|慢游"
)


def _data_dir() -> Path:
    return Path(get_settings().data_dir)


def _legacy_path() -> Path:
    return _data_dir() / "knowledge" / "travel_knowledge.json"


def _enriched_path() -> Path:
    return _data_dir() / "knowledge" / "attractions_enriched.json"


@lru_cache(maxsize=1)
def load_legacy_knowledge() -> dict[str, Any]:
    path = _legacy_path()
    if not path.exists():
        return {"attractions": [], "regions": []}
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def load_enriched_attractions() -> list[dict[str, Any]]:
    """File fallback only — formal enriched path uses Postgres."""
    path = _enriched_path()
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    return list(data.get("attractions") or [])


def reload_knowledge_caches() -> None:
    """数据文件更新后清空缓存，供脚本/热更调用。"""
    load_legacy_knowledge.cache_clear()
    load_enriched_attractions.cache_clear()


def extract_tokens(query: str) -> list[str]:
    cleaned = _STOP.sub(" ", query or "")
    return list(dict.fromkeys(re.findall(r"[\u4e00-\u9fa5A-Za-z0-9]{2,}", cleaned)))


def _score_attraction(item: dict[str, Any], query: str) -> int:
    content = item.get("content") if isinstance(item.get("content"), dict) else {}
    location = item.get("location") if isinstance(item.get("location"), dict) else {}
    text = " ".join(
        [
            str(item.get("name") or ""),
            " ".join(item.get("aliases") or []),
            str(item.get("city") or ""),
            str(item.get("region") or ""),
            str(item.get("category") or ""),
            " ".join(item.get("tags") or []),
            str(item.get("summary") or ""),
            str(item.get("intro") or ""),
            str(content.get("intro") or ""),
            str(content.get("tips") or ""),
            str(location.get("address") or ""),
        ]
    )
    score = 0
    name = item.get("name") or ""
    if name and name in query:
        score += 120
    aliases = item.get("aliases") or []
    if any(alias in query or query in alias for alias in aliases):
        score += 100
    city = item.get("city") or ""
    if city and city in query:
        score += 34
    region = item.get("region") or ""
    if region and region in query:
        score += 20
    category = item.get("category") or ""
    if category and category in query:
        score += 12
    for tag in item.get("tags") or []:
        if tag and tag in query:
            score += 10
    for token in extract_tokens(query):
        if token in text:
            score += 16 if len(token) >= 4 else 8
    return score


def _public_attraction(item: dict[str, Any]) -> dict[str, Any]:
    """对外返回时去掉 _meta 等内部字段。"""
    return {k: v for k, v in item.items() if not str(k).startswith("_")}


def _search_file_enriched(query: str, limit: int) -> list[dict[str, Any]]:
    items = load_enriched_attractions()
    scored: list[dict[str, Any]] = []
    for item in items:
        score = _score_attraction(item, query)
        if score > 0:
            hit = _public_attraction(item)
            hit["score"] = score
            scored.append(hit)
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:limit]


def search_attractions(
    query: str,
    limit: int = 6,
    *,
    source: KnowledgeSource = "legacy",
) -> list[dict[str, Any]]:
    if source == "enriched":
        # 正式路径：Postgres；仅连接/SQL 失败时退回文件（空结果不回退，避免冲掉严格过滤）
        intent = parse_travel_intent(query)
        hits = search_attraction_kb(query, limit=limit, intent=intent)
        if hits is None:
            return _search_file_enriched(query, limit)
        return [_public_attraction(h) for h in hits]

    items = list((load_legacy_knowledge().get("attractions") or []))
    scored: list[dict[str, Any]] = []
    for item in items:
        score = _score_attraction(item, query)
        if score > 0:
            hit = _public_attraction(item)
            hit["score"] = score
            scored.append(hit)
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:limit]


def search_regions(query: str, limit: int = 3) -> list[dict[str, Any]]:
    """地区概览仅旧库有；正式 enriched 路径不读。"""
    data = load_legacy_knowledge()
    tokens = extract_tokens(query)
    hits = []
    for region in data.get("regions") or []:
        name = str(region.get("region") or "")
        score = 0
        if name and name in query:
            score += 80
        for token in tokens:
            if token and token in name:
                score += 20
            for attr in region.get("attractions") or []:
                if token in str(attr.get("name") or "") or token in str(attr.get("city") or ""):
                    score += 8
        if score > 0:
            hits.append({**region, "score": score})
    hits.sort(key=lambda x: x["score"], reverse=True)
    return hits[:limit]


def knowledge_stats(source: KnowledgeSource) -> dict[str, Any]:
    if source == "enriched":
        db_count = count_attraction_kb()
        file_count = len(load_enriched_attractions())
        return {
            "source": source,
            "backend": "postgres" if db_count is not None else "file_fallback",
            "path": "attraction_kb" if db_count is not None else str(_enriched_path()),
            "attraction_count": db_count if db_count is not None else file_count,
            "file_attraction_count": file_count,
            "region_count": 0,
            "formal_reads_legacy_table": False,
        }
    data = load_legacy_knowledge()
    return {
        "source": source,
        "backend": "file",
        "path": str(_legacy_path()),
        "attraction_count": len(data.get("attractions") or []),
        "region_count": len(data.get("regions") or []),
        "note": "研发对比接口；正式 chat/enriched 不读此源",
    }

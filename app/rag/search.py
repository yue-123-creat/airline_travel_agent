"""Qdrant secondary retrieval with chunk_id anchors + degrade path."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from app.config import Settings, get_settings
from app.rag.collections import AVIATION_COLLECTION, TRAVEL_COLLECTION, get_qdrant_client
from app.rag.embed import EmbedError, embed_query

logger = logging.getLogger(__name__)

DegradeMode = Literal["anchored", "metadata", "empty", "error"]


@dataclass
class RagHit:
    chunk_id: str
    score: float
    text: str = ""
    city: str = ""
    spot_name: str = ""
    airport_code: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class RagSearchResult:
    hits: list[RagHit]
    mode: DegradeMode
    collection: str
    message: str = ""


def _payload_text(payload: dict[str, Any]) -> str:
    for key in ("text", "content", "body", "chunk_text"):
        v = payload.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _hit_from_point(point: Any) -> RagHit:
    payload = dict(getattr(point, "payload", None) or {})
    return RagHit(
        chunk_id=str(payload.get("chunk_id") or ""),
        score=float(getattr(point, "score", 0.0) or 0.0),
        text=_payload_text(payload),
        city=str(payload.get("city") or ""),
        spot_name=str(payload.get("spot_name") or ""),
        airport_code=str(payload.get("airport_code") or ""),
        payload=payload,
    )


def _anchored_filter(chunk_ids: list[str]):
    from qdrant_client.http import models as qm

    ids = [str(x).strip() for x in chunk_ids if str(x).strip()]
    return qm.Filter(
        must=[
            qm.FieldCondition(
                key="chunk_id",
                match=qm.MatchAny(any=ids),
            )
        ]
    )


def _metadata_filter(*, city: str = "", spot_name: str = "", airport_code: str = ""):
    from qdrant_client.http import models as qm

    must: list[Any] = []
    if spot_name.strip():
        must.append(
            qm.FieldCondition(key="spot_name", match=qm.MatchValue(value=spot_name.strip()))
        )
    if city.strip():
        must.append(qm.FieldCondition(key="city", match=qm.MatchValue(value=city.strip())))
    if airport_code.strip():
        must.append(
            qm.FieldCondition(
                key="airport_code",
                match=qm.MatchValue(value=airport_code.strip().upper()),
            )
        )
    if not must:
        return None
    return qm.Filter(must=must)


def _search_collection(
    *,
    collection: str,
    query: str,
    query_filter: Any | None,
    top_k: int,
    settings: Settings,
) -> list[RagHit]:
    vector = embed_query(query, settings=settings)
    client = get_qdrant_client(settings)
    # qdrant-client >=1.10: query_points；兼容 search
    try:
        res = client.query_points(
            collection_name=collection,
            query=vector,
            query_filter=query_filter,
            limit=top_k,
            with_payload=True,
        )
        points = list(res.points or [])
    except Exception:
        points = client.search(
            collection_name=collection,
            query_vector=vector,
            query_filter=query_filter,
            limit=top_k,
            with_payload=True,
        )
    return [_hit_from_point(p) for p in points]


def search_travel_rag(
    query: str,
    *,
    guide_chunk_ids: list[str] | None = None,
    city: str = "",
    spot_name: str = "",
    settings: Settings | None = None,
) -> RagSearchResult:
    """Travel secondary RAG.

    1) guide_chunk_ids 非空 → MatchAny(chunk_id) + top_k=anchored（默认 3）
    2) 空数组 → **禁止**空过滤器；降级 city / spot_name metadata
    3) 仍无 → empty，提示暂无详细游记
    """
    s = settings or get_settings()
    collection = (s.qdrant_travel_collection or TRAVEL_COLLECTION).strip() or TRAVEL_COLLECTION
    ids = [str(x).strip() for x in (guide_chunk_ids or []) if str(x).strip()]
    q = (query or "").strip()
    if not q:
        return RagSearchResult(hits=[], mode="empty", collection=collection, message="empty_query")

    try:
        if ids:
            hits = _search_collection(
                collection=collection,
                query=q,
                query_filter=_anchored_filter(ids),
                top_k=max(1, int(s.rag_top_k_anchored or 3)),
                settings=s,
            )
            if hits:
                return RagSearchResult(hits=hits, mode="anchored", collection=collection)
            # 锚点命中集合内无相似切片 → 仍算锚定路径空结果，可再降级 metadata
            meta_f = _metadata_filter(city=city, spot_name=spot_name)
            if meta_f is not None:
                hits = _search_collection(
                    collection=collection,
                    query=q,
                    query_filter=meta_f,
                    top_k=max(1, int(s.rag_top_k_degraded or 5)),
                    settings=s,
                )
                if hits:
                    return RagSearchResult(
                        hits=hits,
                        mode="metadata",
                        collection=collection,
                        message="anchored_empty_fallback_metadata",
                    )
            return RagSearchResult(
                hits=[],
                mode="empty",
                collection=collection,
                message="暂无详细游记",
            )

        # guide_chunk_ids == []：绝不带空 MatchAny
        meta_f = _metadata_filter(city=city, spot_name=spot_name)
        if meta_f is None:
            return RagSearchResult(
                hits=[],
                mode="empty",
                collection=collection,
                message="暂无详细游记",
            )
        hits = _search_collection(
            collection=collection,
            query=q,
            query_filter=meta_f,
            top_k=max(1, int(s.rag_top_k_degraded or 5)),
            settings=s,
        )
        if hits:
            return RagSearchResult(hits=hits, mode="metadata", collection=collection)
        return RagSearchResult(
            hits=[],
            mode="empty",
            collection=collection,
            message="暂无详细游记",
        )
    except EmbedError as e:
        logger.warning("travel rag embed failed: %s", e)
        return RagSearchResult(
            hits=[], mode="error", collection=collection, message=f"embed_error:{e}"
        )
    except Exception as e:
        logger.warning("travel rag search failed: %s", e)
        return RagSearchResult(
            hits=[], mode="error", collection=collection, message=f"search_error:{e}"
        )


def search_aviation_rag(
    query: str,
    *,
    guide_chunk_ids: list[str] | None = None,
    airport_code: str = "",
    settings: Settings | None = None,
) -> RagSearchResult:
    """Aviation secondary RAG（与 travel 同规则，metadata 用 airport_code）。"""
    s = settings or get_settings()
    collection = (s.qdrant_aviation_collection or AVIATION_COLLECTION).strip() or AVIATION_COLLECTION
    ids = [str(x).strip() for x in (guide_chunk_ids or []) if str(x).strip()]
    q = (query or "").strip()
    if not q:
        return RagSearchResult(hits=[], mode="empty", collection=collection, message="empty_query")

    try:
        if ids:
            hits = _search_collection(
                collection=collection,
                query=q,
                query_filter=_anchored_filter(ids),
                top_k=max(1, int(s.rag_top_k_anchored or 3)),
                settings=s,
            )
            if hits:
                return RagSearchResult(hits=hits, mode="anchored", collection=collection)
            meta_f = _metadata_filter(airport_code=airport_code)
            if meta_f is not None:
                hits = _search_collection(
                    collection=collection,
                    query=q,
                    query_filter=meta_f,
                    top_k=max(1, int(s.rag_top_k_degraded or 5)),
                    settings=s,
                )
                if hits:
                    return RagSearchResult(
                        hits=hits,
                        mode="metadata",
                        collection=collection,
                        message="anchored_empty_fallback_metadata",
                    )
            return RagSearchResult(
                hits=[], mode="empty", collection=collection, message="暂无详细资料"
            )

        meta_f = _metadata_filter(airport_code=airport_code)
        if meta_f is None:
            return RagSearchResult(
                hits=[], mode="empty", collection=collection, message="暂无详细资料"
            )
        hits = _search_collection(
            collection=collection,
            query=q,
            query_filter=meta_f,
            top_k=max(1, int(s.rag_top_k_degraded or 5)),
            settings=s,
        )
        if hits:
            return RagSearchResult(hits=hits, mode="metadata", collection=collection)
        return RagSearchResult(
            hits=[], mode="empty", collection=collection, message="暂无详细资料"
        )
    except EmbedError as e:
        logger.warning("aviation rag embed failed: %s", e)
        return RagSearchResult(
            hits=[], mode="error", collection=collection, message=f"embed_error:{e}"
        )
    except Exception as e:
        logger.warning("aviation rag search failed: %s", e)
        return RagSearchResult(
            hits=[], mode="error", collection=collection, message=f"search_error:{e}"
        )

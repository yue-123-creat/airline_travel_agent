"""Upsert travel/aviation chunks into Qdrant."""

from __future__ import annotations

import logging
from typing import Any, Iterable

from app.config import Settings, get_settings
from app.rag.chunking import TextChunk
from app.rag.collections import (
    AVIATION_COLLECTION,
    TRAVEL_COLLECTION,
    ensure_collections,
    get_qdrant_client,
)
from app.rag.embed import embed_texts
from app.rag.ids import point_id_for_chunk

logger = logging.getLogger(__name__)


def _payload_for_chunk(
    chunk: TextChunk,
    *,
    domain: str = "travel",
    airport_code: str = "",
) -> dict[str, Any]:
    return {
        "chunk_id": chunk.chunk_id,
        "text": chunk.text,
        "city": chunk.city or "",
        "spot_name": chunk.spot_name or "",
        "kb_id": chunk.kb_id or "",
        "place_slug": chunk.place_slug,
        "topic": chunk.topic,
        "domain": domain,
        "airport_code": (airport_code or "").strip().upper(),
    }


def _upsert_chunks(
    chunks: Iterable[TextChunk],
    *,
    collection: str,
    domain: str,
    airport_code: str = "",
    settings: Settings | None = None,
    batch_size: int = 16,
    ensure: bool = True,
) -> dict[str, int]:
    s = settings or get_settings()
    items = [c for c in chunks if (c.text or "").strip() and c.chunk_id]
    if not items:
        return {"chunks": 0, "upserted": 0, "batches": 0}

    if ensure:
        ensure_collections(s)

    client = get_qdrant_client(s)
    batch_size = max(1, int(batch_size))
    upserted = 0
    batches = 0

    from qdrant_client.http import models as qm

    for i in range(0, len(items), batch_size):
        batch = items[i : i + batch_size]
        vectors = embed_texts([c.text for c in batch], settings=s)
        if len(vectors) != len(batch):
            raise RuntimeError("embed batch size mismatch")
        points = [
            qm.PointStruct(
                id=point_id_for_chunk(c.chunk_id),
                vector=vec,
                payload=_payload_for_chunk(c, domain=domain, airport_code=airport_code),
            )
            for c, vec in zip(batch, vectors)
        ]
        client.upsert(collection_name=collection, points=points)
        upserted += len(points)
        batches += 1
        logger.info("upserted %s points into %s (batch %s)", len(points), collection, batches)

    return {"chunks": len(items), "upserted": upserted, "batches": batches}


def upsert_travel_chunks(
    chunks: Iterable[TextChunk],
    *,
    settings: Settings | None = None,
    batch_size: int = 16,
    ensure: bool = True,
) -> dict[str, int]:
    """Embed + upsert into travel_rag. Returns counts."""
    s = settings or get_settings()
    collection = (s.qdrant_travel_collection or TRAVEL_COLLECTION).strip() or TRAVEL_COLLECTION
    return _upsert_chunks(
        chunks,
        collection=collection,
        domain="travel",
        settings=s,
        batch_size=batch_size,
        ensure=ensure,
    )


def upsert_aviation_chunks(
    chunks: Iterable[TextChunk],
    *,
    airport_code: str,
    settings: Settings | None = None,
    batch_size: int = 16,
    ensure: bool = True,
) -> dict[str, int]:
    """Embed + upsert into aviation_rag（payload.airport_code 必填便于降级过滤）。"""
    s = settings or get_settings()
    collection = (s.qdrant_aviation_collection or AVIATION_COLLECTION).strip() or AVIATION_COLLECTION
    code = (airport_code or "").strip().upper()
    return _upsert_chunks(
        chunks,
        collection=collection,
        domain="aviation",
        airport_code=code,
        settings=s,
        batch_size=batch_size,
        ensure=ensure,
    )

"""Ensure travel_rag / aviation_rag collections exist (isolated)."""

from __future__ import annotations

import logging
from typing import Any

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

TRAVEL_COLLECTION = "travel_rag"
AVIATION_COLLECTION = "aviation_rag"


def get_qdrant_client(settings: Settings | None = None):
    from qdrant_client import QdrantClient

    s = settings or get_settings()
    kwargs: dict[str, Any] = {"url": (s.qdrant_url or "http://127.0.0.1:6333").rstrip("/")}
    key = (s.qdrant_api_key or "").strip()
    if key:
        kwargs["api_key"] = key
    # 本地 compose 镜像与 pip 次版本可能不一致；跳过严格兼容检查
    return QdrantClient(**kwargs, timeout=30, check_compatibility=False)


def ensure_collections(settings: Settings | None = None) -> dict[str, str]:
    """Create travel_rag + aviation_rag if missing. Idempotent."""
    from qdrant_client.http import models as qm

    s = settings or get_settings()
    client = get_qdrant_client(s)
    size = int(s.qdrant_vector_size or 1024)
    travel = (s.qdrant_travel_collection or TRAVEL_COLLECTION).strip() or TRAVEL_COLLECTION
    aviation = (s.qdrant_aviation_collection or AVIATION_COLLECTION).strip() or AVIATION_COLLECTION

    existing = {c.name for c in client.get_collections().collections}

    def _create(name: str) -> str:
        if name in existing:
            return "exists"
        client.create_collection(
            collection_name=name,
            vectors_config=qm.VectorParams(size=size, distance=qm.Distance.COSINE),
        )
        # payload indexes for filter / degrade
        for field, schema in (
            ("chunk_id", qm.PayloadSchemaType.KEYWORD),
            ("city", qm.PayloadSchemaType.KEYWORD),
            ("spot_name", qm.PayloadSchemaType.KEYWORD),
            ("airport_code", qm.PayloadSchemaType.KEYWORD),
            ("domain", qm.PayloadSchemaType.KEYWORD),
        ):
            try:
                client.create_payload_index(
                    collection_name=name,
                    field_name=field,
                    field_schema=schema,
                )
            except Exception as e:
                logger.debug("payload index %s.%s: %s", name, field, e)
        return "created"

    return {
        travel: _create(travel),
        aviation: _create(aviation),
    }

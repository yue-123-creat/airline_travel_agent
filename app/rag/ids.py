"""Map string chunk_id ↔ Qdrant Point ID.

Qdrant Point ID 仅支持 UUID / uint；业务 chunk_id 为字符串时：
- Point ID = uuid5(namespace, chunk_id)
- payload.chunk_id = 原字符串（MatchAny 过滤用这个字段）
"""

from __future__ import annotations

import uuid

# 稳定命名空间：airline_travel_agent / rag chunk
CHUNK_ID_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


def point_id_for_chunk(chunk_id: str) -> str:
    """Return UUID string usable as Qdrant point id."""
    cid = (chunk_id or "").strip()
    if not cid:
        raise ValueError("chunk_id empty")
    return str(uuid.uuid5(CHUNK_ID_NAMESPACE, cid))

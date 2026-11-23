"""DashScope-compatible embeddings for RAG queries / upserts."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


class EmbedError(RuntimeError):
    pass


def embed_texts(texts: list[str], *, settings: Settings | None = None) -> list[list[float]]:
    """Embed a batch of texts. Empty input → []."""
    cleaned = [str(t or "").strip() for t in texts]
    cleaned = [t for t in cleaned if t]
    if not cleaned:
        return []

    s = settings or get_settings()
    key = (s.dashscope_api_key or "").strip()
    if not key:
        raise EmbedError("DASHSCOPE_API_KEY missing")

    base = (s.qwen_base_url or "").rstrip("/")
    url = f"{base}/embeddings"
    payload: dict[str, Any] = {
        "model": s.qwen_embed_model or "text-embedding-v3",
        "input": cleaned,
        "dimensions": int(s.qdrant_vector_size or 1024),
        "encoding_format": "float",
    }
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                url,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=payload,
            )
            data = resp.json()
    except Exception as e:
        raise EmbedError(f"embed request failed: {e}") from e

    if resp.status_code >= 400:
        raise EmbedError(f"embed HTTP {resp.status_code}: {data}")

    items = data.get("data") or []
    # API may return unsorted indices
    by_idx = {int(it.get("index", i)): it.get("embedding") for i, it in enumerate(items)}
    out: list[list[float]] = []
    for i in range(len(cleaned)):
        vec = by_idx.get(i)
        if not isinstance(vec, list) or not vec:
            raise EmbedError(f"missing embedding for index {i}")
        out.append([float(x) for x in vec])
    return out


def embed_query(text: str, *, settings: Settings | None = None) -> list[float]:
    vecs = embed_texts([text], settings=settings)
    if not vecs:
        raise EmbedError("empty query")
    return vecs[0]

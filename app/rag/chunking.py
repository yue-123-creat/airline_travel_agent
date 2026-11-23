"""Travel / aviation text chunking + stable chunk_id numbering.

ID format: chunk_{place_slug}_{nn}_{topic}
Example: chunk_djy_01_visitor_guide
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SECTION_TO_TOPIC: dict[str, str] = {
    "交通": "transport",
    "住宿": "lodging",
    "美食": "food",
    "小贴士": "tips",
    "游记": "visitor_guide",
    "攻略": "visitor_guide",
    "概要": "visitor_guide",
    "visitor_guide": "visitor_guide",
}

TOPIC_ORDER: tuple[str, ...] = (
    "visitor_guide",
    "transport",
    "lodging",
    "food",
    "tips",
)


@dataclass(frozen=True)
class TextChunk:
    chunk_id: str
    place_slug: str
    topic: str
    seq: int
    text: str
    spot_name: str = ""
    city: str = ""
    kb_id: str = ""


def place_slug_from_kb_id(kb_id: str) -> str:
    """spot_cd_djy_005 → djy；fallback 末段前的 name 缩写。"""
    parts = [p for p in str(kb_id or "").strip().split("_") if p]
    if len(parts) >= 4 and parts[0] == "spot":
        # spot_{city}_{name}_{nnn}
        return parts[2].lower()
    if len(parts) >= 3 and parts[0] == "spot":
        return parts[1].lower()
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "", kb_id or "").lower()
    return cleaned[:12] or "spot"


def topic_for_section(section: str) -> str:
    key = (section or "").strip()
    if key in SECTION_TO_TOPIC:
        return SECTION_TO_TOPIC[key]
    low = key.lower().replace(" ", "_")
    if low in SECTION_TO_TOPIC.values():
        return low
    return re.sub(r"[^a-z0-9_]+", "_", low).strip("_") or "visitor_guide"


def split_with_overlap(text: str, *, size: int, overlap: int) -> list[str]:
    """Character-window split; keeps short texts as a single piece."""
    body = (text or "").strip()
    if not body:
        return []
    size = max(64, int(size))
    overlap = max(0, min(int(overlap), size // 2))
    if len(body) <= size:
        return [body]
    out: list[str] = []
    start = 0
    n = len(body)
    while start < n:
        end = min(n, start + size)
        piece = body[start:end].strip()
        if piece:
            out.append(piece)
        if end >= n:
            break
        start = max(0, end - overlap)
        if start >= end:
            start = end
    return out


def build_chunks_for_spot(
    *,
    place_slug: str,
    spot_name: str,
    city: str,
    kb_id: str,
    sections: dict[str, str],
    chunk_size: int = 512,
    chunk_overlap: int = 64,
    include_merged_visitor_guide: bool = True,
) -> list[TextChunk]:
    """Build ordered chunks for one KB-linked spot.

    sections keys: 交通/住宿/美食/小贴士 or already-normalized topic keys.
    """
    slug = re.sub(r"[^a-z0-9]+", "", (place_slug or "").lower()) or "spot"
    prepared: list[tuple[str, str]] = []

    if include_merged_visitor_guide:
        merged_parts = []
        for label in ("交通", "住宿", "美食", "小贴士"):
            t = (sections.get(label) or "").strip()
            if t:
                merged_parts.append(f"{label}：{t}")
        if not merged_parts:
            for topic in TOPIC_ORDER:
                if topic == "visitor_guide":
                    continue
                t = (sections.get(topic) or "").strip()
                if t:
                    merged_parts.append(t)
        merged = "\n".join(merged_parts).strip()
        if merged:
            prepared.append(("visitor_guide", merged))

    # Prefer Chinese section headers then topic keys
    seen_topics: set[str] = {"visitor_guide"} if prepared else set()
    for label, topic in (
        ("交通", "transport"),
        ("住宿", "lodging"),
        ("美食", "food"),
        ("小贴士", "tips"),
    ):
        text = (sections.get(label) or sections.get(topic) or "").strip()
        if text:
            prepared.append((topic, text))
            seen_topics.add(topic)

    for key, text in sections.items():
        topic = topic_for_section(key)
        if topic in seen_topics:
            continue
        body = (text or "").strip()
        if body:
            prepared.append((topic, body))
            seen_topics.add(topic)

    chunks: list[TextChunk] = []
    seq = 0
    for topic, text in prepared:
        for piece in split_with_overlap(text, size=chunk_size, overlap=chunk_overlap):
            seq += 1
            chunk_id = f"chunk_{slug}_{seq:02d}_{topic}"
            chunks.append(
                TextChunk(
                    chunk_id=chunk_id,
                    place_slug=slug,
                    topic=topic,
                    seq=seq,
                    text=piece,
                    spot_name=spot_name,
                    city=city,
                    kb_id=kb_id,
                )
            )
    return chunks

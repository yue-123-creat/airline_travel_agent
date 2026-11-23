#!/usr/bin/env python3
"""Batch 3: backfill guide_chunk_ids + optional Qdrant upsert for matched spots only.

Prereq:
  python scripts/match_dest_attractions.py

Rules:
  - Only rows in _dest_match_report.json matched_rows
  - Unmatched spots stay in docs/未入库地点.md — never upsert
  - Default: update Postgres attraction_kb
  - --upsert-qdrant: embed + upsert travel_rag (after you补齐未入库名片)
  - --sync-json: also rewrite attractions_enriched.json rag_links
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env", override=True)

from psycopg.types.json import Jsonb

from app.config import get_settings
from app.rag.chunking import TextChunk, build_chunks_for_spot
from app.rag.upsert import upsert_travel_chunks

REPORT = ROOT / "data" / "knowledge" / "_dest_match_report.json"
ENRICHED = ROOT / "data" / "knowledge" / "attractions_enriched.json"


def _load_report() -> dict[str, Any]:
    if not REPORT.exists():
        raise SystemExit(f"missing {REPORT}; run scripts/match_dest_attractions.py first")
    return json.loads(REPORT.read_text(encoding="utf-8"))


def _aggregate_by_kb(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Merge multiple doc spots that map to the same kb_id."""
    by_kb: dict[str, dict[str, Any]] = {}
    for row in report.get("matched_rows") or []:
        kb_id = str(row.get("kb_id") or "").strip()
        if not kb_id:
            continue
        bucket = by_kb.setdefault(
            kb_id,
            {
                "kb_id": kb_id,
                "kb_name": row.get("kb_name") or row.get("name") or "",
                "kb_city": row.get("kb_city") or row.get("city") or "",
                "place_slug": row.get("place_slug") or "",
                "section_texts": {},
                "match_names": [],
            },
        )
        bucket["match_names"].append(row.get("name") or "")
        secs = row.get("section_texts") or {}
        if isinstance(secs, dict):
            for k, v in secs.items():
                cur = bucket["section_texts"].get(k) or ""
                if len(str(v or "")) > len(cur):
                    bucket["section_texts"][k] = str(v)
    return by_kb


def _chunks_for_bucket(bucket: dict[str, Any], *, size: int, overlap: int) -> list[TextChunk]:
    return build_chunks_for_spot(
        place_slug=str(bucket.get("place_slug") or ""),
        spot_name=str(bucket.get("kb_name") or ""),
        city=str(bucket.get("kb_city") or ""),
        kb_id=str(bucket.get("kb_id") or ""),
        sections=dict(bucket.get("section_texts") or {}),
        chunk_size=size,
        chunk_overlap=overlap,
        include_merged_visitor_guide=True,
    )


def update_postgres(by_kb_chunks: dict[str, list[TextChunk]]) -> dict[str, int]:
    get_settings.cache_clear()
    settings = get_settings()
    if not settings.database_url.startswith("postgresql"):
        raise RuntimeError("DATABASE_URL must be postgresql for runtime ingest")

    import psycopg

    updated = 0
    missing = 0
    today = datetime.now(timezone.utc).date().isoformat()
    with psycopg.connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            for kb_id, chunks in by_kb_chunks.items():
                ids = [c.chunk_id for c in chunks]
                cur.execute("SELECT payload FROM attraction_kb WHERE id = %s", (kb_id,))
                row = cur.fetchone()
                if not row:
                    missing += 1
                    continue
                payload = row[0] if isinstance(row[0], dict) else {}
                if not isinstance(payload, dict):
                    payload = {}
                rag = payload.get("rag_links")
                if not isinstance(rag, dict):
                    rag = {}
                rag["guide_chunk_ids"] = ids
                rag["last_updated"] = today
                payload["rag_links"] = rag
                cur.execute(
                    """
                    UPDATE attraction_kb
                    SET payload = %s,
                        chunk_count = %s,
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (Jsonb(payload), len(ids), kb_id),
                )
                updated += 1
        conn.commit()
    return {"updated": updated, "missing_in_pg": missing}


def sync_enriched_json(by_kb_chunks: dict[str, list[TextChunk]]) -> int:
    raw = json.loads(ENRICHED.read_text(encoding="utf-8"))
    items = raw["attractions"] if isinstance(raw, dict) and "attractions" in raw else raw
    if not isinstance(items, list):
        raise ValueError("attractions_enriched.json unexpected shape")
    today = datetime.now(timezone.utc).date().isoformat()
    n = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        kid = str(item.get("id") or "")
        if kid not in by_kb_chunks:
            continue
        ids = [c.chunk_id for c in by_kb_chunks[kid]]
        rag = item.get("rag_links")
        if not isinstance(rag, dict):
            rag = {}
        rag["guide_chunk_ids"] = ids
        rag["last_updated"] = today
        item["rag_links"] = rag
        n += 1
    ENRICHED.write_text(
        json.dumps(raw if isinstance(raw, dict) else items, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return n


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest matched travel RAG chunks")
    parser.add_argument(
        "--upsert-qdrant",
        action="store_true",
        help="Embed + upsert matched chunks into travel_rag",
    )
    parser.add_argument(
        "--sync-json",
        action="store_true",
        help="Also write guide_chunk_ids into attractions_enriched.json",
    )
    parser.add_argument("--skip-pg", action="store_true", help="Do not update Postgres")
    parser.add_argument("--dry-run", action="store_true", help="Print plan only")
    args = parser.parse_args()

    get_settings.cache_clear()
    settings = get_settings()
    report = _load_report()
    buckets = _aggregate_by_kb(report)
    by_kb_chunks: dict[str, list[TextChunk]] = {}
    all_chunks: list[TextChunk] = []
    for kb_id, bucket in buckets.items():
        chunks = _chunks_for_bucket(
            bucket,
            size=int(settings.rag_chunk_size or 512),
            overlap=int(settings.rag_chunk_overlap or 64),
        )
        by_kb_chunks[kb_id] = chunks
        all_chunks.extend(chunks)

    print(
        f"matched_kb={len(by_kb_chunks)} chunks={len(all_chunks)} "
        f"report_matched_rows={report.get('matched')} missed={report.get('missed')}"
    )
    for kid, chs in list(by_kb_chunks.items())[:3]:
        print(f"  {kid}: {[c.chunk_id for c in chs[:8]]}{'...' if len(chs) > 8 else ''}")

    if args.dry_run:
        print("dry-run: no writes")
        return

    if not args.skip_pg:
        pg_stats = update_postgres(by_kb_chunks)
        print(f"postgres: {pg_stats}")

    if args.sync_json:
        n = sync_enriched_json(by_kb_chunks)
        print(f"synced attractions_enriched.json rows={n}")

    if args.upsert_qdrant:
        uniq: dict[str, TextChunk] = {c.chunk_id: c for c in all_chunks}
        # DashScope embeddings：单批 contents 不宜超过 10
        stats = upsert_travel_chunks(uniq.values(), settings=settings, batch_size=10)
        print(f"qdrant: {stats}")
    else:
        print("skip qdrant (pass --upsert-qdrant after 未入库地点 are added to KB)")


if __name__ == "__main__":
    main()

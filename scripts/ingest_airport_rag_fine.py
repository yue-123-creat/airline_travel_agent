"""机场指引按 RAG 细切（CHUNK_SIZE/OVERLAP）覆盖 aviation_rag，并回填 rag_links。

对 LJG / WNZ / LYA / PVG / JHG：
  1) 删除 aviation_rag 中该 airport_code 全部旧点（避免粗切 id 残留）
  2) 按大类拼原文 → split_with_overlap 细切 → upsert
  3) 同大类全部 chunk_id 写回 airport_services.rag_links

用法:
  python scripts/ingest_airport_rag_fine.py --dry-run
  python scripts/ingest_airport_rag_fine.py
  python scripts/ingest_airport_rag_fine.py --airports JHG
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings
from app.rag.chunking import TextChunk, split_with_overlap
from app.rag.collections import AVIATION_COLLECTION, ensure_collections, get_qdrant_client
from app.rag.upsert import upsert_aviation_chunks


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _cat_texts_uncapped_wnz(mod) -> dict[str, str]:
    docs = {k: p.read_text(encoding="utf-8") for k, p in mod.RAW_FILES.items()}
    out: dict[str, str] = {}
    for cat, ranges in mod.RANGES:
        parts: list[str] = []
        for doc_key, start, end in ranges:
            piece = mod._extract(docs.get(doc_key) or "", start, end)
            if piece:
                parts.append(piece)
        seen: set[str] = set()
        lines: list[str] = []
        for part in parts:
            for ln in part.splitlines():
                k = ln.strip()
                if not k or k in seen:
                    continue
                seen.add(k)
                lines.append(ln)
        out[cat] = "\n".join(lines).strip()
    return out


def _cat_texts_uncapped_ljg(mod) -> dict[str, str]:
    body = mod._body_text(mod.RAW.read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for cat, ranges in mod.RANGES:
        parts: list[str] = []
        for start, end in ranges:
            piece = mod._extract(body, start, end)
            if piece:
                parts.append(piece)
        seen: set[str] = set()
        lines: list[str] = []
        for part in parts:
            for ln in part.splitlines():
                k = ln.strip()
                if not k or k in seen:
                    continue
                seen.add(k)
                lines.append(ln)
        out[cat] = "\n".join(lines).strip()
    return out


def _cat_texts_lya(mod) -> dict[str, str]:
    if not mod.RAW.is_file():
        raise SystemExit(f"missing {mod.RAW}")
    return mod.build_category_texts(mod.RAW.read_text(encoding="utf-8"))


def _cat_texts_pvg(mod) -> dict[str, str]:
    docs: dict[str, str] = {}
    for key, path in mod.RAW_FILES.items():
        if not path.is_file():
            raise SystemExit(f"missing {path}")
        docs[key] = path.read_text(encoding="utf-8")
    return mod.build_category_texts(docs)


def _cat_texts_multi_doc(mod) -> dict[str, str]:
    """JHG / PVG 等同构：RAW_FILES + build_category_texts(docs)."""
    docs: dict[str, str] = {}
    for key, path in mod.RAW_FILES.items():
        if not path.is_file():
            raise SystemExit(f"missing {path}")
        docs[key] = path.read_text(encoding="utf-8")
    return mod.build_category_texts(docs)


def build_fine_chunks(
    *,
    slug: str,
    airport_code: str,
    city: str,
    spot: str,
    cat_order: list[str],
    cat_texts: dict[str, str],
    size: int,
    overlap: int,
) -> tuple[list[TextChunk], dict[str, list[str]]]:
    chunks: list[TextChunk] = []
    by_cat: dict[str, list[str]] = defaultdict(list)
    seq = 0
    for cat in cat_order:
        text = (cat_texts.get(cat) or "").strip()
        if not text:
            continue
        for piece in split_with_overlap(text, size=size, overlap=overlap):
            seq += 1
            cid = f"chunk_{slug}_{seq:02d}_{cat}"
            chunks.append(
                TextChunk(
                    chunk_id=cid,
                    place_slug=slug,
                    topic=cat,
                    seq=seq,
                    text=piece,
                    spot_name=spot,
                    city=city,
                    kb_id=airport_code,
                )
            )
            by_cat[cat].append(cid)
    return chunks, dict(by_cat)


def delete_airport_points(airport_code: str) -> int:
    from qdrant_client.http import models as qm

    s = get_settings()
    ensure_collections(s)
    client = get_qdrant_client(s)
    coll = (s.qdrant_aviation_collection or AVIATION_COLLECTION).strip() or AVIATION_COLLECTION
    code = airport_code.strip().upper()
    # count first
    points, next_offset = client.scroll(
        collection_name=coll,
        scroll_filter=qm.Filter(
            must=[qm.FieldCondition(key="airport_code", match=qm.MatchValue(value=code))]
        ),
        limit=200,
        with_payload=False,
        with_vectors=False,
    )
    ids = [p.id for p in points]
    while next_offset is not None:
        points, next_offset = client.scroll(
            collection_name=coll,
            scroll_filter=qm.Filter(
                must=[qm.FieldCondition(key="airport_code", match=qm.MatchValue(value=code))]
            ),
            limit=200,
            offset=next_offset,
            with_payload=False,
            with_vectors=False,
        )
        ids.extend(p.id for p in points)
    if ids:
        client.delete(
            collection_name=coll,
            points_selector=qm.PointIdsList(points=ids),
        )
    return len(ids)


def update_postgres(airport_code: str, chunk_ids_by_cat: dict[str, list[str]]) -> int:
    import psycopg

    s = get_settings()
    n = 0
    with psycopg.connect(s.database_url) as conn:
        for cat, ids in chunk_ids_by_cat.items():
            rag = json.dumps({"guide_chunk_ids": ids}, ensure_ascii=False)
            cur = conn.execute(
                """
                UPDATE airport_services
                SET rag_links = %s::jsonb
                WHERE airport_code = %s AND service_category = %s
                """,
                (rag, airport_code, cat),
            )
            n += cur.rowcount or 0
        conn.commit()
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--airports", default="LJG,WNZ,LYA,PVG,JHG", help="comma codes")
    ap.add_argument("--pg-only", action="store_true")
    ap.add_argument("--qdrant-only", action="store_true")
    args = ap.parse_args()

    s = get_settings()
    size = int(s.rag_chunk_size or 512)
    overlap = int(s.rag_chunk_overlap or 64)
    want = {x.strip().upper() for x in args.airports.split(",") if x.strip()}

    jobs = []
    if "WNZ" in want:
        wnz = _load("ingest_wnz", ROOT / "scripts" / "ingest_wnz_airport_rag.py")
        jobs.append(
            {
                "code": "WNZ",
                "slug": "wnz",
                "city": wnz.CITY,
                "spot": wnz.SPOT,
                "cats": list(wnz.CAT_CHUNK.keys()),
                "texts": _cat_texts_uncapped_wnz(wnz),
            }
        )
    if "LJG" in want:
        ljg = _load("ingest_ljg", ROOT / "scripts" / "ingest_ljg_airport_rag.py")
        jobs.append(
            {
                "code": "LJG",
                "slug": "ljg",
                "city": ljg.CITY,
                "spot": ljg.SPOT,
                "cats": list(ljg.CAT_CHUNK.keys()),
                "texts": _cat_texts_uncapped_ljg(ljg),
            }
        )
    if "LYA" in want:
        lya = _load("ingest_lya", ROOT / "scripts" / "ingest_lya_airport_rag.py")
        jobs.append(
            {
                "code": "LYA",
                "slug": "lya",
                "city": lya.CITY,
                "spot": lya.SPOT,
                "cats": list(lya.CAT_CHUNK.keys()),
                "texts": _cat_texts_lya(lya),
            }
        )
    if "PVG" in want:
        pvg = _load("ingest_pvg", ROOT / "scripts" / "ingest_pvg_airport_rag.py")
        jobs.append(
            {
                "code": "PVG",
                "slug": "pvg",
                "city": pvg.CITY,
                "spot": pvg.SPOT,
                "cats": list(pvg.CAT_CHUNK.keys()),
                "texts": _cat_texts_pvg(pvg),
            }
        )
    if "JHG" in want:
        jhg = _load("ingest_jhg", ROOT / "scripts" / "ingest_jhg_airport_rag.py")
        jobs.append(
            {
                "code": "JHG",
                "slug": "jhg",
                "city": jhg.CITY,
                "spot": jhg.SPOT,
                "cats": list(jhg.CAT_CHUNK.keys()),
                "texts": _cat_texts_multi_doc(jhg),
            }
        )

    print(f"fine cut size={size} overlap={overlap}")

    for job in jobs:
        chunks, by_cat = build_fine_chunks(
            slug=job["slug"],
            airport_code=job["code"],
            city=job["city"],
            spot=job["spot"],
            cat_order=job["cats"],
            cat_texts=job["texts"],
            size=size,
            overlap=overlap,
        )
        print(f"\n{job['code']}: {len(chunks)} chunks")
        for cat in job["cats"]:
            ids = by_cat.get(cat) or []
            src = len(job["texts"].get(cat) or "")
            print(f"  {cat}: src={src} chars -> {len(ids)} chunks")

        if args.dry_run:
            preview = ROOT / "data" / "knowledge" / "_airport_chunk_preview_fine" / job["slug"]
            preview.mkdir(parents=True, exist_ok=True)
            for c in chunks:
                (preview / f"{c.chunk_id}.txt").write_text(c.text, encoding="utf-8")
            print(f"  preview: {preview}")
            print(f"  rag_links: {json.dumps(by_cat, ensure_ascii=False)}")
            continue

        if not args.pg_only:
            deleted = delete_airport_points(job["code"])
            print(f"  deleted old points: {deleted}")
            # DashScope embedding batch ≤10
            stats = upsert_aviation_chunks(chunks, airport_code=job["code"], batch_size=8)
            print(f"  qdrant: {stats}")

        if not args.qdrant_only:
            n = update_postgres(job["code"], by_cat)
            print(f"  postgres updated rows: {n}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

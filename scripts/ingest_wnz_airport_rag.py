"""温州机场三份指引：按大类粗切（每类 1 块，共 6 块）→ aviation_rag，并回填 rag_links。

来源（Projects 根目录 docx → data/knowledge/_wnz_*_raw.txt）：
  - 温州机场乘机业务服务.docx      → _wnz_boarding_raw.txt
  - 温州机场便民与特殊旅客服务.docx → _wnz_facility_special_raw.txt
  - 温州机场咨询指引服务.docx      → _wnz_consult_raw.txt

用法:
  python scripts/ingest_wnz_airport_rag.py --dry-run
  python scripts/ingest_wnz_airport_rag.py
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings
from app.rag.chunking import TextChunk
from app.rag.upsert import upsert_aviation_chunks

RAW_DIR = ROOT / "data" / "knowledge"
RAW_FILES: dict[str, Path] = {
    "boarding": RAW_DIR / "_wnz_boarding_raw.txt",
    "facility_special": RAW_DIR / "_wnz_facility_special_raw.txt",
    "consult": RAW_DIR / "_wnz_consult_raw.txt",
}

AIRPORT_CODE = "WNZ"
CITY = "温州"
SPOT = "温州龙湾国际机场"
MAX_CHARS = 6500

CAT_CHUNK: dict[str, str] = {
    "transport": "chunk_wnz_01_transport",
    "navigation": "chunk_wnz_02_navigation",
    "checkin": "chunk_wnz_03_checkin",
    "facility": "chunk_wnz_04_facility",
    "lounge": "chunk_wnz_05_lounge",
    "emergency": "chunk_wnz_06_emergency",
}

# (category, [(doc_key, start, end_exclusive|None), ...])
RANGES: list[tuple[str, list[tuple[str, str, str | None]]]] = [
    (
        "transport",
        [
            ("facility_special", "地面交通", "爱心服务"),
            ("consult", "神州租车", "LATOALPS"),
        ],
    ),
    (
        "navigation",
        [
            ("boarding", "乘机流程", "值机"),
            ("boarding", "登机", "联检政策"),
            ("boarding", "联检政策", "锂电池"),
            ("boarding", "中转服务", "[[TABLE 1]]"),
            ("facility_special", "闻讯柜台", "卫生间"),
            ("consult", "帮助指引", "商业服务"),
        ],
    ),
    (
        "checkin",
        [
            ("boarding", "值机", "登机"),
            ("boarding", "锂电池", "延误转机公安登机"),
            ("boarding", "安全检查", "中转服务"),
            ("facility_special", "值机柜台", "WiFi认证"),
            ("facility_special", "临时乘机证明", "闻讯柜台"),
            ("facility_special", "安检通道指引", "[[TABLE 1]]"),
        ],
    ),
    (
        "facility",
        [
            ("facility_special", "公共设施", "头等舱休息室"),
            ("facility_special", "普通退税", "值机柜台"),
            ("facility_special", "WiFi认证", "临时乘机证明"),
            ("facility_special", "卫生间", "安检通道指引"),
            ("consult", "刻睡", "瑞妮小熊"),
            ("consult", "中图便利店", "DIV.POLO"),
            ("consult", "艾力斯特按摩椅", "DIV.POLO"),
        ],
    ),
    (
        "lounge",
        [
            ("facility_special", "头等舱休息室", "失物招领柜台"),
            ("consult", "商业服务", "神州租车"),
            ("consult", "LATOALPS", "刻睡"),
            ("consult", "瑞妮小熊", "中图便利店"),
            ("consult", "DIV.POLO", "[[TABLE 1]]"),
        ],
    ),
    (
        "emergency",
        [
            ("facility_special", "爱心服务", "公共设施"),
            ("facility_special", "失物招领柜台", "普通退税"),
            ("boarding", "延误转机公安登机", "安全检查"),
        ],
    ),
]


def _find(body: str, needle: str, *, whole_line: bool = False) -> int:
    """Locate needle; whole_line=True 要求整行等于 needle（按行偏移，避免 find 误中「登机牌」）。"""
    if not needle:
        return -1
    target = needle.strip()
    pos = 0
    for line in body.splitlines(keepends=True):
        raw = line[:-1] if line.endswith("\n") else line
        if line.endswith("\r\n"):
            raw = line[:-2]
        elif line.endswith("\n") or line.endswith("\r"):
            raw = line[:-1]
        if whole_line:
            if raw.strip() == target:
                return pos
        else:
            if target in raw:
                return pos + raw.find(target)
            soft = re.sub(r"[（）()：:\s]", "", target)
            if soft and soft in re.sub(r"[（）()：:\s]", "", raw):
                return pos
        pos += len(line)
    return -1


def _extract(body: str, start: str, end: str | None) -> str:
    i = _find(body, start, whole_line=True)
    if i < 0:
        i = _find(body, start, whole_line=False)
    if i < 0:
        return ""
    if end:
        rest = body[i + len(start) :]
        j = _find(rest, end, whole_line=True)
        if j < 0:
            j = _find(rest, end, whole_line=False)
        j = i + len(start) + j if j >= 0 else len(body)
    else:
        j = len(body)
    return body[i:j].strip()


def build_category_texts(docs: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for cat, ranges in RANGES:
        parts: list[str] = []
        for doc_key, start, end in ranges:
            body = docs.get(doc_key) or ""
            piece = _extract(body, start, end)
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
        text = "\n".join(lines).strip()
        if len(text) > MAX_CHARS:
            text = text[:MAX_CHARS].rstrip() + "\n…（原文过长已截断，详见完整指引）"
        out[cat] = text
    return out


def build_chunks(cat_texts: dict[str, str]) -> list[TextChunk]:
    chunks: list[TextChunk] = []
    seq = 0
    for cat in CAT_CHUNK:
        text = (cat_texts.get(cat) or "").strip()
        if not text:
            continue
        seq += 1
        chunks.append(
            TextChunk(
                chunk_id=CAT_CHUNK[cat],
                place_slug="wnz",
                topic=cat,
                seq=seq,
                text=text,
                spot_name=SPOT,
                city=CITY,
                kb_id=AIRPORT_CODE,
            )
        )
    return chunks


def update_postgres(chunk_ids_by_cat: dict[str, list[str]]) -> int:
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
                (rag, AIRPORT_CODE, cat),
            )
            n += cur.rowcount or 0
        conn.commit()
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--pg-only", action="store_true")
    ap.add_argument("--qdrant-only", action="store_true")
    args = ap.parse_args()

    docs: dict[str, str] = {}
    for key, path in RAW_FILES.items():
        if not path.is_file():
            print(f"missing: {path}")
            return 1
        docs[key] = path.read_text(encoding="utf-8")

    cat_texts = build_category_texts(docs)
    for cat, t in cat_texts.items():
        print(f"  {cat}: {len(t)} chars -> {CAT_CHUNK[cat]}")
        if not t:
            print(f"  WARNING empty: {cat}")

    chunks = build_chunks(cat_texts)
    print(f"chunks: {len(chunks)}")
    for c in chunks:
        print(f"  {c.chunk_id} len={len(c.text)}")

    by_cat = {cat: [CAT_CHUNK[cat]] for cat in CAT_CHUNK if any(c.topic == cat for c in chunks)}
    for cat, cid in CAT_CHUNK.items():
        by_cat.setdefault(cat, [cid])

    if args.dry_run:
        preview_dir = RAW_DIR / "_wnz_chunk_preview"
        preview_dir.mkdir(parents=True, exist_ok=True)
        for c in chunks:
            (preview_dir / f"{c.chunk_id}.txt").write_text(c.text, encoding="utf-8")
        print("rag_links:", json.dumps(by_cat, ensure_ascii=False))
        print(f"preview written: {preview_dir}")
        return 0

    if not args.pg_only:
        stats = upsert_aviation_chunks(chunks, airport_code=AIRPORT_CODE)
        print("qdrant:", stats)

    if not args.qdrant_only:
        n = update_postgres(by_cat)
        print(f"postgres updated rows: {n}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

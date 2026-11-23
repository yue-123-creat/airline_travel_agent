"""丽江机场指引：按大类粗切（默认每类 1 块，共 7 块）→ aviation_rag，并回填 rag_links。

用法:
  python scripts/ingest_ljg_airport_rag.py
  python scripts/ingest_ljg_airport_rag.py --dry-run
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

RAW = ROOT / "data" / "knowledge" / "_ljg_airport_raw.txt"
AIRPORT_CODE = "LJG"
CITY = "丽江"
SPOT = "丽江三义国际机场"
MAX_CHARS = 6500  # 单块上限，避免过碎；embedding 可接受

CAT_CHUNK: dict[str, str] = {
    "transport": "chunk_ljg_01_transport",
    "navigation": "chunk_ljg_02_navigation",
    "checkin": "chunk_ljg_03_checkin",
    "facility": "chunk_ljg_04_facility",
    "lounge": "chunk_ljg_05_lounge",
    "emergency": "chunk_ljg_06_emergency",
    "policy": "chunk_ljg_07_policy",
}

# (category, [(start, end_exclusive), ...])  正文连续区间，避免重叠切片
# end 为空表示直到文末
RANGES: list[tuple[str, list[tuple[str, str | None]]]] = [
    (
        "navigation",
        [
            ("一、核心旅客路线", "二、出发流程保障"),
            ("(二)航站楼开放及关闭", "(四)航站楼指引"),
            ("3.综合服务柜台（问讯）", "4.出发层值机柜台"),
            ("三、到达流程保障", "四、特殊情况保障"),
        ],
    ),
    (
        "transport",
        [
            ("(一)交通指引", "(二)航站楼开放及关闭"),
            ("1.交通指引", "2.基础服务指引"),
            ("27.停车场相关规定", "34.航站楼内宾馆/钟点房相关"),
        ],
    ),
    (
        "checkin",
        [
            ("（七）值机环节保障", "三、到达流程保障"),
            ("1.值机区域分布情况：", "2.行李寄存分布情况"),
            ("7.临时乘机身份证明办理地点", "8.旅客乘机姓名错误相关问题"),
            ("15.超规行李办理地点", "18.常见行李类问题及处理方式"),
            ("24.登机牌办理及登机口关闭时间", "27.停车场相关规定"),
        ],
    ),
    (
        "facility",
        [
            ("5.无障碍通道", "12.售票点"),
            ("（五）便民设施信息", "（六）商业及相关服务"),
            ("2.行李寄存分布情况", "4.丽江机场失物招领地点及分布"),
            ("34.航站楼内宾馆/钟点房相关", "37.安检内餐饮及商铺分布"),
            ("38.急救室位置", "41.水生动物托运相关规定"),
            ("44.机场WiFi使用方式", "45.遗失物品代领规定"),
            ("69.便民箱物品配置要求", "十、丽江机场航站楼平面流程图"),
        ],
    ),
    (
        "lounge",
        [
            ("1.两舱/金银卡休息室", "3.综合服务柜台（问讯）"),
            ("12.售票点", "（五）便民设施信息"),
            ("（六）商业及相关服务", "（七）值机环节保障"),
            ("五、专项服务保障", "六、服务品牌"),
            ("63.机场附近银行相关", "66.机场药品相关服务"),
        ],
    ),
    (
        "emergency",
        [
            ("四、特殊情况保障", "五、专项服务保障"),
            ("八、丽江机场各服务业务咨询电话", "九、丽江机场日常问讯业务知识"),
            ("4.丽江机场失物招领地点及分布", "7.临时乘机身份证明办理地点"),
            ("18.常见行李类问题及处理方式", "24.登机牌办理及登机口关闭时间"),
            ("67.航班延误/取消后的手续办理", "69.便民箱物品配置要求"),
            ("十二、丽江机场与96566呼叫中心衔接方案", None),
        ],
    ),
    (
        "policy",
        [
            ("七、机场净空管理", "八、丽江机场各服务业务咨询电话"),
        ],
    ),
]


def _body_text(raw: str) -> str:
    m = re.search(r"^一、核心旅客路线\s*$", raw, flags=re.M)
    return raw[m.start() :].strip() if m else raw.strip()


def _find(body: str, needle: str) -> int:
    if not needle:
        return -1
    i = body.find(needle)
    if i >= 0:
        return i
    soft = re.sub(r"[（）()：:]", "", needle)
    for line in body.splitlines():
        if soft and soft in re.sub(r"[（）()：:]", "", line):
            return body.find(line)
    return -1


def _extract(body: str, start: str, end: str | None) -> str:
    i = _find(body, start)
    if i < 0:
        return ""
    if end:
        j = _find(body[i + len(start) :], end)
        j = i + len(start) + j if j >= 0 else len(body)
    else:
        j = len(body)
    return body[i:j].strip()


def build_category_texts(raw: str) -> dict[str, str]:
    body = _body_text(raw)
    out: dict[str, str] = {}
    for cat, ranges in RANGES:
        parts: list[str] = []
        for start, end in ranges:
            piece = _extract(body, start, end)
            if piece:
                parts.append(piece)
        # 行级去重保序
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
                place_slug="ljg",
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

    if not RAW.is_file():
        print(f"missing: {RAW}")
        return 1

    raw = RAW.read_text(encoding="utf-8")
    cat_texts = build_category_texts(raw)
    for cat, t in cat_texts.items():
        print(f"  {cat}: {len(t)} chars -> {CAT_CHUNK[cat]}")
        if not t:
            print(f"  WARNING empty: {cat}")

    chunks = build_chunks(cat_texts)
    print(f"chunks: {len(chunks)}")
    for c in chunks:
        print(f"  {c.chunk_id} len={len(c.text)}")

    by_cat = {cat: [CAT_CHUNK[cat]] for cat in CAT_CHUNK if any(c.topic == cat for c in chunks)}
    # 空类也挂主 id，便于表侧筛选
    for cat, cid in CAT_CHUNK.items():
        by_cat.setdefault(cat, [cid])

    if args.dry_run:
        print("rag_links:", json.dumps(by_cat, ensure_ascii=False))
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

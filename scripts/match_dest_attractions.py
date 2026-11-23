#!/usr/bin/env python3
"""Match destination corpus spots → attraction_kb / attractions_enriched.json.

Writes:
  - docs/未入库地点.md          (misses only; do NOT upsert these to Qdrant)
  - data/knowledge/_dest_match_report.json
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.rag.chunking import build_chunks_for_spot, place_slug_from_kb_id
from app.rag.dest_corpus import DestSpot, load_dest_attractions
from app.travel.kb_normalize import normalize_city, normalize_name

RAW = ROOT / "data" / "knowledge" / "_dest_attractions_raw.txt"
ENRICHED = ROOT / "data" / "knowledge" / "attractions_enriched.json"
OUT_MISS = ROOT / "docs" / "未入库地点.md"
OUT_MATCH = ROOT / "data" / "knowledge" / "_dest_match_report.json"


def _load_kb(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("attractions"), list):
        return list(data["attractions"])
    if isinstance(data, list):
        return data
    raise ValueError(f"unexpected KB shape: {path}")


def _blankish(s: str) -> bool:
    t = (s or "").strip()
    return (not t) or t in {"—", "-", "－", "无", "未知", "null", "None"}


def _spot_place_keys(spot: DestSpot) -> list[str]:
    """City keys for matching. City 为空时回退省级 / region（直辖市常见：City=— 省级=上海市）。"""
    keys: list[str] = []
    for raw in (spot.city, spot.province, spot.region_header, spot.district):
        if _blankish(str(raw or "")):
            continue
        # region_header 可能是「成都市·都江堰」→ 取前段
        part = str(raw).split("·")[0].split("/")[0].strip()
        n = normalize_city(part)
        if n and n not in keys:
            keys.append(n)
        # 也保留未 normalize 的短串，便于「上海」对「上海市」
        if part and part not in keys:
            keys.append(part)
    return keys


def _kb_city_text(row: dict[str, Any]) -> str:
    return str(row.get("city") or row.get("city_raw") or row.get("region") or "")


def _city_compatible(doc_city: str, kb_city: str) -> bool:
    a = normalize_city(doc_city)
    b = normalize_city(kb_city)
    if not a or not b:
        # 直辖市/省级原文兜底
        da, db = (doc_city or "").strip(), (kb_city or "").strip()
        if not da or not db:
            return False
        return da in db or db in da
    return a == b or a in b or b in a


def _city_compatible_any(place_keys: list[str], kb_city: str) -> bool:
    return any(_city_compatible(k, kb_city) for k in place_keys)


def _kb_names(row: dict[str, Any]) -> list[str]:
    names = [str(row.get("name") or "")]
    aliases = row.get("aliases") or []
    if isinstance(aliases, list):
        names.extend(str(a) for a in aliases if a)
    out: list[str] = []
    seen: set[str] = set()
    for n in names:
        nn = normalize_name(n)
        if nn and nn not in seen:
            seen.add(nn)
            out.append(n)
    return out


def _name_soft_equal(a: str, b: str) -> bool:
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    # allow 景区/风景区/公园 suffix noise
    for suf in ("景区", "风景区", "旅游区", "公园", "古城", "古镇"):
        if na.endswith(suf):
            na = na[: -len(suf)]
        if nb.endswith(suf):
            nb = nb[: -len(suf)]
    return bool(na) and bool(nb) and (na == nb or na in nb or nb in na)


def _name_prefix_hit(doc_name: str, kb_name: str) -> bool:
    """语料截断名：外滩肯定 ← 外滩；要求库名长度≥2 且为语料名的前缀。"""
    na, nb = normalize_name(doc_name), normalize_name(kb_name)
    if not na or not nb or len(nb) < 2:
        return False
    if na == nb:
        return True
    # KB 名是语料名的前缀（语料多了「肯定/值得…」尾巴）
    if na.startswith(nb) and len(na) <= len(nb) + 6:
        return True
    return False


def _row_name_match(doc_name: str, row: dict[str, Any], *, mode: str) -> bool:
    for nm in _kb_names(row):
        if mode == "exact" and normalize_name(nm) == normalize_name(doc_name):
            return True
        if mode == "soft" and _name_soft_equal(doc_name, nm):
            return True
        if mode == "prefix" and _name_prefix_hit(doc_name, nm):
            return True
    return False


def match_spot(spot: DestSpot, kb_rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return best KB hit or None. Prefer exact name+city; reject cross-city substring."""
    name = spot.name
    place_keys = _spot_place_keys(spot)

    def _with_city(mode: str) -> list[dict[str, Any]]:
        return [
            r
            for r in kb_rows
            if _row_name_match(name, r, mode=mode)
            and _city_compatible_any(place_keys, _kb_city_text(r))
        ]

    # 1) exact name + city/province
    exact_city = _with_city("exact")
    if len(exact_city) == 1:
        return {"row": exact_city[0], "match": "exact_name_city"}
    if len(exact_city) > 1:
        return {"row": exact_city[0], "match": "ambiguous_name_city"}

    # 2) soft name + city
    soft_city = _with_city("soft")
    if len(soft_city) == 1:
        return {"row": soft_city[0], "match": "soft_name_city"}
    if len(soft_city) > 1:
        soft_city.sort(key=lambda r: abs(len(str(r.get("name") or "")) - len(name)))
        return {"row": soft_city[0], "match": "ambiguous_soft_city"}

    # 3) 截断前缀：外滩肯定 → 外滩（必须同城/同省）
    prefix_city = _with_city("prefix")
    if prefix_city:
        # 取最长库名，避免「外」误伤；这里库名≥2 已在 _name_prefix_hit 约束
        prefix_city.sort(key=lambda r: len(normalize_name(str(r.get("name") or ""))), reverse=True)
        return {"row": prefix_city[0], "match": "prefix_name_city"}

    # 4) unique exact name globally
    exact_name = [r for r in kb_rows if _row_name_match(name, r, mode="exact")]
    if len(exact_name) == 1:
        return {"row": exact_name[0], "match": "exact_name_only"}

    # 5) unique soft name + city check
    soft_name = [r for r in kb_rows if _row_name_match(name, r, mode="soft")]
    if len(soft_name) == 1:
        if _city_compatible_any(place_keys, _kb_city_text(soft_name[0])):
            return {"row": soft_name[0], "match": "soft_name_only"}
        return None

    return None


def _section_keys(spot: DestSpot) -> list[str]:
    order = ("交通", "住宿", "美食", "小贴士")
    return [k for k in order if (spot.sections.get(k) or "").strip()]


def write_miss_doc(misses: list[DestSpot], *, total: int, matched: int) -> None:
    by_region: dict[str, list[DestSpot]] = defaultdict(list)
    for s in misses:
        key = s.region_header or s.city or "未分区"
        by_region[key].append(s)

    lines = [
        "# 未入库地点",
        "",
        "> 来源：`目的地景点解耦整理-每景点自包含.docx` 解析结果。",
        "> 这些景点在语料中有游记段，但未能匹配到当前 `attraction_kb` / `attractions_enriched.json`。",
        "> **请先在 Postgres `attraction_kb` 补齐名片后，再灌入 Qdrant（勿先 upsert 未入库点）。**",
        "",
        f"- 语料景点总数：{total}",
        f"- 已匹配：{matched}",
        f"- **未入库：{len(misses)}**",
        "",
        "## 清单",
        "",
    ]
    for region in sorted(by_region.keys()):
        lines.append(f"### {region}")
        lines.append("")
        lines.append("| 景点名 | City | 省级 | 区县 |")
        lines.append("| --- | --- | --- | --- |")
        for s in by_region[region]:
            lines.append(
                f"| {s.name} | {s.city} | {s.province} | {s.district or '—'} |"
            )
        lines.append("")
    OUT_MISS.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    if not RAW.exists():
        raise SystemExit(f"missing corpus: {RAW}")
    spots = load_dest_attractions(RAW)
    kb_rows = _load_kb(ENRICHED)

    matched_rows: list[dict[str, Any]] = []
    missed: list[DestSpot] = []

    for spot in spots:
        hit = match_spot(spot, kb_rows)
        if not hit:
            missed.append(spot)
            continue
        row = hit["row"]
        kb_id = str(row.get("id") or "")
        kb_name = str(row.get("name") or "")
        kb_city = str(row.get("city") or row.get("city_raw") or "")
        slug = place_slug_from_kb_id(kb_id)
        chunks = build_chunks_for_spot(
            place_slug=slug,
            spot_name=kb_name or spot.name,
            city=kb_city or normalize_city(spot.city) or spot.city,
            kb_id=kb_id,
            sections=spot.sections,
            chunk_size=512,
            chunk_overlap=64,
            include_merged_visitor_guide=True,
        )
        matched_rows.append(
            {
                "doc_index": spot.doc_index,
                "name": spot.name,
                "city": spot.city,
                "province": spot.province,
                "district": spot.district,
                "region_header": spot.region_header,
                "match": hit["match"],
                "kb_id": kb_id,
                "kb_name": kb_name,
                "kb_city": kb_city,
                "sections": _section_keys(spot),
                "place_slug": slug,
                "guide_chunk_ids": [c.chunk_id for c in chunks],
                "section_texts": spot.sections,
            }
        )

    report = {
        "source": str(RAW),
        "total_spots": len(spots),
        "matched": len(matched_rows),
        "missed": len(missed),
        "matched_rows": matched_rows,
        "missed_rows": [
            {
                "doc_index": s.doc_index,
                "name": s.name,
                "city": s.city,
                "province": s.province,
                "district": s.district,
                "region_header": s.region_header,
                "sections": _section_keys(s),
            }
            for s in missed
        ],
    }
    OUT_MATCH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    write_miss_doc(missed, total=len(spots), matched=len(matched_rows))
    print(f"total={len(spots)} matched={len(matched_rows)} missed={len(missed)}")
    print(f"wrote {OUT_MATCH}")
    print(f"wrote {OUT_MISS}")


if __name__ == "__main__":
    main()

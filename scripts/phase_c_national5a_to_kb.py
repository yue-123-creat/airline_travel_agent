#!/usr/bin/env python3
"""Phase C: merge travel-lingxi-ai national-scenic-knowledge.js into attraction_kb.

Confirmed rules:
- Match by name/officialName/aliases → kb name+aliases (city prefer; unique name fallback)
- Ambiguous multi-match → skip (report only)
- Matched: aliases UNION; tags UNION from filtered keywords + 国家5A; no content overwrite
- Unmatched 5A → INSERT minimal row (coords left empty for later Amap fill)
- Supports --offset/--limit batches; dry-run by default; --apply to write
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from psycopg.types.json import Jsonb

from app.travel.kb_normalize import normalize_city, normalize_name

N5A_DEFAULT = ROOT.parent / "travel-lingxi-ai" / "national-scenic-knowledge.js"
if not N5A_DEFAULT.exists():
    N5A_DEFAULT = ROOT / ".." / "travel-lingxi-ai" / "national-scenic-knowledge.js"

TAG_5A = "国家5A"
# keywords longer than this (or containing separators) stay in meta only
_KW_MAX_LEN = 12
_KW_BAD = re.compile(r"[、，,。；;：:\s]")

UPDATE_SQL = """
UPDATE attraction_kb SET
  aliases = %(aliases)s,
  tags = %(tags)s,
  category = %(category)s,
  payload = %(payload)s,
  updated_at = %(updated_at)s
WHERE id = %(id)s
"""

INSERT_SQL = """
INSERT INTO attraction_kb (
  id, name, name_norm, aliases, city_raw, city_norm, region, category, tags,
  is_free, adult_price, currency, has_cover, has_intro, chunk_count,
  lat, lng, geo_code, payload, source, updated_at
) VALUES (
  %(id)s, %(name)s, %(name_norm)s, %(aliases)s, %(city_raw)s, %(city_norm)s,
  %(region)s, %(category)s, %(tags)s,
  NULL, NULL, 'CNY', FALSE, %(has_intro)s, 0,
  NULL, NULL, NULL, %(payload)s, 'national_5a', %(updated_at)s
)
"""

CITY_ABBR = {
    "上海": "sh",
    "成都": "cd",
    "北京": "bj",
    "杭州": "hz",
    "重庆": "cq",
    "西安": "xa",
    "广州": "gz",
    "深圳": "sz",
    "南京": "nj",
    "苏州": "szu",
    "武汉": "wh",
    "长沙": "cs",
    "昆明": "km",
    "丽江": "lj",
    "大理": "dl",
    "桂林": "gl",
    "厦门": "xm",
    "青岛": "qd",
    "天津": "tj",
    "三亚": "sy",
    "无锡": "wx",
    "连云港": "lyg",
    "徐州": "xz",
    "常州": "cz",
    "盐城": "yc",
    "淮安": "ha",
    "镇江": "zj",
    "宿迁": "sq",
}


def load_national_5a(path: Path) -> list[dict[str, Any]]:
    """Flatten region groups → attraction list (province injected)."""
    try:
        proc = subprocess.run(
            [
                "node",
                "-e",
                "const fs=require('fs');"
                "const s=fs.readFileSync(process.argv[1],'utf8');"
                "const m=s.match(/globalThis\\.nationalScenicKnowledge\\s*=\\s*(\\[[\\s\\S]*\\])\\s*;?\\s*$/);"
                "if(!m){console.error('parse fail'); process.exit(2)}"
                "const regions=eval(m[1]);"
                "const out=[];"
                "for(const r of regions){"
                "  const prov=r.region||'';"
                "  for(const a of (r.attractions||[])){"
                "    out.push(Object.assign({}, a, {province: a.region||prov, _group_region: prov}));"
                "  }"
                "}"
                "console.log(JSON.stringify(out));",
                str(path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            data = json.loads(proc.stdout)
            if isinstance(data, list):
                return data
        raise SystemExit(f"node parse failed: {proc.stderr[:500]}")
    except FileNotFoundError as e:
        raise SystemExit(f"node required to parse JS: {e}") from e


def _merge_str_list(existing: list[str], incoming: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for x in list(existing or []) + list(incoming or []):
        s = str(x).strip()
        if not s:
            continue
        key = normalize_name(s)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def _alias_candidates(lx: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for key in ("officialName", "name"):
        v = str(lx.get(key) or "").strip()
        if v:
            out.append(v)
    out.extend(str(x).strip() for x in (lx.get("aliases") or []) if str(x).strip())
    # short keyword that looks like a name/alias (not theme tag)
    name_keys = {normalize_name(x) for x in out if normalize_name(x)}
    city_n = normalize_name(str(lx.get("city") or ""))
    prov_n = normalize_name(str(lx.get("province") or lx.get("region") or ""))
    for kw in lx.get("keywords") or []:
        s = str(kw).strip()
        if not s or len(s) > _KW_MAX_LEN or _KW_BAD.search(s):
            continue
        kn = normalize_name(s)
        if not kn or kn in name_keys or kn == city_n or kn == prov_n:
            continue
        # prefer alias-like: overlaps with official/name stem
        if any(kn in nk or nk in kn for nk in name_keys if len(nk) >= 2):
            out.append(s)
    return out


def _tag_candidates(lx: dict[str, Any]) -> list[str]:
    tags: list[str] = [TAG_5A]
    cat = str(lx.get("category") or "").strip()
    if cat and len(cat) <= _KW_MAX_LEN:
        tags.append(cat)

    ban = {
        normalize_name(str(lx.get("name") or "")),
        normalize_name(str(lx.get("officialName") or "")),
        normalize_name(str(lx.get("city") or "")),
        normalize_name(str(lx.get("province") or lx.get("region") or "")),
        normalize_name(cat),
        normalize_name(TAG_5A),
    }
    for al in lx.get("aliases") or []:
        ban.add(normalize_name(str(al)))

    for kw in lx.get("keywords") or []:
        s = str(kw).strip()
        if not s or len(s) > _KW_MAX_LEN or _KW_BAD.search(s):
            continue
        kn = normalize_name(s)
        if not kn or kn in ban:
            continue
        # drop pure alias-like duplicates already handled as aliases
        if any(kn in normalize_name(str(lx.get("name") or "")) for _ in [0]):
            # still allow short theme words that are not name substrings of full official
            pass
        tags.append(s)
        ban.add(kn)
    return _merge_str_list([], tags)


def _pinyin_initials(text: str, limit: int = 8) -> str:
    try:
        from pypinyin import lazy_pinyin, Style

        parts = lazy_pinyin(text, style=Style.FIRST_LETTER)
        s = "".join(parts)
    except Exception:
        s = re.sub(r"[^a-zA-Z0-9]+", "", text)
    s = re.sub(r"[^a-z0-9]", "", s.lower())
    return (s or "x")[:limit]


def make_new_id(city: str, name: str, used: set[str]) -> str:
    city_n = normalize_city(city)
    abbr = CITY_ABBR.get(city_n) or _pinyin_initials(city_n, 4)
    name_abbr = _pinyin_initials(name, 8)
    seq = 1
    while True:
        spot_id = f"spot_{abbr}_{name_abbr}_{seq:03d}"
        if spot_id not in used:
            used.add(spot_id)
            return spot_id
        seq += 1


def build_indexes(
    rows: list[dict[str, Any]],
) -> tuple[dict[tuple[str, str], list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    by_city: dict[tuple[str, str], list[dict[str, Any]]] = {}
    by_name: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        city_n = row.get("city_norm") or normalize_city(row.get("city_raw") or "")
        names = [row.get("name") or ""]
        names.extend(row.get("aliases") or [])
        for n in names:
            nn = normalize_name(str(n))
            if not nn:
                continue
            by_city.setdefault((nn, city_n), []).append(row)
            by_name.setdefault(nn, []).append(row)
    return by_city, by_name


def _dedupe_rows(cands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for r in cands:
        rid = r["id"]
        if rid in seen:
            continue
        seen.add(rid)
        out.append(r)
    return out


def _prefer_hit(hits: list[dict[str, Any]], lx: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve multi-hit: exact primary name > officialName > single survivor."""
    hits = _dedupe_rows(hits)
    if not hits:
        return None
    if len(hits) == 1:
        return hits[0]

    primary = {
        normalize_name(str(lx.get("name") or "")),
        normalize_name(str(lx.get("officialName") or "")),
    }
    primary.discard("")

    exact = [r for r in hits if normalize_name(str(r.get("name") or "")) in primary]
    if len(exact) == 1:
        return exact[0]
    pool = exact if exact else hits

    # true duplicate rows (same display name): pick richest then stable id
    if len({normalize_name(str(r.get("name") or "")) for r in pool}) == 1:

        def score(r: dict[str, Any]) -> tuple:
            tags = list(r.get("tags") or [])
            aliases = list(r.get("aliases") or [])
            payload = r.get("payload") if isinstance(r.get("payload"), dict) else {}
            intro = ""
            content = payload.get("content") if isinstance(payload.get("content"), dict) else {}
            intro = str(content.get("intro") or "")
            return (len(tags), len(aliases), len(intro), r["id"])

        return sorted(pool, key=score, reverse=True)[0]
    return None


def find_match(
    lx: dict[str, Any],
    by_city: dict[tuple[str, str], list[dict[str, Any]]],
    by_name: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, Any] | None, str]:
    """Return (row|None, status) status: matched|ambiguous|miss."""
    city_n = normalize_city(str(lx.get("city") or ""))
    prov = str(lx.get("province") or lx.get("region") or "").strip()
    query_names: list[str] = []
    for key in ("name", "officialName"):
        v = str(lx.get(key) or "").strip()
        if v:
            query_names.append(v)
    query_names.extend(str(x).strip() for x in (lx.get("aliases") or []) if str(x).strip())

    # 1) same city
    for q in query_names:
        nn = normalize_name(q)
        if not nn:
            continue
        hits = _dedupe_rows(by_city.get((nn, city_n), []))
        if not hits:
            continue
        picked = _prefer_hit(hits, lx)
        if picked is not None:
            return picked, "matched"
        return None, "ambiguous"

    # 2) name across kb
    for q in query_names:
        nn = normalize_name(q)
        if not nn:
            continue
        hits = _dedupe_rows(by_name.get(nn, []))
        if not hits:
            continue
        if prov:
            narrowed = [
                r
                for r in hits
                if prov in (r.get("region") or "")
                or prov
                in str((r.get("payload") or {}).get("content", {}).get("province") or "")
                or normalize_city(r.get("city_norm") or "")
                == normalize_city(str(lx.get("city") or ""))
            ]
            narrowed = _dedupe_rows(narrowed)
            if narrowed:
                hits = narrowed
        picked = _prefer_hit(hits, lx)
        if picked is not None:
            return picked, "matched"
        return None, "ambiguous"
    return None, "miss"


def merge_matched(row: dict[str, Any], lx: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    actions: dict[str, str] = {}
    payload = deepcopy(row.get("payload") or {})
    if not isinstance(payload, dict):
        payload = {}

    new_aliases = _merge_str_list(list(row.get("aliases") or []), _alias_candidates(lx))
    actions["aliases"] = "union" if new_aliases != list(row.get("aliases") or []) else "same"

    new_tags = _merge_str_list(list(row.get("tags") or []), _tag_candidates(lx))
    actions["tags"] = "union" if new_tags != list(row.get("tags") or []) else "same"

    cat = row.get("category")
    lx_cat = str(lx.get("category") or "").strip() or None
    if not (cat and str(cat).strip()) and lx_cat:
        cat = lx_cat
        actions["category"] = "fill"
    else:
        actions["category"] = "keep_existing" if cat else "skip"

    payload["aliases"] = new_aliases
    payload["tags"] = new_tags
    if cat:
        payload["category"] = cat

    meta = payload.get("_meta") if isinstance(payload.get("_meta"), dict) else {}
    meta["national_5a"] = True
    meta["national_5a_name"] = str(lx.get("name") or "")
    meta["national_5a_source"] = str(lx.get("dataSource") or "national-5a")
    if lx.get("sourceUrl"):
        meta["national_5a_url"] = str(lx.get("sourceUrl"))
    # keep raw keywords for audit; do not use as tags wholesale
    kws = [str(x).strip() for x in (lx.get("keywords") or []) if str(x).strip()]
    if kws:
        meta["national_5a_keywords"] = kws
    payload["_meta"] = meta

    return {
        "id": row["id"],
        "aliases": new_aliases,
        "tags": new_tags,
        "category": cat,
        "payload": payload,
        "updated_at": datetime.now(timezone.utc),
    }, actions


def new_row_from_5a(lx: dict[str, Any], spot_id: str) -> dict[str, Any]:
    name = str(lx.get("name") or "").strip()
    city = str(lx.get("city") or "").strip()
    province = str(lx.get("province") or lx.get("region") or "").strip()
    city_norm = normalize_city(city)
    aliases = _merge_str_list([], _alias_candidates(lx))
    tags = _tag_candidates(lx)
    category = str(lx.get("category") or "").strip() or None
    intro = str(lx.get("intro") or "").strip() or None
    tips = str(lx.get("tips") or "").strip() or None
    summary = str(lx.get("summary") or "").strip() or None
    highlights = [str(x).strip() for x in (lx.get("highlights") or []) if str(x).strip()]
    best_time = str(lx.get("bestTime") or "").strip() or None
    keywords = [str(x).strip() for x in (lx.get("keywords") or []) if str(x).strip()]

    payload = {
        "id": spot_id,
        "name": name,
        "aliases": aliases,
        "city": city,
        "region": province or city,
        "category": category,
        "tags": tags,
        "location": {"lat": None, "lng": None, "address": None, "geo_code": None},
        "visit_info": {
            "duration_hours": None,
            "opening_hours": None,
            "ticket": {
                "is_free": None,
                "adult_price": None,
                "currency": "CNY",
                "remark": "以景区票价为准",
            },
            "facilities": [],
            "transport": None,
        },
        "content": {
            "intro": intro,
            "summary": summary,
            "tips": tips,
            "highlights": highlights,
            "best_time": best_time,
            "province": province or None,
            "keywords": keywords,
        },
        "relations": {"nearby_spots": [], "recommended_routes": []},
        "media": {"cover": None, "photos": []},
        "rag_links": {
            "guide_chunk_ids": [],
            "last_updated": datetime.now(timezone.utc).date().isoformat(),
        },
        "_meta": {
            "national_5a": True,
            "national_5a_name": name,
            "national_5a_source": str(lx.get("dataSource") or "national-5a"),
            "national_5a_url": str(lx.get("sourceUrl") or ""),
            "national_5a_keywords": keywords,
        },
    }
    return {
        "id": spot_id,
        "name": name,
        "name_norm": normalize_name(name),
        "aliases": aliases,
        "city_raw": city,
        "city_norm": city_norm,
        "region": province or city,
        "category": category,
        "tags": tags,
        "has_intro": bool(intro),
        "payload": payload,
        "updated_at": datetime.now(timezone.utc),
    }


def fetch_all(conn) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, name, name_norm, aliases, city_raw, city_norm, region,
                   category, tags, payload
            FROM attraction_kb
            """
        )
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def main() -> None:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Phase C: national 5A → attraction_kb")
    parser.add_argument("--source", type=Path, default=N5A_DEFAULT)
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL", ""))
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=0, help="0 = all from offset")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.database_url:
        raise SystemExit("DATABASE_URL missing")
    if not args.source.exists():
        raise SystemExit(f"source not found: {args.source}")

    import psycopg

    all_5a = load_national_5a(args.source)
    end = len(all_5a) if args.limit <= 0 else min(len(all_5a), args.offset + args.limit)
    batch = all_5a[args.offset : end]
    print(f"source_total={len(all_5a)} batch={args.offset}:{end} size={len(batch)}")

    with psycopg.connect(args.database_url) as conn:
        rows = fetch_all(conn)
        used_ids = {r["id"] for r in rows}
        by_city, by_name = build_indexes(rows)
        print(f"kb_rows={len(rows)}")

        updates: list[tuple[dict[str, Any], dict[str, str], str]] = []
        inserts: list[dict[str, Any]] = []
        ambiguous: list[str] = []
        matched = 0

        for lx in batch:
            name = str(lx.get("name") or "").strip()
            city = str(lx.get("city") or "").strip()
            if not name:
                continue
            row, status = find_match(lx, by_city, by_name)
            label = f"{city}/{name}"
            if status == "matched" and row is not None:
                matched += 1
                merged, actions = merge_matched(row, lx)
                updates.append((merged, actions, label))
            elif status == "ambiguous":
                ambiguous.append(label)
            else:
                spot_id = make_new_id(city, name, used_ids)
                inserts.append(new_row_from_5a(lx, spot_id))

        def count_action(key: str, val: str) -> int:
            return sum(1 for _, acts, _ in updates if acts.get(key) == val)

        print("--- dry-run summary ---")
        print(
            f"matched={matched} inserts={len(inserts)} ambiguous={len(ambiguous)} "
            f"aliases_union={count_action('aliases','union')} tags_union={count_action('tags','union')}"
        )
        if ambiguous:
            print("ambiguous (skipped):")
            for line in ambiguous[:40]:
                print(" ", line)
            if len(ambiguous) > 40:
                print(f"  ... +{len(ambiguous) - 40} more")
        if inserts:
            print("new_rows (sample):")
            for row in inserts[:20]:
                print(f"  {row['city_raw']}/{row['name']} → {row['id']}")
            if len(inserts) > 20:
                print(f"  ... +{len(inserts) - 20} more")

        if not args.apply:
            print("dry-run only; re-run with --apply to write")
            return

        with conn.cursor() as cur:
            for merged, _, _ in updates:
                cur.execute(
                    UPDATE_SQL,
                    {
                        **merged,
                        "payload": Jsonb(merged["payload"]),
                    },
                )
            for row in inserts:
                cur.execute(
                    INSERT_SQL,
                    {
                        **row,
                        "payload": Jsonb(row["payload"]),
                    },
                )
        conn.commit()
        print(f"applied updates={len(updates)} inserts={len(inserts)}")


if __name__ == "__main__":
    main()

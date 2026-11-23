#!/usr/bin/env python3
"""Phase A: merge travel-lingxi-ai attraction-knowledge.js into attraction_kb.

Rules (confirmed):
- Do not overwrite existing copy; if intro/tips differ, append after (dedupe substring)
- province (Lingxi region) → payload.content.province; do NOT overwrite region hot column on updates
- nearby → relations.nearby_spots only when empty; names only, distance null
- bestTime → content.best_time
- aliases union; category fill-if-empty
- unmatched Lingxi rows → INSERT new id (coords left for Phase B)
- dry-run by default; --apply to write
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

LINGXI_DEFAULT = (
    ROOT.parent / "travel-lingxi-ai" / "dist" / "attraction-knowledge.js"
)
if not LINGXI_DEFAULT.exists():
    LINGXI_DEFAULT = ROOT / ".." / "travel-lingxi-ai" / "dist" / "attraction-knowledge.js"

UPDATE_SQL = """
UPDATE attraction_kb SET
  name = %(name)s,
  name_norm = %(name_norm)s,
  aliases = %(aliases)s,
  city_raw = %(city_raw)s,
  city_norm = %(city_norm)s,
  category = %(category)s,
  has_intro = %(has_intro)s,
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
  NULL, NULL, NULL, %(payload)s, 'lingxi_phase_a', %(updated_at)s
)
"""


def load_lingxi(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    # Prefer Node so JS object literals parse correctly.
    try:
        proc = subprocess.run(
            [
                "node",
                "-e",
                "const fs=require('fs');"
                "const s=fs.readFileSync(process.argv[1],'utf8');"
                "const m=s.match(/globalThis\\.travelAttractionKnowledge\\s*=\\s*(\\[[\\s\\S]*\\])\\s*;?\\s*$/);"
                "if(!m){console.error('parse fail'); process.exit(2)}"
                "const data=eval(m[1]); console.log(JSON.stringify(data));",
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
    except FileNotFoundError:
        pass

    # Fallback: crude JS→JSON (keys quoted)
    m = re.search(r"globalThis\.travelAttractionKnowledge\s*=\s*(\[[\s\S]*\])\s*;?\s*$", text)
    if not m:
        raise SystemExit(f"cannot parse Lingxi file: {path}")
    raw = m.group(1)
    raw = re.sub(r"(\n\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:", r'\1"\2":', raw)
    raw = raw.replace("'", '"')
    return json.loads(raw)


def _nonempty(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, str):
        return bool(v.strip())
    if isinstance(v, list):
        return len(v) > 0
    return True


def _append_text(existing: str | None, incoming: str | None) -> tuple[str | None, str]:
    """Return (new_value, action) where action in skip|fill|append|same."""
    inc = (incoming or "").strip()
    cur = (existing or "").strip() if existing is not None else ""
    if not inc:
        return (existing if existing is not None else None) or None, "skip"
    if not cur:
        return inc, "fill"
    if inc in cur or cur == inc:
        return cur, "same"
    return f"{cur}\n（补充）{inc}", "append"


def _merge_aliases(existing: list[str], incoming: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for x in list(existing or []) + list(incoming or []):
        s = str(x).strip()
        if not s:
            continue
        key = normalize_name(s)
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def _nearby_from_lingxi(names: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for n in names or []:
        s = str(n).strip()
        if not s:
            continue
        key = normalize_name(s)
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": s, "distance_km": None, "drive_minutes": None})
    return out


def _pinyin_initials(text: str, limit: int = 8) -> str:
    try:
        from pypinyin import lazy_pinyin, Style

        parts = lazy_pinyin(text, style=Style.FIRST_LETTER)
        s = "".join(parts)
    except Exception:
        s = re.sub(r"[^a-zA-Z0-9]+", "", text)
    s = re.sub(r"[^a-z0-9]", "", s.lower())
    return (s or "x")[:limit]


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
}


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


def build_index(rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    """(name_norm, city_norm) → row; also index aliases."""
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        city_n = row.get("city_norm") or normalize_city(row.get("city_raw") or "")
        names = [row.get("name") or ""]
        names.extend(row.get("aliases") or [])
        for n in names:
            nn = normalize_name(str(n))
            if not nn:
                continue
            key = (nn, city_n)
            # first wins for stable dedupe
            by_key.setdefault(key, row)
    return by_key


def merge_into_row(
    row: dict[str, Any],
    lx: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, str]]:
    """Return updated row dict + actions log."""
    actions: dict[str, str] = {}
    payload = deepcopy(row.get("payload") or {})
    if not isinstance(payload, dict):
        payload = {}
    content = payload.get("content") if isinstance(payload.get("content"), dict) else {}
    content = dict(content)
    relations = payload.get("relations") if isinstance(payload.get("relations"), dict) else {}
    relations = dict(relations)

    # province from Lingxi region
    province = str(lx.get("region") or "").strip()
    if province:
        if not _nonempty(content.get("province")):
            content["province"] = province
            actions["province"] = "fill"
        elif content.get("province") == province:
            actions["province"] = "same"
        else:
            actions["province"] = "keep_existing"

    # intro / tips: fill or append
    intro, a_intro = _append_text(content.get("intro"), lx.get("intro"))
    content["intro"] = intro
    actions["intro"] = a_intro
    tips, a_tips = _append_text(content.get("tips"), lx.get("tips"))
    content["tips"] = tips
    actions["tips"] = a_tips

    # highlights: only if empty
    lx_hl = lx.get("highlights") if isinstance(lx.get("highlights"), list) else []
    cur_hl = content.get("highlights") if isinstance(content.get("highlights"), list) else []
    if lx_hl and not cur_hl:
        content["highlights"] = [str(x).strip() for x in lx_hl if str(x).strip()]
        actions["highlights"] = "fill"
    else:
        actions["highlights"] = "skip" if not lx_hl else "keep_existing"

    # best_time
    bt = str(lx.get("bestTime") or "").strip()
    if bt:
        if not _nonempty(content.get("best_time")):
            content["best_time"] = bt
            actions["best_time"] = "fill"
        else:
            actions["best_time"] = "keep_existing"
    else:
        actions["best_time"] = "skip"

    # nearby → nearby_spots only if empty
    lx_near = lx.get("nearby") if isinstance(lx.get("nearby"), list) else []
    cur_near = relations.get("nearby_spots") if isinstance(relations.get("nearby_spots"), list) else []
    if lx_near and not cur_near:
        relations["nearby_spots"] = _nearby_from_lingxi(lx_near)
        actions["nearby_spots"] = "fill"
    else:
        actions["nearby_spots"] = "skip" if not lx_near else "keep_existing"
    relations.setdefault("recommended_routes", relations.get("recommended_routes") or [])

    payload["content"] = content
    payload["relations"] = relations

    # sync top-level mirrors used elsewhere
    if intro:
        payload["intro"] = intro  # optional legacy; content is source of truth

    # aliases union
    new_aliases = _merge_aliases(list(row.get("aliases") or []), list(lx.get("aliases") or []))
    if new_aliases != list(row.get("aliases") or []):
        actions["aliases"] = "union"
    else:
        actions["aliases"] = "same"

    # category fill-if-empty
    cat = row.get("category")
    lx_cat = str(lx.get("category") or "").strip() or None
    if not _nonempty(cat) and lx_cat:
        cat = lx_cat
        actions["category"] = "fill"
    else:
        actions["category"] = "keep_existing" if cat else "skip"

    # city_raw: keep existing on update; only fill if empty
    city_raw = row.get("city_raw") or ""
    lx_city = str(lx.get("city") or "").strip()
    if not city_raw and lx_city:
        city_raw = lx_city
        actions["city_raw"] = "fill"
    else:
        actions["city_raw"] = "keep_existing"
    city_norm = row.get("city_norm") or normalize_city(city_raw or lx_city)

    # payload city/region mirrors: do not overwrite region with province
    if lx_city and not _nonempty(payload.get("city")):
        payload["city"] = lx_city
    payload["name"] = row.get("name") or lx.get("name")
    if "rag_links" not in payload or not isinstance(payload.get("rag_links"), dict):
        payload["rag_links"] = {"guide_chunk_ids": [], "last_updated": datetime.now(timezone.utc).date().isoformat()}
    else:
        payload["rag_links"].setdefault("guide_chunk_ids", [])

    has_intro = bool(str(content.get("intro") or "").strip())

    updated = {
        "id": row["id"],
        "name": row.get("name") or str(lx.get("name") or "").strip(),
        "name_norm": normalize_name(row.get("name") or str(lx.get("name") or "")),
        "aliases": new_aliases,
        "city_raw": city_raw,
        "city_norm": city_norm,
        "category": cat,
        "has_intro": has_intro,
        "payload": payload,
        "updated_at": datetime.now(timezone.utc),
    }
    return updated, actions


def new_row_from_lingxi(lx: dict[str, Any], spot_id: str) -> dict[str, Any]:
    name = str(lx.get("name") or "").strip()
    city = str(lx.get("city") or "").strip()
    province = str(lx.get("region") or "").strip()
    city_norm = normalize_city(city)
    intro = str(lx.get("intro") or "").strip() or None
    tips = str(lx.get("tips") or "").strip() or None
    highlights = [str(x).strip() for x in (lx.get("highlights") or []) if str(x).strip()]
    best_time = str(lx.get("bestTime") or "").strip() or None
    nearby = _nearby_from_lingxi(list(lx.get("nearby") or []))
    aliases = _merge_aliases([], list(lx.get("aliases") or []))
    category = str(lx.get("category") or "").strip() or None

    payload = {
        "id": spot_id,
        "name": name,
        "aliases": aliases,
        "city": city,
        "region": city,  # keep city-ish region; province separate
        "category": category,
        "tags": [],
        "location": {"lat": None, "lng": None, "address": None, "geo_code": None},
        "visit_info": {
            "duration_hours": None,
            "opening_hours": None,
            "ticket": {"is_free": None, "adult_price": None, "currency": "CNY", "remark": None},
            "facilities": [],
            "transport": None,
        },
        "content": {
            "intro": intro,
            "tips": tips,
            "highlights": highlights,
            "best_time": best_time,
            "province": province or None,
        },
        "relations": {"nearby_spots": nearby, "recommended_routes": []},
        "media": {"cover": None, "photos": []},
        "rag_links": {"guide_chunk_ids": [], "last_updated": datetime.now(timezone.utc).date().isoformat()},
    }
    return {
        "id": spot_id,
        "name": name,
        "name_norm": normalize_name(name),
        "aliases": aliases,
        "city_raw": city,
        "city_norm": city_norm,
        "region": city,  # do not put province here
        "category": category,
        "tags": [],
        "has_intro": bool(intro),
        "payload": payload,
        "updated_at": datetime.now(timezone.utc),
    }


def fetch_all(conn) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, name, name_norm, aliases, city_raw, city_norm, region,
                   category, tags, has_intro, payload
            FROM attraction_kb
            """
        )
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def main() -> None:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Phase A: Lingxi → attraction_kb")
    parser.add_argument("--lingxi", type=Path, default=LINGXI_DEFAULT)
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL", ""))
    parser.add_argument("--apply", action="store_true", help="Write DB (default dry-run)")
    args = parser.parse_args()
    if not args.database_url:
        raise SystemExit("DATABASE_URL missing")
    if not args.lingxi.exists():
        raise SystemExit(f"Lingxi file not found: {args.lingxi}")

    import psycopg

    lingxi = load_lingxi(args.lingxi)
    print(f"lingxi_loaded={len(lingxi)} from {args.lingxi}")

    with psycopg.connect(args.database_url) as conn:
        rows = fetch_all(conn)
        used_ids = {r["id"] for r in rows}
        index = build_index(rows)
        print(f"kb_rows={len(rows)}")

        updates: list[tuple[dict[str, Any], dict[str, str], str]] = []
        inserts: list[dict[str, Any]] = []
        unmatched_names: list[str] = []
        matched = 0

        for lx in lingxi:
            name = str(lx.get("name") or "").strip()
            city = str(lx.get("city") or "").strip()
            if not name:
                continue
            city_n = normalize_city(city)
            name_n = normalize_name(name)
            row = index.get((name_n, city_n))
            if row is None:
                # try aliases of lingxi against index
                for al in lx.get("aliases") or []:
                    row = index.get((normalize_name(str(al)), city_n))
                    if row:
                        break
            if row is None:
                # city-less fallback: unique name_norm across kb
                cands = [r for r in rows if normalize_name(r.get("name") or "") == name_n]
                if len(cands) == 1:
                    row = cands[0]

            if row is not None:
                matched += 1
                merged, actions = merge_into_row(row, lx)
                updates.append((merged, actions, name))
            else:
                spot_id = make_new_id(city, name, used_ids)
                inserts.append(new_row_from_lingxi(lx, spot_id))
                unmatched_names.append(f"{city}/{name} → INSERT {spot_id}")

        # summarize actions
        def count_action(key: str, val: str) -> int:
            return sum(1 for _, acts, _ in updates if acts.get(key) == val)

        print("--- dry-run summary ---")
        print(f"matched_updates={matched} inserts={len(inserts)}")
        for field in ("intro", "tips", "highlights", "best_time", "nearby_spots", "province", "aliases", "category"):
            fill = count_action(field, "fill")
            append = count_action(field, "append")
            keep = count_action(field, "keep_existing")
            same = count_action(field, "same")
            union = count_action(field, "union")
            print(
                f"  {field}: fill={fill} append={append} keep={keep} same={same} union={union}"
            )
        if inserts:
            print("new_rows:")
            for line in unmatched_names[:30]:
                print(" ", line)
            if len(unmatched_names) > 30:
                print(f"  ... +{len(unmatched_names) - 30} more")

        if not args.apply:
            print("dry-run only; re-run with --apply to write")
            return

        with conn.cursor() as cur:
            for merged, _acts, _name in updates:
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

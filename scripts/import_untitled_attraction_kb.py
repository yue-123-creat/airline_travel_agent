#!/usr/bin/env python3
"""Parse untitled-1.json (concatenated objects), dedupe vs attraction_kb, write SQL + upsert."""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.config import get_settings
from app.travel.kb_normalize import normalize_city, normalize_name

load_dotenv(ROOT / ".env", override=True)

import importlib.util

_spec = importlib.util.spec_from_file_location(
    "import_attraction_kb", ROOT / "scripts" / "import_attraction_kb.py"
)
assert _spec and _spec.loader
_imp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_imp)
UPSERT_ENRICHED = _imp.UPSERT_ENRICHED
build_enriched_row = _imp.build_enriched_row

SRC = Path(r"d:\Projects\untitled-1.json")
OUT_SQL = ROOT / "scripts" / "attraction_kb.sql"


def _pinyin_initials(text: str, limit: int = 8) -> str:
    try:
        from pypinyin import Style, lazy_pinyin

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
    "海口": "hk",
    "拉萨": "ls",
    "呼伦贝尔": "hlbe",
    "张家界": "zjj",
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


def load_concat_json(path: Path) -> list[dict[str, Any]]:
    raw = path.read_text(encoding="utf-8")
    decoder = json.JSONDecoder()
    objs: list[dict[str, Any]] = []
    i = 0
    n = len(raw)
    while i < n:
        while i < n and raw[i].isspace():
            i += 1
        if i >= n:
            break
        obj, end = decoder.raw_decode(raw, i)
        if isinstance(obj, dict):
            objs.append(obj)
        i = end
    return objs


def sql_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def sql_text_array(items: list[str]) -> str:
    if not items:
        return "'{}'::text[]"
    inner = ",".join(sql_quote(x) for x in items)
    return f"ARRAY[{inner}]::text[]"


def sql_nullable_num(v: Any) -> str:
    if v is None:
        return "NULL"
    return str(v)


def sql_nullable_bool(v: Any) -> str:
    if v is None:
        return "NULL"
    return "TRUE" if v else "FALSE"


def sql_nullable_float(v: Any) -> str:
    if v is None:
        return "NULL"
    return repr(float(v))


def row_to_sql(row: dict[str, Any]) -> str:
    payload = row["payload"]
    if isinstance(payload, Jsonb):
        payload_obj = payload.obj
    else:
        payload_obj = payload
    payload_json = json.dumps(payload_obj, ensure_ascii=False)
    updated = row["updated_at"]
    if isinstance(updated, datetime):
        ts = updated.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S%z")
    else:
        ts = str(updated)
    cols = (
        f"{sql_quote(row['id'])}, {sql_quote(row['name'])}, {sql_quote(row['name_norm'])}, "
        f"{sql_text_array(list(row['aliases'] or []))}, {sql_quote(row['city_raw'])}, "
        f"{sql_quote(row['city_norm'])}, {sql_quote(row['region'])}, "
        f"{('NULL' if row['category'] is None else sql_quote(row['category']))}, "
        f"{sql_text_array(list(row['tags'] or []))}, "
        f"{sql_nullable_bool(row['is_free'])}, {sql_nullable_num(row['adult_price'])}, "
        f"{sql_quote(row['currency'])}, {sql_nullable_bool(row['has_cover'])}, "
        f"{sql_nullable_bool(row['has_intro'])}, {int(row['chunk_count'] or 0)}, "
        f"{sql_nullable_float(row['lat'])}, {sql_nullable_float(row['lng'])}, "
        f"{('NULL' if not row.get('geo_code') else sql_quote(str(row['geo_code'])))}, "
        f"{sql_quote(payload_json)}::jsonb, {sql_quote(row['source'])}, "
        f"{sql_quote(ts)}::timestamptz"
    )
    return (
        "INSERT INTO attraction_kb (\n"
        "    id, name, name_norm, aliases, city_raw, city_norm, region, category, tags,\n"
        "    is_free, adult_price, currency, has_cover, has_intro, chunk_count,\n"
        "    lat, lng, geo_code, payload, source, updated_at\n"
        f") VALUES (\n    {cols}\n)\n"
        "ON CONFLICT (id) DO NOTHING;\n"
    )


def main() -> None:
    if not SRC.is_file():
        raise SystemExit(f"missing source: {SRC}")

    spots = load_concat_json(SRC)
    print(f"loaded {len(spots)} objects from {SRC}")

    import psycopg

    s = get_settings()
    with psycopg.connect(s.database_url, row_factory=dict_row) as conn:
        rows = conn.execute(
            """
            SELECT id, name, name_norm, aliases, city_norm, city_raw,
                   payload->'_meta'->>'amap_poi_id' AS amap_poi_id
            FROM attraction_kb
            """
        ).fetchall()

        used_ids = {str(r["id"]) for r in rows}
        by_name_city: set[tuple[str, str]] = set()
        by_poi: set[str] = set()
        for r in rows:
            city_n = str(r.get("city_norm") or normalize_city(r.get("city_raw") or ""))
            nn = str(r.get("name_norm") or normalize_name(r.get("name") or ""))
            if nn:
                by_name_city.add((nn, city_n))
            for a in r.get("aliases") or []:
                an = normalize_name(str(a))
                if an:
                    by_name_city.add((an, city_n))
            poi = str(r.get("amap_poi_id") or "").strip()
            if poi:
                by_poi.add(poi)

        to_insert: list[dict[str, Any]] = []
        skipped_db = 0
        skipped_self = 0
        seen_file: set[tuple[str, str]] = set()
        seen_poi_file: set[str] = set()

        for spot in spots:
            name = str(spot.get("name") or "").strip()
            if not name:
                skipped_self += 1
                continue
            city_raw = str(spot.get("city") or "").strip()
            city_n = normalize_city(city_raw)
            name_n = normalize_name(name)
            meta = spot.get("_meta") if isinstance(spot.get("_meta"), dict) else {}
            poi = str(meta.get("amap_poi_id") or "").strip()

            if poi and (poi in by_poi or poi in seen_poi_file):
                skipped_db += 1
                continue
            keys = [(name_n, city_n)]
            for a in spot.get("aliases") or []:
                an = normalize_name(str(a))
                if an:
                    keys.append((an, city_n))
            if any(k in by_name_city for k in keys) or any(k in seen_file for k in keys):
                if any(k in by_name_city for k in keys):
                    skipped_db += 1
                else:
                    skipped_self += 1
                continue

            spot_id = make_new_id(city_raw or city_n, name, used_ids)
            spot_with_id = dict(spot)
            spot_with_id["id"] = spot_id
            # keep amap meta inside payload via build_enriched_row (strips _meta)
            # re-attach useful meta under payload._meta for browse/debug
            row = build_enriched_row(spot_with_id)
            if not row:
                skipped_self += 1
                continue
            payload_obj = row["payload"].obj if isinstance(row["payload"], Jsonb) else dict(row["payload"])
            payload_obj["_meta"] = {
                "amap_poi_id": meta.get("amap_poi_id"),
                "amap_poi_name": meta.get("amap_poi_name"),
                "source_city": meta.get("source_city"),
                "source_province": meta.get("source_province"),
                "source_district": meta.get("source_district"),
                "import_batch": "untitled-1",
            }
            row["payload"] = Jsonb(payload_obj)
            to_insert.append(row)
            for k in keys:
                seen_file.add(k)
                by_name_city.add(k)
            if poi:
                seen_poi_file.add(poi)
                by_poi.add(poi)

        sql_parts = [
            "-- Auto-generated from untitled-1.json → attraction_kb (deduped)\n",
            f"-- generated_at={datetime.now(timezone.utc).isoformat()}\n",
            f"-- source_objects={len(spots)} insert={len(to_insert)} "
            f"skip_db_or_dup={skipped_db} skip_self={skipped_self}\n\n",
            "BEGIN;\n\n",
        ]
        for row in to_insert:
            sql_parts.append(row_to_sql(row))
            sql_parts.append("\n")
        sql_parts.append("COMMIT;\n")
        OUT_SQL.write_text("".join(sql_parts), encoding="utf-8")
        print(f"wrote {OUT_SQL} rows={len(to_insert)}")

        ok = 0
        with conn.cursor() as cur:
            for row in to_insert:
                cur.execute(UPSERT_ENRICHED, row)
                ok += 1
            conn.commit()
        print(f"upserted={ok} skipped_existing={skipped_db} skipped_file_dup={skipped_self}")
        total = conn.execute("SELECT COUNT(*)::int AS c FROM attraction_kb").fetchone()["c"]
        print(f"attraction_kb total={total}")


if __name__ == "__main__":
    main()

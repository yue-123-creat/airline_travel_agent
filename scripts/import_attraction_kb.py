#!/usr/bin/env python3
"""Import attractions_enriched.json → attraction_kb (and optional legacy).

Rules:
- adult_price: only numeric values; never coerce missing/unknown to 0 (store NULL)
- rag_links.guide_chunk_ids: always present as [] if missing
- payload: strip _meta
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from psycopg.types.json import Jsonb

from app.travel.kb_normalize import normalize_city, normalize_name

DDL_PATH = ROOT / "scripts" / "init_attraction_kb.sql"
ENRICHED_DEFAULT = ROOT / "data" / "knowledge" / "attractions_enriched.json"
LEGACY_DEFAULT = ROOT / "data" / "knowledge" / "travel_knowledge.json"

UPSERT_ENRICHED = """
INSERT INTO attraction_kb (
    id, name, name_norm, aliases, city_raw, city_norm, region, category, tags,
    is_free, adult_price, currency, has_cover, has_intro, chunk_count,
    lat, lng, geo_code, payload, source, updated_at
) VALUES (
    %(id)s, %(name)s, %(name_norm)s, %(aliases)s, %(city_raw)s, %(city_norm)s,
    %(region)s, %(category)s, %(tags)s, %(is_free)s, %(adult_price)s, %(currency)s,
    %(has_cover)s, %(has_intro)s, %(chunk_count)s, %(lat)s, %(lng)s, %(geo_code)s,
    %(payload)s, %(source)s, %(updated_at)s
)
ON CONFLICT (id) DO UPDATE SET
    name = EXCLUDED.name,
    name_norm = EXCLUDED.name_norm,
    aliases = EXCLUDED.aliases,
    city_raw = EXCLUDED.city_raw,
    city_norm = EXCLUDED.city_norm,
    region = EXCLUDED.region,
    category = EXCLUDED.category,
    tags = EXCLUDED.tags,
    is_free = EXCLUDED.is_free,
    adult_price = EXCLUDED.adult_price,
    currency = EXCLUDED.currency,
    has_cover = EXCLUDED.has_cover,
    has_intro = EXCLUDED.has_intro,
    chunk_count = EXCLUDED.chunk_count,
    lat = EXCLUDED.lat,
    lng = EXCLUDED.lng,
    geo_code = EXCLUDED.geo_code,
    payload = EXCLUDED.payload,
    source = EXCLUDED.source,
    updated_at = EXCLUDED.updated_at
"""

UPSERT_LEGACY = """
INSERT INTO attraction_kb_legacy (id, name, city_raw, region, payload, updated_at)
VALUES (%(id)s, %(name)s, %(city_raw)s, %(region)s, %(payload)s, NOW())
ON CONFLICT (id) DO UPDATE SET
    name = EXCLUDED.name,
    city_raw = EXCLUDED.city_raw,
    region = EXCLUDED.region,
    payload = EXCLUDED.payload,
    updated_at = NOW()
"""


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if item is None:
            continue
        s = str(item).strip()
        if s:
            out.append(s)
    return out


def parse_adult_price(raw: Any) -> Decimal | None:
    """Only real numbers → Decimal; missing/invalid → None. Never invent 0."""
    if raw is None or raw is False:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return Decimal(str(raw))
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        try:
            return Decimal(text)
        except InvalidOperation:
            return None
    return None


def _parse_updated_at(raw: Any) -> datetime:
    if isinstance(raw, str) and raw.strip():
        for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                dt = datetime.strptime(raw.strip(), fmt)
                if dt.tzinfo is None:
                    return dt.replace(tzinfo=timezone.utc)
                return dt
            except ValueError:
                continue
    return datetime.now(timezone.utc)


def build_enriched_row(spot: dict[str, Any]) -> dict[str, Any] | None:
    name = str(spot.get("name") or "").strip()
    if not name:
        return None

    spot_id = str(spot.get("id") or "").strip()
    city_raw = str(spot.get("city") or "").strip()
    if not spot_id:
        spot_id = f"legacy-import:{name}:{city_raw or 'unknown'}"

    visit = spot.get("visit_info") if isinstance(spot.get("visit_info"), dict) else {}
    ticket = visit.get("ticket") if isinstance(visit.get("ticket"), dict) else {}
    location = spot.get("location") if isinstance(spot.get("location"), dict) else {}
    content = spot.get("content") if isinstance(spot.get("content"), dict) else {}
    media = spot.get("media") if isinstance(spot.get("media"), dict) else {}
    rag = spot.get("rag_links") if isinstance(spot.get("rag_links"), dict) else {}

    chunk_ids = rag.get("guide_chunk_ids")
    if not isinstance(chunk_ids, list):
        chunk_ids = []

    intro = content.get("intro")
    if intro is None:
        intro = spot.get("intro")
    cover = media.get("cover")
    has_intro = bool(str(intro).strip()) if intro is not None else False
    has_cover = bool(str(cover).strip()) if cover is not None else False

    adult_price = parse_adult_price(ticket.get("adult_price"))
    is_free = ticket.get("is_free")
    if is_free is not None and not isinstance(is_free, bool):
        is_free = None

    lat = location.get("lat")
    lng = location.get("lng")
    try:
        lat_f = float(lat) if lat is not None else None
    except (TypeError, ValueError):
        lat_f = None
    try:
        lng_f = float(lng) if lng is not None else None
    except (TypeError, ValueError):
        lng_f = None

    payload = dict(spot)
    payload.pop("_meta", None)
    payload.setdefault("rag_links", {})
    if not isinstance(payload["rag_links"], dict):
        payload["rag_links"] = {}
    payload["rag_links"]["guide_chunk_ids"] = list(chunk_ids)
    if "last_updated" not in payload["rag_links"] and rag.get("last_updated"):
        payload["rag_links"]["last_updated"] = rag.get("last_updated")

    currency = str(ticket.get("currency") or "CNY").strip() or "CNY"

    return {
        "id": spot_id,
        "name": name,
        "name_norm": normalize_name(name),
        "aliases": _as_str_list(spot.get("aliases")),
        "city_raw": city_raw,
        "city_norm": normalize_city(city_raw),
        "region": str(spot.get("region") or "").strip(),
        "category": (str(spot["category"]).strip() if spot.get("category") is not None else None),
        "tags": _as_str_list(spot.get("tags")),
        "is_free": is_free,
        "adult_price": adult_price,  # None → SQL NULL
        "currency": currency,
        "has_cover": has_cover,
        "has_intro": has_intro,
        "chunk_count": len(chunk_ids),
        "lat": lat_f,
        "lng": lng_f,
        "geo_code": (str(location["geo_code"]).strip() if location.get("geo_code") else None),
        "payload": Jsonb(payload),
        "source": "enriched",
        "updated_at": _parse_updated_at(rag.get("last_updated")),
    }


def build_legacy_row(spot: dict[str, Any]) -> dict[str, Any] | None:
    name = str(spot.get("name") or "").strip()
    if not name:
        return None
    city_raw = str(spot.get("city") or "").strip()
    region = str(spot.get("region") or "").strip() or None
    return {
        "id": f"legacy:{name}:{city_raw or 'unknown'}",
        "name": name,
        "city_raw": city_raw or None,
        "region": region,
        "payload": Jsonb(spot),
    }


def apply_ddl(conn) -> None:
    sql = DDL_PATH.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
    print(f"applied DDL: {DDL_PATH}")


def import_enriched(conn, path: Path) -> tuple[int, int, int]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise SystemExit(f"expected list in {path}")
    ok = skip = null_price = 0
    with conn.cursor() as cur:
        for spot in data:
            if not isinstance(spot, dict):
                skip += 1
                continue
            row = build_enriched_row(spot)
            if not row:
                skip += 1
                continue
            if row["adult_price"] is None:
                null_price += 1
            cur.execute(UPSERT_ENRICHED, row)
            ok += 1
    conn.commit()
    return ok, skip, null_price


def import_legacy(conn, path: Path) -> tuple[int, int]:
    data = json.loads(path.read_text(encoding="utf-8"))
    attractions = data.get("attractions") if isinstance(data, dict) else None
    if not isinstance(attractions, list):
        raise SystemExit(f"expected .attractions list in {path}")
    ok = skip = 0
    with conn.cursor() as cur:
        for spot in attractions:
            if not isinstance(spot, dict):
                skip += 1
                continue
            row = build_legacy_row(spot)
            if not row:
                skip += 1
                continue
            cur.execute(UPSERT_LEGACY, row)
            ok += 1
    conn.commit()
    return ok, skip


def main() -> None:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Import attraction KB into Postgres")
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL", ""))
    parser.add_argument("--enriched", type=Path, default=ENRICHED_DEFAULT)
    parser.add_argument("--legacy", type=Path, default=None, help="Path to travel_knowledge.json")
    parser.add_argument("--with-legacy", action="store_true", help=f"Also import {LEGACY_DEFAULT.name}")
    parser.add_argument("--apply-ddl", action="store_true", help="Create tables/indexes first")
    args = parser.parse_args()

    if not args.database_url:
        raise SystemExit("DATABASE_URL missing (set env or --database-url)")

    import psycopg

    legacy_path = args.legacy
    if args.with_legacy and legacy_path is None:
        legacy_path = LEGACY_DEFAULT

    with psycopg.connect(args.database_url) as conn:
        if args.apply_ddl:
            apply_ddl(conn)
        ok, skip, null_price = import_enriched(conn, args.enriched)
        print(
            f"enriched upserted={ok} skipped={skip} adult_price_null={null_price} "
            f"from {args.enriched}"
        )
        if legacy_path is not None:
            lok, lskip = import_legacy(conn, legacy_path)
            print(f"legacy upserted={lok} skipped={lskip} from {legacy_path}")

        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM attraction_kb")
            total = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM attraction_kb WHERE adult_price IS NULL")
            nulls = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM attraction_kb WHERE adult_price = 0")
            zeros = cur.fetchone()[0]
            cur.execute(
                "SELECT COUNT(*) FROM attraction_kb "
                "WHERE COALESCE(jsonb_array_length(payload->'rag_links'->'guide_chunk_ids'), 0) = 0"
            )
            empty_chunks = cur.fetchone()[0]
        print(
            f"verify attraction_kb total={total} adult_price_null={nulls} "
            f"adult_price_zero={zeros} empty_guide_chunk_ids={empty_chunks}"
        )


if __name__ == "__main__":
    main()

"""Postgres search for attraction_kb (formal enriched path)."""

from __future__ import annotations

import logging
from typing import Any

import psycopg
from psycopg.rows import dict_row

from app.config import get_settings
from app.travel.query_intent import TravelIntent, parse_travel_intent

logger = logging.getLogger(__name__)

_SEARCH_SQL = """
SELECT
  id,
  name,
  city_norm,
  city_raw,
  region,
  category,
  tags,
  adult_price,
  is_free,
  has_cover,
  has_intro,
  chunk_count,
  lat,
  lng,
  payload,
  CASE
    WHEN %(name_norm)s <> '' AND name_norm = %(name_norm)s THEN 3
    WHEN %(name_query)s <> '' AND name ILIKE '%%' || %(name_query)s || '%%' THEN 2
    WHEN %(name_query)s <> '' AND %(name_query)s = ANY (aliases) THEN 2
    WHEN char_length(%(name_query)s) >= 4
         AND %(name_query)s ILIKE '%%' || name || '%%' THEN 1
    WHEN char_length(%(name_query)s) >= 4
         AND EXISTS (
           SELECT 1 FROM unnest(aliases) AS a(alias)
           WHERE %(name_query)s ILIKE '%%' || a.alias || '%%'
              OR a.alias ILIKE '%%' || %(name_query)s || '%%'
         ) THEN 1
    ELSE 0
  END AS name_hit,
  CASE
    WHEN cardinality(%(user_tags)s::text[]) = 0 THEN 0
    ELSE (
      SELECT COUNT(*)::int
      FROM unnest(tags) AS t(tag)
      WHERE t.tag = ANY (%(user_tags)s::text[])
    )
  END AS tag_overlap
FROM attraction_kb
WHERE
  (%(city_norm)s = '' OR city_norm = %(city_norm)s)
  AND (
    cardinality(%(province_norms)s::text[]) = 0
    OR %(city_norm)s <> ''
    OR EXISTS (
      SELECT 1
      FROM unnest(%(province_norms)s::text[]) AS p(prov)
      WHERE region = p.prov
         OR region ILIKE p.prov || '%%'
         OR coalesce(payload->'content'->>'province', '') ILIKE '%%' || p.prov || '%%'
         OR city_raw ILIKE '%%' || p.prov || '%%'
         OR name ILIKE '%%' || p.prov || '%%'
    )
  )
  AND (
    cardinality(%(user_tags)s::text[]) = 0
    OR tags @> %(user_tags)s::text[]
  )
  AND (
    %(max_price)s::numeric IS NULL
    OR (adult_price IS NOT NULL AND adult_price < %(max_price)s::numeric)
  )
  AND (
    %(name_query)s = ''
    OR name_norm = %(name_norm)s
    OR name ILIKE '%%' || %(name_query)s || '%%'
    OR %(name_query)s = ANY (aliases)
    OR (
      char_length(%(name_query)s) >= 4
      AND (
        %(name_query)s ILIKE '%%' || name || '%%'
        OR EXISTS (
          SELECT 1 FROM unnest(aliases) AS a(alias)
          WHERE %(name_query)s ILIKE '%%' || a.alias || '%%'
             OR a.alias ILIKE '%%' || %(name_query)s || '%%'
        )
      )
    )
    OR %(city_norm)s <> ''
    OR cardinality(%(province_norms)s::text[]) > 0
    OR cardinality(%(user_tags)s::text[]) > 0
  )
ORDER BY
  name_hit DESC,
  tag_overlap DESC,
  has_cover DESC,
  has_intro DESC,
  chunk_count DESC,
  CASE WHEN %(price_sort)s = 'asc' THEN adult_price END ASC NULLS LAST,
  CASE WHEN %(price_sort)s = 'desc' THEN adult_price END DESC NULLS LAST,
  name ASC
LIMIT %(limit)s
"""


def count_attraction_kb() -> int | None:
    try:
        with psycopg.connect(get_settings().database_url) as conn:
            row = conn.execute("SELECT COUNT(*) FROM attraction_kb").fetchone()
            return int(row[0]) if row else 0
    except Exception as e:
        logger.warning("attraction_kb count failed: %s", e)
        return None


def search_attraction_kb(
    query: str,
    *,
    limit: int = 5,
    intent: TravelIntent | None = None,
) -> list[dict[str, Any]] | None:
    """Return hits list, or None if Postgres is unavailable (caller may file-fallback).

    Empty list means query ran OK but no rows — do **not** fall back to file.
    """
    intent = intent or parse_travel_intent(query)
    province_norms = list(intent.province_norms or [])
    if not province_norms and intent.province_norm:
        province_norms = [intent.province_norm]
    limit_n = max(1, min(limit, 20))

    # 多省：每省各取一批再交错合并，避免一侧省份占满 TopN
    if len(province_norms) >= 2 and not (intent.city_norm or ""):
        per = max(2, (limit_n + len(province_norms) - 1) // len(province_norms))
        buckets: list[list[dict[str, Any]]] = []
        try:
            with psycopg.connect(get_settings().database_url, row_factory=dict_row) as conn:
                for prov in province_norms:
                    params = {
                        "city_norm": "",
                        "province_norms": [prov],
                        "user_tags": intent.tags or [],
                        "max_price": intent.max_price,
                        "price_sort": intent.price_sort or "none",
                        "name_query": intent.name_query or "",
                        "name_norm": intent.name_norm or "",
                        "limit": per,
                    }
                    rows = conn.execute(_SEARCH_SQL, params).fetchall()
                    buckets.append([_row_to_hit(r) for r in rows])
        except Exception as e:
            logger.warning("attraction_kb search failed: %s", e)
            return None
        hits: list[dict[str, Any]] = []
        seen: set[str] = set()
        progressed = True
        while progressed and len(hits) < limit_n:
            progressed = False
            for bucket in buckets:
                if not bucket:
                    continue
                item = bucket.pop(0)
                rid = str(item.get("id") or "")
                if rid and rid in seen:
                    continue
                if rid:
                    seen.add(rid)
                hits.append(item)
                progressed = True
                if len(hits) >= limit_n:
                    break
        return hits

    params = {
        "city_norm": intent.city_norm or "",
        "province_norms": province_norms,
        "user_tags": intent.tags or [],
        "max_price": intent.max_price,
        "price_sort": intent.price_sort or "none",
        "name_query": intent.name_query or "",
        "name_norm": intent.name_norm or "",
        "limit": limit_n,
    }
    try:
        with psycopg.connect(get_settings().database_url, row_factory=dict_row) as conn:
            rows = conn.execute(_SEARCH_SQL, params).fetchall()
    except Exception as e:
        logger.warning("attraction_kb search failed: %s", e)
        return None
    return [_row_to_hit(r) for r in rows]


def _row_to_hit(row: dict[str, Any]) -> dict[str, Any]:
    payload = row.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {}
    item = dict(payload)
    item["id"] = row["id"]
    item["name"] = row["name"]
    item["city"] = row.get("city_raw") or row.get("city_norm") or item.get("city") or ""
    item["city_norm"] = row.get("city_norm") or ""
    item["region"] = row.get("region") or item.get("region") or ""
    item["category"] = row.get("category")
    item["tags"] = list(row.get("tags") or [])
    item["adult_price"] = float(row["adult_price"]) if row.get("adult_price") is not None else None
    item["is_free"] = row.get("is_free")
    item["lat"] = row.get("lat")
    item["lng"] = row.get("lng")
    item["guide_chunk_ids"] = list(
        ((payload.get("rag_links") or {}) if isinstance(payload.get("rag_links"), dict) else {}).get(
            "guide_chunk_ids"
        )
        or []
    )
    item["score"] = int(row.get("name_hit") or 0) * 100 + int(row.get("tag_overlap") or 0) * 10
    item["_kb"] = "postgres"
    return item

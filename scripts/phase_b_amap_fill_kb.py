#!/usr/bin/env python3
"""Phase B: fill factual gaps in attraction_kb via Amap (no invented copy).

Fills only when empty / null (selectable via --only):
- lat/lng/address/geo_code
- visit_info.opening_hours
- adult_price / is_free / ticket remark (never fake 0 for unknown; often skipped)
- relations.nearby_spots (names + distance when Amap returns)
- existing nearby name-only rows: fill distance_km / drive_minutes when matchable

Does NOT touch intro/tips/highlights/best_time/province/duration_hours.

Examples:
  python scripts/phase_b_amap_fill_kb.py --only open,nearby
  python scripts/phase_b_amap_fill_kb.py --only open,nearby --apply
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from psycopg.types.json import Jsonb

from app.config import get_settings
from app.tools.amap import amap_poi_snapshot_by_name
from app.travel.kb_normalize import normalize_name

UPDATE_SQL = """
UPDATE attraction_kb SET
  aliases = %(aliases)s,
  is_free = %(is_free)s,
  adult_price = %(adult_price)s,
  has_cover = %(has_cover)s,
  has_intro = %(has_intro)s,
  lat = %(lat)s,
  lng = %(lng)s,
  geo_code = %(geo_code)s,
  payload = %(payload)s,
  updated_at = %(updated_at)s
WHERE id = %(id)s
"""


def _blank(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _need_open(payload: dict[str, Any]) -> bool:
    visit = payload.get("visit_info") if isinstance(payload.get("visit_info"), dict) else {}
    return not _blank(visit.get("opening_hours"))


def _need_price(row: dict[str, Any], payload: dict[str, Any]) -> bool:
    if row.get("adult_price") is not None:
        return False
    visit = payload.get("visit_info") if isinstance(payload.get("visit_info"), dict) else {}
    ticket = visit.get("ticket") if isinstance(visit.get("ticket"), dict) else {}
    return ticket.get("adult_price") is None and ticket.get("is_free") is None


def _need_coords(row: dict[str, Any], payload: dict[str, Any]) -> bool:
    if row.get("lat") is not None and row.get("lng") is not None:
        return False
    loc = payload.get("location") if isinstance(payload.get("location"), dict) else {}
    return loc.get("lat") is None or loc.get("lng") is None


def _nearby_list(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rel = payload.get("relations") if isinstance(payload.get("relations"), dict) else {}
    spots = rel.get("nearby_spots") if isinstance(rel.get("nearby_spots"), list) else []
    return [s for s in spots if isinstance(s, dict)]


def _need_nearby_fill(payload: dict[str, Any]) -> bool:
    spots = _nearby_list(payload)
    if not spots:
        return True
    return any(s.get("distance_km") is None for s in spots)


def _merge_nearby(
    existing: list[dict[str, Any]],
    amap_items: list[dict[str, Any]],
    *,
    fill_empty_list: bool,
) -> tuple[list[dict[str, Any]], str]:
    """Merge Amap nearby into existing; never invent names."""
    if not amap_items:
        return existing, "amap_empty"

    def to_row(it: dict[str, Any]) -> dict[str, Any]:
        dist_label = it.get("approx_distance")
        # parse meters/km from human label is fragile; keep distance_km if we can from raw
        # amap_nearby items only have approx_distance string — store that in remark-like field
        # Prefer numeric if present
        dist_km = it.get("distance_km")
        drive = it.get("drive_minutes")
        return {
            "name": it.get("name"),
            "distance_km": dist_km,
            "drive_minutes": drive,
            "approx_distance": dist_label,
            "opening_hours": it.get("opening_hours"),
            "adult_price": it.get("adult_price"),
            "ticket_remark": it.get("ticket_remark"),
        }

    if not existing and fill_empty_list:
        out = []
        seen: set[str] = set()
        for it in amap_items:
            name = _blank(it.get("name"))
            if not name:
                continue
            key = normalize_name(name)
            if key in seen:
                continue
            seen.add(key)
            out.append(to_row(it))
            if len(out) >= 5:
                break
        return out, "fill_list"

    # fill distances onto existing names by fuzzy name match
    amap_by_norm = {normalize_name(str(it.get("name") or "")): it for it in amap_items}
    changed = False
    out = []
    for spot in existing:
        row = dict(spot)
        name = _blank(row.get("name"))
        if name and row.get("distance_km") is None:
            it = amap_by_norm.get(normalize_name(name))
            if it is None:
                # partial contains
                nn = normalize_name(name)
                for k, cand in amap_by_norm.items():
                    if nn and (nn in k or k in nn):
                        it = cand
                        break
            if it is not None:
                if it.get("distance_km") is not None:
                    row["distance_km"] = it["distance_km"]
                if it.get("approx_distance") and not row.get("approx_distance"):
                    row["approx_distance"] = it["approx_distance"]
                # derive rough minutes from approx if possible later
                changed = True
        out.append(row)
    return out, ("fill_distance" if changed else "no_match")


def fetch_around_raw(lng: float, lat: float, self_name: str, radius_m: int = 3000) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (normalized nearby items, raw pois).

    Phase B 不用行程工具那套「<200m 丢弃」过滤——景区内子点常在 200m 内，
    否则 empty nearby 永远补不上。仅排除与自身同名/包含名。
    """
    import re

    from app.tools import amap as amap_mod

    settings = get_settings()
    path = settings.amap_place_around_path or "/v3/place/around"
    data = amap_mod._get_json(
        path,
        {
            "location": f"{lng},{lat}",
            "keywords": "风景名胜",
            "types": "110000",
            "radius": max(500, min(radius_m, 5000)),
            "offset": 10,
            "page": 1,
            "extensions": "all",
        },
    )
    if str(data.get("status")) != "1":
        return [], []
    raw = [p for p in (data.get("pois") or []) if isinstance(p, dict)]
    self_n = re.sub(r"\s+", "", self_name or "")
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for poi in raw:
        name = _blank(poi.get("name"))
        if not name:
            continue
        key = re.sub(r"\s+", "", name)
        if self_n and (key == self_n or self_n in key or key in self_n):
            continue
        if key in seen:
            continue
        try:
            dist_m = float(poi["distance"]) if poi.get("distance") not in (None, "") else None
        except (TypeError, ValueError):
            dist_m = None
        # 排除几乎重合的同址点（<30m），其余保留
        if dist_m is not None and dist_m < 30:
            continue
        seen.add(key)
        dist_km = round(dist_m / 1000.0, 2) if dist_m is not None else None
        drive = max(1, int(round((dist_m / 1000.0) * 2))) if dist_m else None
        if dist_m is None:
            approx = None
        elif dist_m < 1000:
            approx = f"大约{int(dist_m)}米"
        else:
            approx = f"大约{dist_km}公里"
        items.append(
            {
                "name": name,
                "distance_km": dist_km,
                "drive_minutes": drive,
                "approx_distance": approx,
            }
        )
        if len(items) >= 5:
            break
    return items, raw


def process_row(
    row: dict[str, Any],
    *,
    sleep_s: float,
    kinds: set[str] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, str]]:
    """kinds: subset of {open,price,nearby,coords}. None/empty = all. Never invent price 0."""
    actions: dict[str, str] = {}
    payload = deepcopy(row.get("payload") or {})
    if not isinstance(payload, dict):
        payload = {}

    allow = kinds or {"open", "price", "nearby", "coords"}
    do_open = "open" in allow and _need_open(payload)
    do_price = "price" in allow and _need_price(row, payload)
    do_coords = "coords" in allow and _need_coords(row, payload)
    do_near = "nearby" in allow and _need_nearby_fill(payload)
    # 邻近缺坐标时，仅作依赖补坐标（仍不补票价）
    need_coords_for_near = do_near and _need_coords(row, payload)
    need_snap = do_open or do_price or do_coords or need_coords_for_near

    if not need_snap and not do_near:
        return None, {"status": "skip_no_gap"}

    snap = None
    if need_snap:
        snap = amap_poi_snapshot_by_name(
            name=str(row.get("name") or ""),
            city=str(row.get("city_norm") or row.get("city_raw") or ""),
        )
        time.sleep(sleep_s)
        if not snap.get("ok"):
            actions["snapshot"] = f"fail:{snap.get('error')}"
        else:
            actions["snapshot"] = "ok"

    # location
    loc = payload.get("location") if isinstance(payload.get("location"), dict) else {}
    loc = dict(loc)
    lat = row.get("lat")
    lng = row.get("lng")
    if lat is None:
        lat = loc.get("lat")
    if lng is None:
        lng = loc.get("lng")

    if snap and snap.get("ok"):
        if (do_coords or need_coords_for_near) and lat is None and snap.get("lat") is not None:
            lat = snap["lat"]
            actions["lat"] = "fill"
        else:
            actions["lat"] = "keep"
        if (do_coords or need_coords_for_near) and lng is None and snap.get("lng") is not None:
            lng = snap["lng"]
            actions["lng"] = "fill"
        else:
            actions["lng"] = "keep"

        if (do_coords or need_coords_for_near) and not _blank(loc.get("address")):
            if snap.get("address"):
                loc["address"] = snap["address"]
                actions["address"] = "fill"
            else:
                actions["address"] = "amap_empty"
        else:
            actions["address"] = "keep"

        if (do_coords or need_coords_for_near) and not _blank(loc.get("geo_code")) and not row.get("geo_code"):
            if snap.get("adcode"):
                loc["geo_code"] = snap["adcode"]
                actions["geo_code"] = "fill"
            else:
                actions["geo_code"] = "amap_empty"
        else:
            actions["geo_code"] = "keep"

        visit = payload.get("visit_info") if isinstance(payload.get("visit_info"), dict) else {}
        visit = dict(visit)
        ticket = visit.get("ticket") if isinstance(visit.get("ticket"), dict) else {}
        ticket = dict(ticket)

        if do_open:
            if not _blank(visit.get("opening_hours")):
                if snap.get("opening_hours"):
                    visit["opening_hours"] = snap["opening_hours"]
                    actions["opening_hours"] = "fill"
                else:
                    actions["opening_hours"] = "amap_empty"
            else:
                actions["opening_hours"] = "keep"
        else:
            actions["opening_hours"] = "skip"

        # price — only when kinds includes price; never invent 0 for unknown
        if do_price:
            if row.get("adult_price") is None and ticket.get("adult_price") is None and ticket.get("is_free") is None:
                if snap.get("adult_price") is not None or snap.get("is_free") is not None:
                    ticket["adult_price"] = snap.get("adult_price")
                    ticket["is_free"] = snap.get("is_free")
                    if snap.get("ticket_remark"):
                        ticket["remark"] = snap["ticket_remark"]
                    actions["price"] = "fill"
                elif snap.get("ticket_remark"):
                    if not _blank(ticket.get("remark")) or ticket.get("remark") == "以景区为准":
                        ticket["remark"] = snap["ticket_remark"]
                    actions["price"] = "remark_only"
                else:
                    actions["price"] = "amap_empty"
            else:
                actions["price"] = "keep"
        else:
            actions["price"] = "skip"

        visit["ticket"] = ticket
        payload["visit_info"] = visit

        content = payload.get("content") if isinstance(payload.get("content"), dict) else {}
        content = dict(content)
        if do_coords and not _blank(content.get("province")) and snap.get("pname"):
            content["province"] = snap["pname"]
            actions["province"] = "fill_amap"
            payload["content"] = content

    loc["lat"] = lat
    loc["lng"] = lng
    payload["location"] = loc

    # nearby
    if do_near and lat is not None and lng is not None:
        items, _raw = fetch_around_raw(float(lng), float(lat), str(row.get("name") or ""))
        time.sleep(sleep_s)
        rel = payload.get("relations") if isinstance(payload.get("relations"), dict) else {}
        rel = dict(rel)
        existing = _nearby_list(payload)
        merged, near_act = _merge_nearby(existing, items, fill_empty_list=not existing)
        actions["nearby"] = near_act
        rel["nearby_spots"] = merged
        rel.setdefault("recommended_routes", rel.get("recommended_routes") or [])
        payload["relations"] = rel
    elif do_near:
        actions["nearby"] = "skip_no_coords"
    else:
        actions["nearby"] = "skip"

    substantive = (
        actions.get("opening_hours") == "fill"
        or actions.get("price") in {"fill", "remark_only"}
        or actions.get("nearby") in {"fill_list", "fill_distance"}
        or actions.get("lat") == "fill"
        or actions.get("lng") == "fill"
        or actions.get("address") == "fill"
        or actions.get("geo_code") == "fill"
        or actions.get("province") == "fill_amap"
    )
    if not substantive:
        return None, actions

    # hot columns
    visit = payload.get("visit_info") if isinstance(payload.get("visit_info"), dict) else {}
    ticket = visit.get("ticket") if isinstance(visit.get("ticket"), dict) else {}
    adult_price = row.get("adult_price")
    is_free = None
    # read from ticket if we filled
    if ticket.get("adult_price") is not None:
        adult_price = Decimal(str(ticket["adult_price"]))
    if ticket.get("is_free") is not None:
        is_free = ticket["is_free"]

    media = payload.get("media") if isinstance(payload.get("media"), dict) else {}
    has_cover = bool(_blank(media.get("cover")))
    content = payload.get("content") if isinstance(payload.get("content"), dict) else {}
    has_intro = bool(_blank(content.get("intro")))

    if "rag_links" not in payload or not isinstance(payload.get("rag_links"), dict):
        payload["rag_links"] = {"guide_chunk_ids": [], "last_updated": datetime.now(timezone.utc).date().isoformat()}

    updated = {
        "id": row["id"],
        "aliases": list(row.get("aliases") or []),
        "is_free": is_free if is_free is not None else row.get("is_free"),
        "adult_price": adult_price,
        "has_cover": has_cover,
        "has_intro": has_intro,
        "lat": float(lat) if lat is not None else None,
        "lng": float(lng) if lng is not None else None,
        "geo_code": loc.get("geo_code") or row.get("geo_code"),
        "payload": payload,
        "updated_at": datetime.now(timezone.utc),
    }
    return updated, actions


def main() -> None:
    load_dotenv(ROOT / ".env")
    get_settings.cache_clear()
    parser = argparse.ArgumentParser(description="Phase B Amap factual fill")
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL", ""))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="Max rows this batch (0=all remaining gaps)")
    parser.add_argument("--offset", type=int, default=0, help="Skip first N gap candidates (for batching)")
    parser.add_argument("--sleep", type=float, default=0.12, help="Delay between Amap calls")
    parser.add_argument(
        "--only",
        default="all",
        help="Comma-separated kinds: open,nearby,coords,price or all (default all)",
    )
    args = parser.parse_args()
    if not args.database_url:
        raise SystemExit("DATABASE_URL missing")
    if not (get_settings().amap_api_key or "").strip():
        raise SystemExit("AMAP_API_KEY missing")

    only_raw = (args.only or "all").strip().lower()
    if only_raw in {"", "all"}:
        kinds: set[str] | None = None
    else:
        kinds = {x.strip() for x in only_raw.replace("+", ",").split(",") if x.strip()}
        allowed = {"open", "price", "nearby", "coords"}
        bad = kinds - allowed
        if bad:
            raise SystemExit(f"unknown --only kinds: {sorted(bad)}; allowed={sorted(allowed)}")
        if not kinds:
            raise SystemExit("--only empty")

    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(args.database_url, row_factory=dict_row) as conn:
        rows = conn.execute(
            """
            SELECT id, name, aliases, city_raw, city_norm, region, category, tags,
                   is_free, adult_price, has_cover, has_intro, lat, lng, geo_code, payload
            FROM attraction_kb
            ORDER BY name
            """
        ).fetchall()

        candidates: list[dict[str, Any]] = []
        for row in rows:
            payload = row.get("payload") or {}
            if not isinstance(payload, dict):
                payload = {}
            flags = {
                "open": _need_open(payload),
                "price": _need_price(row, payload),
                "nearby": _need_nearby_fill(payload),
                "coords": _need_coords(row, payload),
            }
            if kinds is None:
                if any(flags.values()):
                    candidates.append(row)
            elif any(flags[k] for k in kinds if k in flags):
                candidates.append(row)

        total_gaps = len(candidates)
        offset = max(0, int(args.offset or 0))
        if offset:
            candidates = candidates[offset:]
        if args.limit and args.limit > 0:
            candidates = candidates[: args.limit]

        kinds_label = "all" if kinds is None else ",".join(sorted(kinds))
        print(
            f"kb_rows={len(rows)} gaps_total={total_gaps} "
            f"batch_offset={offset} batch_size={len(candidates)} only={kinds_label}"
        )

        if not args.apply:
            # sample reasons among candidates (scoped to selected kinds for open/nearby)
            open_n = price_n = near_n = coord_n = 0
            for row in candidates:
                p = row.get("payload") or {}
                open_n += int(_need_open(p))
                price_n += int(_need_price(row, p))
                near_n += int(_need_nearby_fill(p))
                coord_n += int(_need_coords(row, p))
            print(f"gap_open={open_n} gap_price={price_n} gap_nearby={near_n} gap_coords={coord_n}")
            print("dry-run only; re-run with --apply to call Amap and write")
            return

        stats = {
            "processed": 0,
            "updated": 0,
            "noop": 0,
            "opening_fill": 0,
            "price_fill": 0,
            "price_skip": 0,
            "nearby_fill": 0,
            "nearby_empty": 0,
            "nearby_nomatch": 0,
            "coord_fill": 0,
            "snap_fail": 0,
            "fail": 0,
        }
        with conn.cursor() as cur:
            for row in candidates:
                stats["processed"] += 1
                try:
                    updated, actions = process_row(dict(row), sleep_s=args.sleep, kinds=kinds)
                except Exception as e:
                    stats["fail"] += 1
                    print(f"ERR {row.get('name')}: {e}")
                    continue
                if str(actions.get("snapshot") or "").startswith("fail:"):
                    stats["snap_fail"] += 1
                    if stats["snap_fail"] <= 3:
                        print(f"SNAP_FAIL {row.get('name')}: {actions.get('snapshot')}")
                if actions.get("nearby") == "amap_empty":
                    stats["nearby_empty"] += 1
                if actions.get("nearby") == "no_match":
                    stats["nearby_nomatch"] += 1
                if not updated:
                    stats["noop"] += 1
                    continue
                cur.execute(
                    UPDATE_SQL,
                    {**updated, "payload": Jsonb(updated["payload"])},
                )
                stats["updated"] += 1
                if actions.get("opening_hours") == "fill":
                    stats["opening_fill"] += 1
                if actions.get("price") == "fill":
                    stats["price_fill"] += 1
                if actions.get("price") == "skip":
                    stats["price_skip"] += 1
                if actions.get("nearby") in {"fill_list", "fill_distance"}:
                    stats["nearby_fill"] += 1
                if actions.get("lat") == "fill" or actions.get("lng") == "fill":
                    stats["coord_fill"] += 1
                if stats["processed"] % 25 == 0:
                    conn.commit()
                    print(f"... progress {stats['processed']}/{len(candidates)} {stats}")
        conn.commit()
        print("done", stats)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Offline: province-attractions.js name list vs attraction_kb coverage.

Does NOT write to DB. Source names come from data/province_attraction_names.json
(extracted from Lingxi province-attractions.js).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

from app.travel.kb_normalize import normalize_city, normalize_name

NAMES_DEFAULT = ROOT / "data" / "province_attraction_names.json"


def load_names(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise SystemExit(f"expected list in {path}")
    return data


def fetch_kb(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT id, name, aliases, city_norm, city_raw FROM attraction_kb")
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def build_index(rows: list[dict]) -> tuple[dict[str, list[dict]], dict[tuple[str, str], list[dict]]]:
    by_name: dict[str, list[dict]] = {}
    by_city: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        city_n = row.get("city_norm") or normalize_city(row.get("city_raw") or "")
        names = [row.get("name") or ""]
        names.extend(row.get("aliases") or [])
        for n in names:
            nn = normalize_name(str(n))
            if not nn:
                continue
            by_name.setdefault(nn, []).append(row)
            by_city.setdefault((nn, city_n), []).append(row)
    return by_name, by_city


def main() -> None:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Province attractions coverage vs KB")
    parser.add_argument("--names", type=Path, default=NAMES_DEFAULT)
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL", ""))
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "_province_coverage.txt")
    args = parser.parse_args()
    if not args.database_url:
        raise SystemExit("DATABASE_URL missing")
    if not args.names.exists():
        raise SystemExit(f"names file missing: {args.names}")

    import psycopg

    src = load_names(args.names)
    with psycopg.connect(args.database_url) as conn:
        rows = fetch_kb(conn)
    by_name, by_city = build_index(rows)

    strict = 0
    soft_only = 0
    miss: list[str] = []
    soft_hits: list[str] = []
    for item in src:
        name = str(item.get("name") or "").strip()
        city = str(item.get("city") or "").strip()
        region = str(item.get("region") or "").strip()
        nn = normalize_name(name)
        # city field may be "苏州昆山" / "桂林/阳朔" — try segments
        city_parts = [p.strip() for p in re_split_city(city) if p.strip()]
        matched_strict = False
        for part in city_parts or [""]:
            cn = normalize_city(part) if part else ""
            if cn and by_city.get((nn, cn)):
                matched_strict = True
                break
        if not matched_strict and by_name.get(nn):
            matched_strict = True
        label = f"{region}/{city}/{name}"
        if matched_strict:
            strict += 1
            continue
        soft = False
        for kn in by_name:
            if nn and kn and (nn in kn or kn in nn) and abs(len(nn) - len(kn)) <= 4:
                soft = True
                break
        if soft:
            soft_only += 1
            soft_hits.append(label)
        else:
            miss.append(label)

    total = len(src)
    covered = strict + soft_only
    rate_strict = (strict / total * 100) if total else 0.0
    rate_all = (covered / total * 100) if total else 0.0
    lines = [
        f"source_attractions={total}",
        f"kb_rows={len(rows)}",
        f"strict_covered={strict}",
        f"soft_only={soft_only}",
        f"hard_missing={len(miss)}",
        f"strict_coverage={rate_strict:.1f}%",
        f"loose_coverage={rate_all:.1f}%",
        "--- soft_only ---",
        *soft_hits,
        "--- hard_missing ---",
        *miss,
    ]
    args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        f"strict={rate_strict:.1f}% ({strict}/{total}) "
        f"loose={rate_all:.1f}% soft_only={soft_only} hard_missing={len(miss)}"
    )
    print(f"report={args.out}")


def re_split_city(city: str) -> list[str]:
    import re

    return re.split(r"[\/·、,，]", city or "")


if __name__ == "__main__":
    main()

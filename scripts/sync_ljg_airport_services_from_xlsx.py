"""从丽江机场 airport_services Excel 重写 scripts/airports_service.sql（仅 LJG）。

用法：
  python scripts/sync_ljg_airport_services_from_xlsx.py
  python scripts/sync_ljg_airport_services_from_xlsx.py --xlsx "D:/Projects/丽江机场airport_services(1).xlsx"
  python scripts/sync_ljg_airport_services_from_xlsx.py --apply   # 写 SQL 后灌入本地 Postgres

约定：
- 只改 airports_service.sql 与库中 airport_code=LJG 行，不动其他机场
- details 包装为 {"text": "..."}；空/— → {}
- rag_links 先写空 guide_chunk_ids；入库后请再跑：
    python scripts/ingest_airport_rag_fine.py --airports LJG --pg-only
  （若向量也要刷新，去掉 --pg-only）
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SQL_PATH = ROOT / "scripts" / "airports_service.sql"
DEFAULT_XLSX_CANDIDATES = [
    Path(r"D:/Projects/丽江机场airport_services(1).xlsx"),
    Path(r"D:/Projects/丽江机场airport_services.xlsx"),
]


def _find_xlsx(explicit: str | None) -> Path:
    if explicit:
        p = Path(explicit)
        if not p.is_file():
            raise SystemExit(f"xlsx not found: {p}")
        return p
    for p in DEFAULT_XLSX_CANDIDATES:
        if p.is_file():
            return p
    # newest *airport_services*.xlsx whose first data row is LJG
    try:
        import openpyxl
    except ImportError as e:
        raise SystemExit("need openpyxl") from e
    cands = sorted(
        Path(r"D:/Projects").glob("*airport_services*.xlsx"),
        key=lambda x: -x.stat().st_mtime,
    )
    for p in cands:
        wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
        ws = wb[wb.sheetnames[0]]
        rows = list(ws.iter_rows(min_row=2, max_row=2, values_only=True))
        wb.close()
        if rows and rows[0] and str(rows[0][0] or "").strip().upper() == "LJG":
            return p
    raise SystemExit("no LJG airport_services xlsx found under D:/Projects")


def _sql_escape(s: str) -> str:
    return s.replace("'", "''")


def _cell_text(v) -> str | None:
    if v is None:
        return None
    t = str(v).strip()
    if not t or t in {"—", "-", "无", "N/A", "n/a"}:
        return None
    return t


def load_excel_rows(xlsx: Path) -> list[dict]:
    import openpyxl

    wb = openpyxl.load_workbook(xlsx, read_only=True, data_only=True)
    if "airport_services" in wb.sheetnames:
        ws = wb["airport_services"]
    else:
        ws = wb[wb.sheetnames[0]]
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    if not rows:
        raise SystemExit("empty workbook")
    header = [str(c or "").strip().lower() for c in rows[0]]
    need = ["airport_code", "service_category", "service_name", "location", "details"]
    for col in need:
        if col not in header:
            raise SystemExit(f"missing column {col}; got {header}")
    idx = {c: header.index(c) for c in need}
    out: list[dict] = []
    for r in rows[1:]:
        if not r:
            continue
        code = str(r[idx["airport_code"]] or "").strip().upper()
        name = _cell_text(r[idx["service_name"]])
        cat = _cell_text(r[idx["service_category"]])
        if not code or not name or not cat:
            continue
        if code != "LJG":
            raise SystemExit(f"expected only LJG rows, got {code} / {name}")
        details_raw = _cell_text(r[idx["details"]])
        details = {"text": details_raw} if details_raw else {}
        out.append(
            {
                "airport_code": code,
                "service_category": cat,
                "service_name": name,
                "location": _cell_text(r[idx["location"]]),
                "details": details,
            }
        )
    if not out:
        raise SystemExit("no LJG data rows")
    return out


def render_sql(rows: list[dict], xlsx: Path) -> str:
    from collections import Counter

    counts = Counter(r["service_category"] for r in rows)
    lines = [
        "-- 丽江机场（LJG）airport_services 入库脚本",
        f"-- 来源：{xlsx.name}",
        '-- details：Excel 原文包装为 {"text": "..."}；空/— 则为 {}',
        "-- location：保留 TEXT；空/— 则为 NULL",
        "-- 分类：transport/navigation/checkin/facility/lounge/emergency",
        "-- rag_links.guide_chunk_ids 先空；fine ingest 后再按大类回填",
        "-- 本文件仅含 LJG；更新时只改本文件并 DELETE/INSERT LJG，勿动其他机场",
        "",
        "DELETE FROM airport_services WHERE airport_code = 'LJG';",
        "",
        "INSERT INTO airport_services (",
        "    airport_code, service_category, service_name, details, location, rag_links",
        ") VALUES",
    ]
    value_lines: list[str] = []
    for r in rows:
        details_json = json.dumps(r["details"], ensure_ascii=False)
        loc = "NULL" if not r["location"] else f"'{_sql_escape(r['location'])}'"
        value_lines.append(
            "    ('LJG', "
            f"'{_sql_escape(r['service_category'])}', "
            f"'{_sql_escape(r['service_name'])}', "
            f"'{_sql_escape(details_json)}'::jsonb, "
            f"{loc}, "
            "'{\"guide_chunk_ids\": []}'::jsonb)"
        )
    lines.append(",\n".join(value_lines) + ";")
    lines.append("")
    lines.append(f"-- 合计 {len(rows)} 条")
    for cat in ("transport", "navigation", "checkin", "facility", "lounge", "emergency", "policy"):
        if cat in counts:
            lines.append(f"--   {cat}: {counts[cat]}")
    lines.append("")
    lines.append("-- 筛选示例：")
    lines.append(
        "-- SELECT service_name, location, details FROM airport_services "
        "WHERE airport_code='LJG' AND service_category='transport';"
    )
    lines.append(
        "-- SELECT service_name, details->>'text' AS text FROM airport_services "
        "WHERE airport_code='LJG' AND service_name='母婴室';"
    )
    lines.append("")
    return "\n".join(lines)


def apply_sql(sql_path: Path) -> None:
    # Prefer docker compose postgres used by this project
    cmd = [
        "docker",
        "compose",
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "qa",
        "-d",
        "travel_qa",
        "-v",
        "ON_ERROR_STOP=1",
    ]
    data = sql_path.read_bytes()
    proc = subprocess.run(cmd, input=data, cwd=str(ROOT), capture_output=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr.decode("utf-8", errors="replace"))
        raise SystemExit(f"psql apply failed: {proc.returncode}")
    print(proc.stdout.decode("utf-8", errors="replace").strip() or "apply ok")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--xlsx", default=None, help="path to LJG airport_services xlsx")
    ap.add_argument("--apply", action="store_true", help="also DELETE/INSERT into local Postgres")
    ap.add_argument("--check-only", action="store_true", help="diff Excel vs current SQL, no write")
    args = ap.parse_args()

    xlsx = _find_xlsx(args.xlsx)
    rows = load_excel_rows(xlsx)
    new_sql = render_sql(rows, xlsx)

    if args.check_only:
        old = SQL_PATH.read_text(encoding="utf-8") if SQL_PATH.is_file() else ""
        # ignore trailing whitespace
        same = re.sub(r"\s+\Z", "\n", old) == re.sub(r"\s+\Z", "\n", new_sql)
        print(f"xlsx={xlsx}")
        print(f"rows={len(rows)} sql_in_sync={same}")
        return 0 if same else 1

    SQL_PATH.write_text(new_sql, encoding="utf-8", newline="\n")
    print(f"wrote {SQL_PATH} ({len(rows)} rows) from {xlsx.name}")

    if args.apply:
        apply_sql(SQL_PATH)
        print("DB LJG replaced. Next: python scripts/ingest_airport_rag_fine.py --airports LJG")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

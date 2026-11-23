#!/usr/bin/env python3
"""从 _dest_match_report.json 导出未匹配景点清单 → docs/未入库地点.md"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "data" / "knowledge" / "_dest_match_report.json"
OUT = ROOT / "docs" / "未入库地点.md"


def main() -> int:
    if not REPORT.exists():
        print(f"missing {REPORT}", file=sys.stderr)
        return 1
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    rows = list(report.get("missed_rows") or [])
    rows.sort(
        key=lambda x: (
            str(x.get("region_header") or x.get("city") or ""),
            str(x.get("name") or ""),
        )
    )

    by_region: dict[str, list] = defaultdict(list)
    for r in rows:
        key = str(r.get("region_header") or r.get("city") or r.get("province") or "未知").strip() or "未知"
        by_region[key].append(r)

    lines: list[str] = [
        "# 未入库地点",
        "",
        "> 来源：目的地景点语料匹配报告 `_dest_match_report.json`。",
        "> 语料中有游记段，但未能匹配到当前 `attraction_kb`。",
        "> **请先补齐 Postgres 名片后，再灌入 `travel_rag`（勿混入 `aviation_rag`）。**",
        "",
        f"- 语料景点总数：{report.get('total_spots')}",
        f"- 已匹配：{report.get('matched')}",
        f"- **未入库：{report.get('missed')}**（本清单 {len(rows)} 条）",
        "",
        "## 清单",
        "",
    ]

    for region in sorted(by_region.keys()):
        items = by_region[region]
        lines.append(f"### {region}")
        lines.append("")
        lines.append("| 景点名 | City | 省级 | 区县 | 语料章节 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for r in items:
            name = str(r.get("name") or "").replace("|", "\\|")
            city = str(r.get("city") or "—") or "—"
            prov = str(r.get("province") or "—") or "—"
            dist = str(r.get("district") or "—") or "—"
            secs = r.get("sections") or []
            if isinstance(secs, list):
                sec = "、".join(str(s) for s in secs) if secs else "—"
            else:
                sec = str(secs or "—")
            lines.append(f"| {name} | {city} | {prov} | {dist} | {sec} |")
        lines.append("")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {OUT} rows={len(rows)} regions={len(by_region)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

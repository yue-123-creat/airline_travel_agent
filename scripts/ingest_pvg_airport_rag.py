"""上海浦东国际机场（PVG）指引：按大类抽原文，供细切 ingest 使用。

来源：
  Projects/上海浦东国际机场旅客服务信息汇编.docx  → _pvg_01_raw.txt
  Projects/上海浦东国际机场旅客服务信息汇编2.docx → _pvg_02_raw.txt

分类：transport/navigation/checkin/facility/lounge/emergency
以汇编2结构化正文为主，汇编1互补（不含航线年表等噪声大段）。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "knowledge"
RAW_FILES: dict[str, Path] = {
    "pvg01": RAW_DIR / "_pvg_01_raw.txt",
    "pvg02": RAW_DIR / "_pvg_02_raw.txt",
}

AIRPORT_CODE = "PVG"
CITY = "上海"
SPOT = "上海浦东国际机场"

CAT_CHUNK: dict[str, str] = {
    "transport": "chunk_pvg_01_transport",
    "navigation": "chunk_pvg_02_navigation",
    "checkin": "chunk_pvg_03_checkin",
    "facility": "chunk_pvg_04_facility",
    "lounge": "chunk_pvg_05_lounge",
    "emergency": "chunk_pvg_06_emergency",
}

# (category, [(doc_key, start, end_exclusive|None), ...])
RANGES: list[tuple[str, list[tuple[str, str, str | None]]]] = [
    (
        "navigation",
        [
            ("pvg02", "一、机场概况", "二、乘机指南"),
            ("pvg02", "二、乘机指南", "三、旅客服务"),
            ("pvg02", "（一）机场问询", "（二）失物认领"),
            ("pvg01", "三、乘机指引", "四、值机与安检"),
        ],
    ),
    (
        "transport",
        [
            ("pvg02", "六、机场交通", "七、服务承诺"),
            ("pvg01", "二、交通接驳", "三、乘机指引"),
        ],
    ),
    (
        "checkin",
        [
            ("pvg02", "（四）值机服务", "（六）特殊旅客"),
            ("pvg02", "（五）安检服务", "（六）特殊旅客"),
            ("pvg02", "（七）国际通关", "四、贵宾、货运与商业服务"),
            ("pvg02", "（八）行李服务", "四、贵宾、货运与商业服务"),
            ("pvg01", "四、值机与安检", "五、便民设施"),
        ],
    ),
    (
        "facility",
        [
            ("pvg02", "五、机场设施与延伸服务", "六、机场交通"),
            ("pvg01", "五、便民设施", "六、贵宾与商业"),
        ],
    ),
    (
        "lounge",
        [
            ("pvg02", "四、贵宾、货运与商业服务", "五、机场设施与延伸服务"),
            ("pvg02", "八、玩转机场", "九、机场酒店"),
            ("pvg02", "九、机场酒店", "十、信息来源与核验网址"),
            ("pvg01", "六、贵宾与商业", "七、异常与保障"),
        ],
    ),
    (
        "emergency",
        [
            ("pvg02", "（二）失物认领", "（三）首乘服务"),
            ("pvg02", "（三）首乘服务", "（四）值机服务"),
            ("pvg02", "（六）特殊旅客", "（七）国际通关"),
            ("pvg02", "七、服务承诺", "八、玩转机场"),
            ("pvg01", "七、异常与保障", "八、信息来源与核验网址"),
        ],
    ),
]


def _find(body: str, needle: str, *, whole_line: bool = False) -> int:
    if not needle:
        return -1
    target = needle.strip()
    pos = 0
    for line in body.splitlines(keepends=True):
        if line.endswith("\r\n"):
            raw = line[:-2]
        elif line.endswith("\n") or line.endswith("\r"):
            raw = line[:-1]
        else:
            raw = line
        if whole_line:
            if raw.strip() == target:
                return pos
        else:
            if target in raw:
                return pos + raw.find(target)
            soft = re.sub(r"[（）()：:\s]", "", target)
            if soft and soft in re.sub(r"[（）()：:\s]", "", raw):
                return pos
        pos += len(line)
    return -1


def _extract(body: str, start: str, end: str | None) -> str:
    i = _find(body, start, whole_line=True)
    if i < 0:
        i = _find(body, start, whole_line=False)
    if i < 0:
        return ""
    if end:
        rest = body[i + len(start) :]
        j = _find(rest, end, whole_line=True)
        if j < 0:
            j = _find(rest, end, whole_line=False)
        j = i + len(start) + j if j >= 0 else len(body)
    else:
        j = len(body)
    return body[i:j].strip()


def build_category_texts(docs: dict[str, str] | None = None) -> dict[str, str]:
    if docs is None:
        docs = {k: p.read_text(encoding="utf-8") for k, p in RAW_FILES.items()}
    out: dict[str, str] = {}
    for cat, ranges in RANGES:
        parts: list[str] = []
        for doc_key, start, end in ranges:
            piece = _extract(docs.get(doc_key) or "", start, end)
            if piece:
                parts.append(piece)
        seen: set[str] = set()
        lines: list[str] = []
        for part in parts:
            for ln in part.splitlines():
                k = ln.strip()
                # skip huge airline/source tables noise
                if not k or k in seen:
                    continue
                if k.startswith("[[TABLE") and int(re.sub(r"\D", "", k) or "0") >= 3:
                    # keep small tables from pvg02; skip late mega tables in pvg01
                    pass
                seen.add(k)
                lines.append(ln)
        out[cat] = "\n".join(lines).strip()
    return out

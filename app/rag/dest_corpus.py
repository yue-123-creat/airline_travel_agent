"""Parse destination attraction decoupling corpus (每景点自包含)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

SPOT_HEAD_RE = re.compile(
    r"^(\d+)\.\s*(.+?)\s+City[:：]\s*(.+?)｜省级[:：]\s*(.+?)｜区县[:：]\s*(\S+)"
)
REGION_RE = re.compile(r"^◆\s*(.+)$")
SECTION_LABELS = ("交通", "住宿", "美食", "小贴士")


@dataclass
class DestSpot:
    doc_index: int
    name: str
    city: str
    province: str
    district: str
    region_header: str
    address_line: str = ""
    sections: dict[str, str] = field(default_factory=dict)
    raw_block: str = ""


def parse_dest_attractions_text(text: str) -> list[DestSpot]:
    lines = [ln.rstrip() for ln in (text or "").splitlines()]
    spots: list[DestSpot] = []
    cur_region = ""
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        rm = REGION_RE.match(line)
        if rm:
            cur_region = rm.group(1).strip()
            i += 1
            continue
        sm = SPOT_HEAD_RE.match(line)
        if not sm:
            i += 1
            continue
        doc_index = int(sm.group(1))
        name = sm.group(2).strip()
        city = sm.group(3).strip()
        province = sm.group(4).strip()
        district = sm.group(5).strip()
        address_line = ""
        if "地址" in line:
            address_line = line.split("地址", 1)[-1].lstrip(" /／:：").strip()

        block_lines = [line]
        i += 1
        sections: dict[str, str] = {k: "" for k in SECTION_LABELS}
        current_label: str | None = None
        buf: list[str] = []

        def _flush() -> None:
            nonlocal current_label, buf
            if current_label and buf:
                sections[current_label] = "\n".join(buf).strip()
            buf = []

        while i < n:
            nxt = lines[i]
            s = nxt.strip()
            if REGION_RE.match(s) or SPOT_HEAD_RE.match(s):
                break
            block_lines.append(nxt)
            # Section start: 「交通：...」 on same line or alone
            labeled = False
            for lab in SECTION_LABELS:
                if s.startswith(lab + "：") or s.startswith(lab + ":"):
                    _flush()
                    current_label = lab
                    rest = s.split("：", 1)[-1] if "：" in s else s.split(":", 1)[-1]
                    buf = [rest.strip()] if rest.strip() else []
                    labeled = True
                    break
                if s == lab:
                    _flush()
                    current_label = lab
                    buf = []
                    labeled = True
                    break
            if not labeled and current_label:
                buf.append(s)
            i += 1
        _flush()

        spots.append(
            DestSpot(
                doc_index=doc_index,
                name=name,
                city=city,
                province=province,
                district=district,
                region_header=cur_region,
                address_line=address_line,
                sections={k: v for k, v in sections.items() if v.strip()},
                raw_block="\n".join(block_lines).strip(),
            )
        )
    return spots


def load_dest_attractions(path: Path) -> list[DestSpot]:
    return parse_dest_attractions_text(path.read_text(encoding="utf-8"))

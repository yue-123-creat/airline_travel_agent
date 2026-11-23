"""Dispatch 硬门控：本轮原话 / 新槽位 vs 工具参数（城市·景点·机场）一致性。"""

from __future__ import annotations

import re
from typing import Any

from app.travel.photo_policy import _spots_fuzzy_match

_TOPIC_PLACE_KEYS = (
    "spot",
    "attraction",
    "poi",
    "place",
    "destination",
    "city",
    "city_norm",
    "province",
)
_AIRPORT_KEYS = ("airport_code", "airport_name", "airport", "iata", "code")
_IATA_RE = re.compile(r"\b([A-Za-z]{3})\b")
# parse_travel_intent 残留词，不能当换题锚点
_PLACE_STOP = frozenset(
    {
        "多少",
        "怎么样",
        "怎样",
        "什么",
        "哪里",
        "怎么",
        "如何",
        "介绍",
        "推荐",
        "图片",
        "照片",
        "门票",
        "开放",
        "时间",
        "好玩",
        "值得",
        "附近",
        "美食",
    }
)


def _norm_place(v: Any) -> str:
    return str(v or "").strip()


def places_from_text(text: str) -> list[str]:
    """从本轮原话抽出城市/景点/省（有则视为换题锚点）。"""
    from app.travel.query_intent import parse_travel_intent

    intent = parse_travel_intent(text or "")
    out: list[str] = []
    seen: set[str] = set()

    def add(p: Any, *, allow_weak_name: bool = False) -> None:
        s = _norm_place(p)
        if not s or s in seen:
            return
        if not allow_weak_name and s in _PLACE_STOP:
            return
        seen.add(s)
        out.append(s)

    add(intent.city_norm, allow_weak_name=True)
    add(intent.province_norm, allow_weak_name=True)
    for p in intent.province_norms or []:
        add(p, allow_weak_name=True)
    # 景点名：过滤口语残留
    add(intent.name_query)
    add(intent.name_norm)
    return out


def places_from_slots(slots: dict[str, Any] | None) -> list[str]:
    if not isinstance(slots, dict):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for k in _TOPIC_PLACE_KEYS:
        v = slots.get(k)
        if v is None:
            continue
        parts = (
            [_norm_place(x) for x in v]
            if isinstance(v, (list, tuple))
            else [_norm_place(v)]
        )
        for p in parts:
            if p and p not in seen:
                seen.add(p)
                out.append(p)
    return out


def _place_in_text(place: str, text: str) -> bool:
    p = _norm_place(place)
    t = (text or "").strip()
    if not p or not t:
        return False
    if p in t:
        return True
    # 短码/别名模糊：双方互含且长度≥2
    return any(_spots_fuzzy_match(p, tok) for tok in re.findall(r"[\u4e00-\u9fffA-Za-z]{2,16}", t))


def _overlaps_any(place: str, anchors: list[str]) -> bool:
    p = _norm_place(place)
    if not p:
        return False
    for a in anchors:
        if _spots_fuzzy_match(p, a):
            return True
    return False


def airport_bits_from_slots(slots: dict[str, Any] | None) -> list[str]:
    if not isinstance(slots, dict):
        return []
    out: list[str] = []
    for k in _AIRPORT_KEYS:
        s = _norm_place(slots.get(k))
        if s:
            out.append(s.upper() if len(s) == 3 and s.isalpha() else s)
    return out


def airport_bits_from_text(text: str) -> list[str]:
    t = text or ""
    bits: list[str] = []
    for m in _IATA_RE.finditer(t):
        bits.append(m.group(1).upper())
    if "机场" in t:
        for m in re.finditer(r"([\u4e00-\u9fff]{2,12}机场)", t):
            bits.append(m.group(1))
    return bits


def dispatch_params_mismatch(
    *,
    raw_message: str,
    normalized_query: str,
    slots: dict[str, Any] | None,
) -> bool:
    """
    True = 工具/槽位参数与本轮原话锚点冲突，应拦截重试。
    短追问（原话未点名地点）不拦截，允许沿用槽位。
    判定重点：normalized_query 是否夹带原话没有的旧城/景点，或机场槽位与本轮冲突。
    """
    raw = (raw_message or "").strip()
    norm = (normalized_query or "").strip()
    turn_places = places_from_text(raw)
    turn_airports = airport_bits_from_text(raw)

    # 原话已点名地点：normalized 不得额外塞入无关旧主题
    if turn_places and norm:
        for p in places_from_text(norm):
            if _place_in_text(p, raw) or _overlaps_any(p, turn_places):
                continue
            return True
        # slots 旧城/景若被写进 normalized，同样拦截
        for p in places_from_slots(slots):
            if not p or p not in norm:
                continue
            if _place_in_text(p, raw) or _overlaps_any(p, turn_places):
                continue
            return True

    # 原话已点名机场：slots 机场不得与本轮冲突
    if turn_airports:
        for a in airport_bits_from_slots(slots):
            a_up = a.upper() if len(a) == 3 and a.isalpha() else a
            if any(a_up == t or a_up in t or t in a_up for t in turn_airports):
                continue
            if _place_in_text(a, raw):
                continue
            return True

    return False


def sanitize_slots_for_turn(
    raw_message: str,
    slots: dict[str, Any] | None,
) -> dict[str, Any]:
    """丢弃与本轮原话不符的主题槽位，保留 photos_shown 等非主题键。"""
    raw = (raw_message or "").strip()
    base = dict(slots or {})
    turn_places = places_from_text(raw)
    turn_airports = airport_bits_from_text(raw)

    if turn_places:
        for k in _TOPIC_PLACE_KEYS:
            if k not in base:
                continue
            v = base.get(k)
            parts = (
                [_norm_place(x) for x in v]
                if isinstance(v, (list, tuple))
                else [_norm_place(v)]
            )
            keep = [
                p
                for p in parts
                if p and (_place_in_text(p, raw) or _overlaps_any(p, turn_places))
            ]
            if not keep:
                base.pop(k, None)
            elif isinstance(v, (list, tuple)):
                base[k] = keep
            else:
                base[k] = keep[0]

    if turn_airports:
        for k in _AIRPORT_KEYS:
            if k not in base:
                continue
            a = _norm_place(base.get(k))
            if not a:
                base.pop(k, None)
                continue
            a_up = a.upper() if len(a) == 3 and a.isalpha() else a
            ok = any(a_up == t or a_up in t or t in a_up for t in turn_airports) or _place_in_text(
                a, raw
            )
            if not ok:
                base.pop(k, None)

    return base


def apply_slots_patch(
    slots: dict[str, Any],
    slots_patch: dict[str, Any] | None,
) -> dict[str, Any]:
    """合并 patch；null / 空串表示清空该键（换题清槽）。"""
    out = dict(slots or {})
    if not isinstance(slots_patch, dict):
        return out
    for k, v in slots_patch.items():
        if v is None:
            out.pop(k, None)
            continue
        if isinstance(v, str) and not v.strip():
            out.pop(k, None)
            continue
        if isinstance(v, (list, tuple)) and not any(str(x).strip() for x in v):
            out.pop(k, None)
            continue
        out[k] = v
    return out

"""Mock get_city_info tool: hardcoded province season / foods (no DB, no JS IO)."""

from __future__ import annotations

import re
from typing import Any

from app.travel.city_info_data import CITY_INFO, CITY_TO_REGION
from app.travel.kb_normalize import PROVINCE_SHORT_TO_NAME

GET_CITY_INFO_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "get_city_info",
        "description": (
            "查询省市旅行常识卡片：最佳季节说明与代表性美食。"
            "入参可为省名、别名或常见城市名；返回省级卡片。"
            "不含景点列表；景点请用知识库/高德工具。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "place": {
                    "type": "string",
                    "description": "省/市/别名，如「杭州」「浙江」「蜀」",
                },
            },
            "required": ["place"],
        },
    },
}

_STRIP_SUFFIX = re.compile(
    r"(壮族自治区|回族自治区|维吾尔自治区|自治区|特别行政区|省|市|地区|盟)$"
)


def _norm(text: str) -> str:
    s = (text or "").strip()
    s = _STRIP_SUFFIX.sub("", s)
    return s.strip()


def _by_region() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for item in CITY_INFO:
        region = str(item.get("region") or "")
        if region:
            out[region] = item
    return out


def resolve_region(place: str) -> str | None:
    """Map place string → canonical region name, or None."""
    raw = (place or "").strip()
    if not raw:
        return None
    by_region = _by_region()

    # exact region / alias
    for item in CITY_INFO:
        region = str(item.get("region") or "")
        aliases = [str(a) for a in (item.get("aliases") or [])]
        if raw == region or raw in aliases:
            return region
        if _norm(raw) == _norm(region) or _norm(raw) in {_norm(a) for a in aliases}:
            return region

    # 单字简称（车牌惯例表，非高德字段）
    if raw in PROVINCE_SHORT_TO_NAME:
        return PROVINCE_SHORT_TO_NAME[raw]

    # city / fragment map (longest key first already in CITY_TO_REGION order preferred)
    if raw in CITY_TO_REGION:
        return CITY_TO_REGION[raw]
    n = _norm(raw)
    if n in CITY_TO_REGION:
        return CITY_TO_REGION[n]

    # substring: place contains city key or vice versa
    candidates: list[tuple[int, str]] = []
    for key, region in CITY_TO_REGION.items():
        if key and (key in raw or raw in key or (n and (n in key or key in n))):
            candidates.append((len(key), region))
    if candidates:
        candidates.sort(reverse=True)
        return candidates[0][1]

    # last resort: region name contained in query
    for region in by_region:
        if region in raw or raw in region:
            return region
    return None


def get_city_info(place: str) -> dict[str, Any]:
    region = resolve_region(place)
    if not region:
        return {"ok": False, "error": "unknown_place", "place": place}
    item = _by_region().get(region)
    if not item:
        return {"ok": False, "error": "unknown_place", "place": place}
    return {
        "ok": True,
        "place": place,
        "region": item.get("region"),
        "aliases": list(item.get("aliases") or []),
        "type": item.get("type") or "",
        "season": item.get("season") or "",
        "foods": list(item.get("foods") or []),
    }


def dispatch_city_info_tool(name: str, args: dict[str, Any]) -> dict[str, Any] | None:
    """Return result if tool is get_city_info, else None (caller continues)."""
    if name != "get_city_info":
        return None
    return get_city_info(str(args.get("place") or args.get("city") or ""))

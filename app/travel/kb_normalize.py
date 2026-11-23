"""Shared city/name normalization for attraction_kb import and SQL search."""

from __future__ import annotations

import re

# Longest-first so「呼伦贝尔」wins over shorter prefixes.
CITY_BASES: tuple[str, ...] = tuple(
    sorted(
        {
            "呼伦贝尔",
            "乌鲁木齐",
            "鄂尔多斯",
            "哈尔滨",
            "石家庄",
            "张家界",
            "景德镇",
            "秦皇岛",
            "上海",
            "成都",
            "北京",
            "杭州",
            "重庆",
            "西安",
            "广州",
            "深圳",
            "南京",
            "苏州",
            "武汉",
            "长沙",
            "昆明",
            "丽江",
            "大理",
            "桂林",
            "厦门",
            "青岛",
            "天津",
            "拉萨",
            "沈阳",
            "大连",
            "三亚",
            "海口",
            "长春",
            "银川",
            "无锡",
            "上饶",
            "洛阳",
            "宜昌",
            "乐山",
            "晋中",
            "喀什",
            "保定",
            "承德",
            "嘉兴",
            "恩施",
            "黄山",
            "烟台",
            "阿坝",
            "甘孜",
            "六安",
            "临汾",
            "忻州",
        },
        key=len,
        reverse=True,
    )
)

# Province / 直辖市 / 自治区 — same peer level as city for place filters.
# 全称列表与高德 v3/config/district 省级 name 对齐（实测返回「青海省」等全称）。
PROVINCE_BASES: tuple[str, ...] = tuple(
    sorted(
        {
            "内蒙古",
            "黑龙江",
            "新疆",
            "西藏",
            "广西",
            "宁夏",
            "香港",
            "澳门",
            "台湾",
            "北京",
            "天津",
            "上海",
            "重庆",
            "河北",
            "山西",
            "辽宁",
            "吉林",
            "江苏",
            "浙江",
            "安徽",
            "福建",
            "江西",
            "山东",
            "河南",
            "湖北",
            "湖南",
            "广东",
            "海南",
            "四川",
            "贵州",
            "云南",
            "陕西",
            "甘肃",
            "青海",
        },
        key=len,
        reverse=True,
    )
)

_NAME_PUNCT = re.compile(r"[\s·•－—\-_/｜|]+")
_PROVINCE_SUFFIX = re.compile(r"(壮族自治区|回族自治区|维吾尔自治区|自治区|特别行政区|省|市)$")

# 省级单字简称 → 规范省名。
# 来源：民用号牌省份简称惯例（公开惯例）；不是高德 API 字段。
# 高德 district 仅返回全称（如「青海省」），已实测无简称字段。
PROVINCE_SHORT_TO_NAME: dict[str, str] = {
    "京": "北京",
    "津": "天津",
    "沪": "上海",
    "渝": "重庆",
    "冀": "河北",
    "晋": "山西",
    "蒙": "内蒙古",
    "辽": "辽宁",
    "吉": "吉林",
    "黑": "黑龙江",
    "苏": "江苏",
    "浙": "浙江",
    "皖": "安徽",
    "闽": "福建",
    "赣": "江西",
    "鲁": "山东",
    "豫": "河南",
    "鄂": "湖北",
    "湘": "湖南",
    "粤": "广东",
    "桂": "广西",
    "琼": "海南",
    "川": "四川",
    "蜀": "四川",
    "黔": "贵州",
    "贵": "贵州",
    "滇": "云南",
    "云": "云南",
    "藏": "西藏",
    "陕": "陕西",
    "秦": "陕西",
    "甘": "甘肃",
    "陇": "甘肃",
    "青": "青海",
    "宁": "宁夏",
    "新": "新疆",
    "港": "香港",
    "澳": "澳门",
    "台": "台湾",
}


def expand_province_short_run(text: str) -> list[str]:
    """把「青甘」「陕甘青」「川藏」等连续简称拆成省名列表（去重保序）。

    只识别长度≥2 的连续简称串，避免「新鲜」「青菜」等单字误伤。
    """
    s = (text or "").strip()
    if not s:
        return []
    out: list[str] = []
    seen: set[str] = set()
    i = 0
    n = len(s)
    while i < n:
        if s[i] not in PROVINCE_SHORT_TO_NAME:
            i += 1
            continue
        j = i
        names: list[str] = []
        while j < n and s[j] in PROVINCE_SHORT_TO_NAME:
            names.append(PROVINCE_SHORT_TO_NAME[s[j]])
            j += 1
        if len(names) >= 2:
            for name in names:
                if name not in seen:
                    seen.add(name)
                    out.append(name)
            i = j
        else:
            i += 1
    return out


def normalize_city(city: str) -> str:
    """上海浦东 / 成都都江堰 → 上海 / 成都；未知城市原样返回。"""
    city = (city or "").strip()
    if not city:
        return ""
    for base in CITY_BASES:
        if city == base or city.startswith(base) or base in city:
            return base
    return city


def normalize_province(place: str) -> str:
    """青海省 / 内蒙古自治区 → 青海 / 内蒙古；未知原样（去后缀后）返回。"""
    place = (place or "").strip()
    if not place:
        return ""
    if place in PROVINCE_SHORT_TO_NAME:
        return PROVINCE_SHORT_TO_NAME[place]
    for base in PROVINCE_BASES:
        if place == base or place.startswith(base) or base in place:
            return base
    return _PROVINCE_SUFFIX.sub("", place).strip() or place


def normalize_name(name: str) -> str:
    """Strip whitespace/punctuation for name_norm matching."""
    return _NAME_PUNCT.sub("", (name or "").strip())

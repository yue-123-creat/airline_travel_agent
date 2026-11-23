"""Parse city / province / tags / price intent from a travel query."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from app.travel.kb_normalize import (
    CITY_BASES,
    PROVINCE_BASES,
    PROVINCE_SHORT_TO_NAME,
    expand_province_short_run,
    normalize_city,
    normalize_name,
    normalize_province,
)

PriceSort = Literal["asc", "desc", "none"]
TripMode = Literal["same_city_day", "multi_day"]

TAG_VOCAB: tuple[str, ...] = tuple(
    sorted(
        {
            "拍照",
            "自然风光",
            "避暑",
            "历史人文",
            "登山徒步",
            "城市地标",
            "亲子游",
            "夜景",
            "宗教文化",
            "博物馆",
            "适合老人",
            "古镇",
            "动物观赏",
            "主题乐园",
        },
        key=len,
        reverse=True,
    )
)

_PRICE_LT = re.compile(
    r"(?:票价|门票|价格|价位)?\s*(?:低于|少于|不到|小于|<|＜)\s*(\d+(?:\.\d+)?)\s*(?:元|块)?"
)
_PRICE_LT2 = re.compile(r"(\d+(?:\.\d+)?)\s*(?:元|块)\s*(?:以下|以内)")
_CHEAP = re.compile(r"便宜|省钱|低价|性价比|实惠|穷游")
_EXPENSIVE = re.compile(r"高端|奢华|贵一点|贵一些|不差钱|豪华")

_QUERY_NOISE = re.compile(
    r"我想去|想去|帮我|请问|一下|有哪些|有什么|哪里好玩|怎么玩|好玩吗|好玩的|值得去吗|"
    r"在哪里|在哪|哪里|位置|地址|怎么去|如何到达|交通|开放时间|几点|"
    r"附近有什么美食|有什么美食|附近吃|好吃的|美食|餐饮|"
    r"介绍一下|介绍|是什么|景点|景区|旅游|旅行|攻略|推荐|路线|安排|规划|行程|怎么安排|怎么走|"
    r"便宜|省钱|门票|票价|低于|少于|不到|以下|以内|元|块|亲子|带娃|"
    r"给我一个|给我|一个|请|做个|帮我做|出游|游玩|玩法|"
    r"看一下|看看|发几张|来几张|照片|图片|实景|风景照|"
    r"\d+\s*日游|[一二三四五六七八九十两多几]\s*日游|多日游|几日游|"
    r"自由行|跟团|深度游|环线"
)

# 粘在景点名后的口语后缀（噪声未剥净时再剥一层）
_NAME_TAIL = re.compile(
    r"(?:在哪里啊?|在哪啊?|哪里啊?|怎么去|如何到达|位置|地址|交通|"
    r"附近有?什么美食|有什么美食|附近吃什么|美食|好吃|"
    r"门票|开放时间|介绍一下?|是什么|的?(?:图片|照片)|图片|照片|的|"
    r"啊|呢|呀|吗|吧)+$"
)

_DAY_NUM = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}
_DAYS_RE = re.compile(r"(\d+)\s*日游|(?:([一二三四五六七八九十两]))\s*日游")
_MULTI_HINT = re.compile(
    r"多日游|几日游|环线|跨城|多地|多城|自由行|"
    r"(?:[2-9]|1\d|[二三四五六七八九十两])\s*日游|"
    r"三日|五日|七日|两日|三天|五天|七天|两天"
)
_SHORT_CHARS = "".join(PROVINCE_SHORT_TO_NAME.keys())
_SHORT_RUN_RE = re.compile(f"[{re.escape(_SHORT_CHARS)}]{{2,}}")


@dataclass
class TravelIntent:
    city_norm: str = ""
    province_norm: str = ""
    province_norms: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    max_price: float | None = None
    price_sort: PriceSort = "none"
    name_query: str = ""
    name_norm: str = ""
    trip_mode: TripMode = "same_city_day"
    trip_days: int = 1


def parse_trip_days(query: str) -> int | None:
    q = query or ""
    m = _DAYS_RE.search(q)
    if not m:
        return None
    if m.group(1):
        try:
            n = int(m.group(1))
            return max(1, min(n, 10))
        except ValueError:
            return None
    return _DAY_NUM.get(m.group(2) or "", None)


def infer_trip_mode(
    query: str,
    *,
    city_norm: str,
    province_norms: list[str],
) -> tuple[TripMode, int]:
    q = query or ""
    days = parse_trip_days(q)
    if days == 1:
        return "same_city_day", 1
    if days is not None and days >= 2:
        return "multi_day", days
    explicit_multi = bool(_MULTI_HINT.search(q))
    multi_province = len(province_norms) >= 2
    province_tour = bool(province_norms) and not city_norm and bool(
        re.search(r"安排|规划|行程|路线|怎么玩|攻略|游", q)
    )
    if multi_province or explicit_multi or province_tour:
        return "multi_day", days or (5 if multi_province else 3)
    return "same_city_day", 1


def _collect_provinces(query: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()

    def add(name: str) -> None:
        n = normalize_province(name)
        if n and n not in seen:
            seen.add(n)
            found.append(n)

    for base in PROVINCE_BASES:
        if base in query:
            add(base)
    for short, full in PROVINCE_SHORT_TO_NAME.items():
        if f"{short}省" in query:
            add(full)
    for name in expand_province_short_run(query):
        add(name)
    return found


def parse_travel_intent(query: str) -> TravelIntent:
    q = (query or "").strip()

    city = ""
    for base in CITY_BASES:
        if base in q:
            city = normalize_city(base)
            break

    provinces = _collect_provinces(q)
    province = provinces[0] if provinces else ""

    if city and not province and city in PROVINCE_BASES:
        province = city
        provinces = [city]
    if province and not city and len(provinces) <= 1 and province in CITY_BASES:
        city = province

    tags: list[str] = []
    for tag in TAG_VOCAB:
        if tag in q and tag not in tags:
            tags.append(tag)
    if ("亲子" in q or "带娃" in q) and "亲子游" not in tags:
        tags.append("亲子游")

    max_price: float | None = None
    m = _PRICE_LT.search(q) or _PRICE_LT2.search(q)
    if m:
        try:
            max_price = float(m.group(1))
        except ValueError:
            max_price = None

    price_sort: PriceSort = "none"
    if _CHEAP.search(q) or max_price is not None:
        price_sort = "asc"
    elif _EXPENSIVE.search(q):
        price_sort = "desc"

    residual = q
    if city:
        residual = residual.replace(city, " ")
    for p in provinces:
        residual = residual.replace(p, " ")
    residual = _SHORT_RUN_RE.sub(" ", residual)
    for tag in tags:
        residual = residual.replace(tag, " ")
    residual = _QUERY_NOISE.sub(" ", residual)
    residual = re.sub(r"\s+", " ", residual).strip()
    tokens = re.findall(r"[\u4e00-\u9fa5A-Za-z0-9]{2,}", residual)
    name_query = tokens[0] if tokens else ""
    if name_query:
        name_query = _NAME_TAIL.sub("", name_query).strip()
        # 仍偏长：再整段剥一次尾（连续中文未切开时）
        if len(name_query) >= 6:
            name_query = _NAME_TAIL.sub("", name_query).strip()
    if len(name_query) < 2:
        name_query = ""

    place_names = {city, *provinces, normalize_name(city), *(normalize_name(p) for p in provinces)}
    place_names.discard("")
    if name_query and (
        name_query in place_names
        or normalize_name(name_query) in place_names
        or name_query in PROVINCE_BASES
        or name_query in CITY_BASES
        or name_query in PROVINCE_SHORT_TO_NAME
    ):
        name_query = ""

    trip_mode, trip_days = infer_trip_mode(q, city_norm=city, province_norms=provinces)

    return TravelIntent(
        city_norm=city,
        province_norm=province,
        province_norms=provinces,
        tags=tags,
        max_price=max_price,
        price_sort=price_sort,
        name_query=name_query,
        name_norm=normalize_name(name_query) if name_query else "",
        trip_mode=trip_mode,
        trip_days=trip_days,
    )

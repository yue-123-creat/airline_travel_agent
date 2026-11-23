"""Guide itinerary: same-city day OR multi-day cross-city; no coordinates for users."""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from typing import Any

from app.llm.qwen import QwenClient, QwenError
from app.tools.amap import ITINERARY_TOOLS, SpotRegistry, make_tool_dispatcher
from app.tools.city_info import get_city_info
from app.tools.weather import WEATHER_UNAVAILABLE_REPLY

_ITINERARY_BASE = """【角色】
你是航途小程序出行助手「途途」，覆盖景点推荐、行程规划、交通衔接与玩法咨询，满足用户旅行出行相关需求。本轮负责行程草案。

【规则·共用】
1. 只用 spots 名单内景点，禁止编造。
2. 缺票价/开放时间写「以现场为准」或「未知」；禁坐标；距离时间用「大约」。

【天气工具 weather_query】
1. 按需调用：只有当用户明确问天气，或询问爬山/户外是否合适、穿搭建议、带伞等受天气影响的问题，或需要按天气排户外/室内时，才调用 weather_query。
2. 强事实不查：问票价、开放时间、怎么走、历史介绍，绝对不准调天气工具。
3. 无城市不查：用户与资料都未给出城市时，工具参数禁止瞎填「北京」；必须先反问「您想查哪个城市的天气？」。有 city/spots 城市时用真实城市名。
4. 超时降级：天气 API 失败时，统一回复「""" + WEATHER_UNAVAILABLE_REPLY + """」；禁止编造预报。
"""

_ITINERARY_DAY = """【任务】
依据 spots 与工具结果，写同城一日游攻略。

【规则·一日】
1. 只安排同一城市；跨城勿塞进同一天。
2. 工具合计≤5：route 仅相邻边（N 点最多 N-1）；snapshot≤2；nearby≤1；get_city_info≤1；weather_query≤1（仅按上方天气规则）。
3. 邻近点只能来自 amap_nearby_pois；串联用 amap_route（或 amap_distance）。

【输出】
严格四段（缺内容写「暂无」），勿加其它大标题：
【上午】…
【下午】…
【交通】…（A→B：大约多久、大约多远）
【注意】…（天气、票价/开放未知、美食等）
"""

_ITINERARY_MULTI = """【任务】
依据 spots、region_foods 与用户问题，写多日行程草案。

【规则·多日】
1. 目的地以 query / place 为准：点名单城则锁定；仅多城/环线可跨城，同日近程。
2. 天数按 trip_days。
3. 工具合计≤5：勿调 nearby / 重复 get_city_info；snapshot≤2；同城才 route；跨城写高铁/汽车以当天为准；weather_query 按天气规则按需调用（每城最多 1 次）。

【输出】
严格按结构（N=trip_days），勿加其它大标题：
【第1天】…（上午/下午，写清城市；可带美食/天气一句）
【第2天】…
（直到第N天）
【交通概览】…
【注意】…（天气、季节、预约、未纳入点等）
"""

ITINERARY_SYSTEM = _ITINERARY_BASE + _ITINERARY_DAY
MULTI_DAY_SYSTEM = _ITINERARY_BASE + _ITINERARY_MULTI

_COORD_LEAK = re.compile(
    r"(?:经度|纬度|坐标|lng|lat|longitude|latitude)\s*[:=：]?\s*-?\d+(?:\.\d+)?|"
    r"-?\d{2,3}\.\d{2,}\s*[,，]\s*-?\d{2,3}\.\d{2,}",
    re.I,
)


def _coords(item: dict[str, Any]) -> tuple[float | None, float | None]:
    lat, lng = item.get("lat"), item.get("lng")
    location = item.get("location") if isinstance(item.get("location"), dict) else {}
    if lat is None:
        lat = location.get("lat")
    if lng is None:
        lng = location.get("lng")
    try:
        lat_f = float(lat) if lat is not None else None
    except (TypeError, ValueError):
        lat_f = None
    try:
        lng_f = float(lng) if lng is not None else None
    except (TypeError, ValueError):
        lng_f = None
    return lat_f, lng_f


def _city_of(item: dict[str, Any]) -> str:
    return str(item.get("city_norm") or item.get("city") or "").strip()


def select_same_city_spots(
    attractions: list[dict[str, Any]],
    *,
    preferred_city: str = "",
    limit: int = 5,
) -> tuple[str, list[dict[str, Any]], list[str]]:
    notes: list[str] = []
    ready: list[dict[str, Any]] = []
    for item in attractions:
        city = _city_of(item)
        lat, lng = _coords(item)
        name = item.get("name") or "未命名"
        if not city:
            notes.append(f"{name}：缺城市，未纳入行程")
            continue
        if lat is None or lng is None:
            notes.append(f"{name}：缺定位，未纳入行程")
            continue
        row = dict(item)
        row["lat"], row["lng"], row["city_norm"] = lat, lng, city
        ready.append(row)

    if not ready:
        return "", [], notes

    city = preferred_city or Counter(_city_of(x) for x in ready).most_common(1)[0][0]
    same = [x for x in ready if _city_of(x) == city]
    for x in ready:
        if _city_of(x) != city:
            notes.append(f"{x.get('name')}：不在{city}，未纳入同一天行程")
    return city, same[:limit], notes


def select_multi_place_spots(
    attractions: list[dict[str, Any]],
    *,
    limit: int = 10,
    per_city: int = 3,
) -> tuple[str, list[dict[str, Any]], list[str]]:
    """Cross-city candidate pool: diversify by city; prefer rows with coords."""
    notes: list[str] = []
    by_city: dict[str, list[dict[str, Any]]] = defaultdict(list)
    no_city: list[dict[str, Any]] = []

    for item in attractions:
        name = item.get("name") or "未命名"
        city = _city_of(item) or str(item.get("region") or "").strip()
        lat, lng = _coords(item)
        row = dict(item)
        row["lat"], row["lng"] = lat, lng
        row["city_norm"] = city or _city_of(item)
        if not city:
            notes.append(f"{name}：缺城市，仍作候选参考")
            no_city.append(row)
            continue
        if lat is None or lng is None:
            notes.append(f"{name}：缺定位，城际路程可能无法测算")
        by_city[city].append(row)

    picked: list[dict[str, Any]] = []
    city_order = sorted(by_city.keys(), key=lambda c: (-len(by_city[c]), c))
    counts = {c: 0 for c in city_order}
    while len(picked) < limit and city_order:
        progressed = False
        for c in list(city_order):
            if counts[c] >= per_city:
                continue
            bucket = by_city[c]
            if counts[c] >= len(bucket):
                continue
            picked.append(bucket[counts[c]])
            counts[c] += 1
            progressed = True
            if len(picked) >= limit:
                break
        if not progressed:
            break

    for row in no_city:
        if len(picked) >= limit:
            break
        picked.append(row)

    label = "、".join(city_order[:4]) if city_order else "多地"
    if len(city_order) > 4:
        label += "等"
    return label, picked[:limit], notes


def public_cards(spots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """LLM 可见名片：不含坐标。"""
    out: list[dict[str, Any]] = []
    for s in spots:
        visit = s.get("visit_info") if isinstance(s.get("visit_info"), dict) else {}
        ticket = visit.get("ticket") if isinstance(visit.get("ticket"), dict) else {}
        content = s.get("content") if isinstance(s.get("content"), dict) else {}
        loc = s.get("location") if isinstance(s.get("location"), dict) else {}
        price = s.get("adult_price")
        if price is None:
            price = ticket.get("adult_price")
        out.append(
            {
                "id": s.get("id"),
                "name": s.get("name"),
                "city": _city_of(s),
                "category": s.get("category"),
                "tags": (s.get("tags") or [])[:5],
                "address": loc.get("address"),
                "opening_hours": visit.get("opening_hours") or None,
                "adult_price": price,
                "ticket_remark": ticket.get("remark"),
                "intro": content.get("intro") or s.get("intro") or s.get("summary"),
                "tips": content.get("tips") or s.get("tips"),
                "duration_hours": visit.get("duration_hours"),
            }
        )
    return out


def strip_coordinates(text: str) -> str:
    if not text:
        return text
    return _COORD_LEAK.sub("（位置已按地名说明，不展示坐标）", text)


def format_tool_trace_human(trace: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for t in trace:
        name = t.get("name")
        res = t.get("result") or {}
        if not res.get("ok"):
            lines.append(f"- {name} 暂不可用：{res.get('error')}")
            continue
        if name in {"amap_route", "amap_distance"}:
            o, d = res.get("origin_name") or "?", res.get("dest_name") or "?"
            approx = "，".join(x for x in [res.get("approx_distance"), res.get("approx_time")] if x) or "见工具结果"
            lines.append(f"- {o} → {d}：{approx}")
        elif name == "amap_poi_snapshot":
            open_h = res.get("opening_hours") or "开放时间未知"
            if res.get("adult_price") is not None:
                price_s = f"门票大约{res.get('adult_price')}元"
            elif res.get("ticket_remark"):
                price_s = str(res.get("ticket_remark"))
            else:
                price_s = "票价未知"
            lines.append(f"- {res.get('name')}：{open_h}；{price_s}")
        elif name == "amap_nearby_pois":
            items = res.get("items") or []
            if not items:
                lines.append("- 附近：高德暂无结果")
            else:
                names = "、".join(
                    f"{i.get('name')}（{i.get('approx_distance') or '距离未知'}）" for i in items[:3]
                )
                lines.append(f"- 附近：{names}")
        elif name == "get_city_info":
            foods = "、".join((res.get("foods") or [])[:4])
            lines.append(
                f"- 地区卡片 {res.get('region')}：{res.get('season') or ''}"
                + (f"；美食 {foods}" if foods else "")
            )
        elif name in {"qweather_daily", "weather_query"}:
            place = res.get("place") or (t.get("args") or {}).get("city") or "?"
            if not res.get("ok"):
                hint = res.get("reply") or "暂不可用"
                lines.append(f"- 天气 {place}：{hint}")
            else:
                bits = []
                for d in (res.get("days") or [])[:5]:
                    bits.append(
                        f"{d.get('date') or d.get('day_index')} "
                        f"{d.get('text_day')}/{d.get('text_night')}"
                    )
                lines.append(f"- 天气 {place}：{'；'.join(bits) or '已查询'}")
        else:
            lines.append(f"- {name}：完成")
    return lines


def _registry_from_spots(spots: list[dict[str, Any]]) -> SpotRegistry:
    rows: list[dict[str, Any]] = []
    for s in spots:
        if not s.get("id"):
            continue
        rows.append(
            {
                "id": s.get("id"),
                "name": s.get("name"),
                "city": _city_of(s),
                "city_norm": _city_of(s),
                "lat": s.get("lat"),
                "lng": s.get("lng"),
                "adult_price": s.get("adult_price"),
                "is_free": s.get("is_free"),
                "visit_info": s.get("visit_info") if isinstance(s.get("visit_info"), dict) else {},
                "content": s.get("content") if isinstance(s.get("content"), dict) else {},
            }
        )
    return SpotRegistry(rows)


async def plan_structured_itinerary(
    query: str,
    attractions: list[dict[str, Any]],
    qwen: QwenClient,
    *,
    preferred_city: str = "",
) -> tuple[str, list[dict[str, Any]], list[str], str]:
    city, spots, notes = select_same_city_spots(
        attractions, preferred_city=preferred_city, limit=5
    )
    if len(spots) < 1:
        return "", [], notes or ["没有同城且能定位的景点，无法生成行程"], city

    registry = _registry_from_spots(spots)
    user = json.dumps(
        {
            "query": query,
            "city": city,
            "spots": public_cards(spots),
            "gate_notes": notes,
            "output_reminder": (
                "工具合计≤5；按需 weather_query 后再排室内/户外；"
                "只用【上午】【下午】【交通】【注意】；不要写坐标"
            ),
            "tool_budget": {
                "max_calls": 5,
                "snapshot_max": 2,
                "nearby_max": 1,
                "route": "adjacent_only",
                "weather_query": "on_demand",
            },
        },
        ensure_ascii=False,
    )
    try:
        content, _pin, _pout, trace = await qwen.chat_with_tools(
            system=ITINERARY_SYSTEM,
            user=user,
            tools=ITINERARY_TOOLS,
            tool_dispatcher=make_tool_dispatcher(registry),
            temperature=0.3,
            max_tokens=900,
            max_rounds=2,
            final_nudge=(
                "工具结果已齐。请不要再调用工具，直接按【上午】【下午】【交通】【注意】输出一日游正文。"
            ),
        )
    except QwenError:
        return "", [], notes + ["行程服务暂不可用"], city

    text = strip_coordinates((content or "").strip())
    if text and "【上午】" not in text:
        text = f"【上午】\n暂无\n【下午】\n暂无\n【交通】\n暂无\n【注意】\n{text}"
    return text, trace, notes, city


def _preinject_region_foods(spots: list[dict[str, Any]], place_hint: str) -> list[dict[str, Any]]:
    """本地字典预注入省市美食/季节，避免多日行程再调 get_city_info / nearby。"""
    places: list[str] = []
    seen: set[str] = set()
    for p in re.split(r"[、,，/\s]+", place_hint or ""):
        p = p.strip()
        if p and p not in seen:
            seen.add(p)
            places.append(p)
    for s in spots:
        for key in (s.get("region"), _city_of(s)):
            k = str(key or "").strip()
            if k and k not in seen:
                seen.add(k)
                places.append(k)
    cards: list[dict[str, Any]] = []
    for place in places[:6]:
        info = get_city_info(place)
        if not info.get("ok"):
            continue
        region = str(info.get("region") or place)
        if any(c.get("region") == region for c in cards):
            continue
        cards.append(
            {
                "region": region,
                "season": info.get("season") or "",
                "foods": list(info.get("foods") or [])[:5],
            }
        )
    return cards


async def plan_multi_day_itinerary(
    query: str,
    attractions: list[dict[str, Any]],
    qwen: QwenClient,
    *,
    trip_days: int = 3,
    place_hint: str = "",
) -> tuple[str, list[dict[str, Any]], list[str], str]:
    days = max(2, min(int(trip_days or 3), 10))
    label, spots, notes = select_multi_place_spots(attractions, limit=min(4 + days * 2, 12))
    if place_hint:
        label = place_hint
    if len(spots) < 1:
        return "", [], notes or ["没有可用候选景点，无法生成多日行程"], label

    region_foods = _preinject_region_foods(spots, label)

    registry = _registry_from_spots(spots)
    user = json.dumps(
        {
            "query": query,
            "place": label,
            "trip_days": days,
            "spots": public_cards(spots),
            "region_foods": region_foods,
            "gate_notes": notes,
            "output_reminder": (
                f"必须输出【第1天】…【第{days}天】+【交通概览】+【注意】；"
                "可跨城；按需 weather_query 后再排户外/室内；工具合计≤5；禁止 nearby；"
                "美食用 region_foods；缺结果写以现场为准"
            ),
            "tool_budget": {
                "max_calls": 5,
                "core_spots_per_day": "1-2",
                "nearby_per_day_max": 0,
                "snapshot_max": 2,
                "route": "adjacent_same_city_only",
                "cross_city_route": False,
                "skip_nearby": True,
                "skip_get_city_info": True,
                "weather_query": "on_demand",
            },
        },
        ensure_ascii=False,
    )
    try:
        content, _pin, _pout, trace = await qwen.chat_with_tools(
            system=MULTI_DAY_SYSTEM,
            user=user,
            tools=ITINERARY_TOOLS,
            tool_dispatcher=make_tool_dispatcher(registry),
            temperature=0.35,
            max_tokens=1400,
            max_rounds=2,
            final_nudge=(
                "工具结果已齐。请不要再调用工具，直接按【第N天】【交通概览】【注意】输出多日行程正文。"
            ),
        )
    except QwenError:
        return "", [], notes + ["多日行程服务暂不可用"], label

    text = strip_coordinates((content or "").strip())
    if text and "【第1天】" not in text and "【第 1 天】" not in text:
        day_blocks = "\n".join(f"【第{i}天】\n暂无" for i in range(1, days + 1))
        text = f"{day_blocks}\n【交通概览】\n暂无\n【注意】\n{text}"
    return text, trace, notes, label

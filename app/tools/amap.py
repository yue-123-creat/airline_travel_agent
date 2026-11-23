"""Amap tools for itinerary: route / POI snapshot / nearby.

Only keep fields Amap actually returns. Never invent price, open hours, or duration_hours.
Tool APIs use spot_id; coordinates stay internal and must not appear in user-facing text.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Literal

import httpx

from app.config import get_settings
from app.tools.airport_service import dispatch_airport_service_tool
from app.tools.city_info import GET_CITY_INFO_TOOL, dispatch_city_info_tool
from app.tools.weather import WEATHER_QUERY_TOOL, dispatch_weather_tool

logger = logging.getLogger(__name__)

DistanceMode = Literal["driving", "walking", "straight"]
RouteMode = Literal["driving", "walking"]

# 进程内内存缓存：poi_snapshot / place 文本检索（含坐标）结果复用
_CACHE_TTL_SEC = 6 * 3600
_CACHE_MAX = 512
_cache_lock = threading.Lock()
_amap_cache: dict[str, tuple[float, Any]] = {}
_http_lock = threading.Lock()
_http_client: httpx.Client | None = None


def _cache_get(key: str) -> Any | None:
    now = time.time()
    with _cache_lock:
        item = _amap_cache.get(key)
        if not item:
            return None
        ts, value = item
        if now - ts > _CACHE_TTL_SEC:
            _amap_cache.pop(key, None)
            return None
        return value


def _cache_set(key: str, value: Any) -> None:
    with _cache_lock:
        if len(_amap_cache) >= _CACHE_MAX:
            # 简单淘汰：删最早一半
            oldest = sorted(_amap_cache.items(), key=lambda kv: kv[1][0])[: _CACHE_MAX // 2]
            for k, _ in oldest:
                _amap_cache.pop(k, None)
        _amap_cache[key] = (time.time(), value)


def _shared_client() -> httpx.Client:
    global _http_client
    with _http_lock:
        if _http_client is None or _http_client.is_closed:
            _http_client = httpx.Client(timeout=12.0)
        return _http_client

AMAP_ROUTE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "amap_route",
        "description": (
            "高德路径规划：按景点 id 算驾车/步行路线，返回大约公里数与大约分钟数。"
            "串联景点时必须调用；不要自己估时间。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "origin_id": {"type": "string", "description": "起点景点 id"},
                "dest_id": {"type": "string", "description": "终点景点 id"},
                "mode": {"type": "string", "enum": ["driving", "walking"]},
            },
            "required": ["origin_id", "dest_id"],
        },
    },
}

AMAP_POI_SNAPSHOT_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "amap_poi_snapshot",
        "description": (
            "高德景点实况：开放时间、门票原文（有数字才解析价格）。"
            "没有就返回空/未知，禁止编造。"
        ),
        "parameters": {
            "type": "object",
            "properties": {"spot_id": {"type": "string"}},
            "required": ["spot_id"],
        },
    },
}

AMAP_NEARBY_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "amap_nearby_pois",
        "description": (
            "高德周边检索。keywords 可传「风景名胜」或「美食」「餐饮」查附近设施/美食。"
            "只返回实际结果；没有则为空列表。多日行程请每天只对 1 个核心点调用 1 次。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "spot_id": {"type": "string"},
                "radius_m": {"type": "integer", "description": "默认3000，最大5000"},
                "keywords": {
                    "type": "string",
                    "description": "默认风景名胜；美食/餐饮用于核心点附近设施",
                },
            },
            "required": ["spot_id"],
        },
    },
}

AMAP_DISTANCE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "amap_distance",
        "description": "轻量距离备用；优先用 amap_route。",
        "parameters": {
            "type": "object",
            "properties": {
                "origin_id": {"type": "string"},
                "dest_id": {"type": "string"},
                "mode": {"type": "string", "enum": ["driving", "walking", "straight"]},
            },
            "required": ["origin_id", "dest_id"],
        },
    },
}

ITINERARY_TOOLS: list[dict[str, Any]] = [
    AMAP_ROUTE_TOOL,
    AMAP_POI_SNAPSHOT_TOOL,
    AMAP_NEARBY_TOOL,
    AMAP_DISTANCE_TOOL,
    GET_CITY_INFO_TOOL,
    WEATHER_QUERY_TOOL,
]


def _key_and_base() -> tuple[str | None, str | None]:
    settings = get_settings()
    key = (settings.amap_api_key or "").strip()
    if not key:
        return None, None
    return key, (settings.amap_base_url or "https://restapi.amap.com").rstrip("/")


def _cacheable_geo_path(path: str) -> bool:
    """place/text（含坐标）与 place/around 可缓存；路径规划实时性要求更高，不缓存。"""
    p = path or ""
    return "/place/text" in p or "/place/around" in p


def _get_json(path: str, params: dict[str, Any]) -> dict[str, Any]:
    key, base = _key_and_base()
    if not key or not base:
        return {"status": "0", "info": "AMAP_API_KEY not configured"}
    # 稳定缓存键：去掉 key，按 path+排序参数
    cache_key = ""
    if _cacheable_geo_path(path):
        items = tuple(sorted((k, str(v)) for k, v in params.items() if k != "key"))
        cache_key = f"amap:{path}:{items}"
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached
    try:
        client = _shared_client()
        resp = client.get(f"{base}{path}", params={**params, "key": key})
        data = resp.json()
        if cache_key and str(data.get("status")) == "1":
            _cache_set(cache_key, data)
        return data
    except Exception as e:
        logger.warning("amap request failed %s: %s", path, e)
        return {"status": "0", "info": str(e)[:200]}


def _blank(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, (list, dict)) and not v:
        return None
    s = str(v).strip()
    return s or None


def normalize_cache_name(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").strip()).lower()


def _parse_cost(cost: str | None) -> tuple[float | None, bool | None, str | None]:
    if not cost or not str(cost).strip():
        return None, None, None
    text = str(cost).strip()
    if "免费" in text or text in {"0", "0.00", "0元"}:
        return 0.0, True, text
    m = re.search(r"(\d+(?:\.\d+)?)", text)
    if m:
        price = float(m.group(1))
        return price, price == 0.0, text
    return None, None, text


def _human_duration(seconds: float | None) -> str | None:
    if seconds is None or seconds <= 0:
        return None
    minutes = int(round(seconds / 60.0))
    if minutes < 60:
        return f"大约{minutes}分钟"
    h, m = divmod(minutes, 60)
    return f"大约{h}小时" if m == 0 else f"大约{h}小时{m}分钟"


def _human_distance(meters: float | None) -> str | None:
    if meters is None or meters <= 0:
        return None
    if meters < 1000:
        return f"大约{int(round(meters))}米"
    return f"大约{round(meters / 1000.0, 1)}公里"


def amap_route_by_coords(
    *,
    origin_lng: float,
    origin_lat: float,
    dest_lng: float,
    dest_lat: float,
    mode: RouteMode = "driving",
    origin_name: str = "",
    dest_name: str = "",
) -> dict[str, Any]:
    settings = get_settings()
    if mode == "walking":
        path = getattr(settings, "amap_direction_walking_path", None) or "/v3/direction/walking"
        params = {"origin": f"{origin_lng},{origin_lat}", "destination": f"{dest_lng},{dest_lat}"}
    else:
        path = getattr(settings, "amap_direction_driving_path", None) or "/v3/direction/driving"
        params = {
            "origin": f"{origin_lng},{origin_lat}",
            "destination": f"{dest_lng},{dest_lat}",
            "extensions": "base",
            "strategy": "0",
        }
    data = _get_json(path, params)
    if str(data.get("status")) != "1":
        return {
            "ok": False,
            "error": data.get("info") or "amap_route_error",
            "origin_name": origin_name or None,
            "dest_name": dest_name or None,
        }
    paths = (data.get("route") or {}).get("paths") or []
    if not paths or not isinstance(paths[0], dict):
        return {"ok": False, "error": "empty_paths", "origin_name": origin_name or None, "dest_name": dest_name or None}
    best = paths[0]
    try:
        meters = float(best.get("distance") or 0)
    except (TypeError, ValueError):
        meters = 0.0
    try:
        seconds = float(best.get("duration") or 0)
    except (TypeError, ValueError):
        seconds = 0.0
    steps: list[str] = []
    for step in (best.get("steps") or [])[:5]:
        if isinstance(step, dict):
            instr = _blank(step.get("instruction")) or _blank(step.get("road"))
            if instr:
                steps.append(instr)
    return {
        "ok": True,
        "mode": mode,
        "origin_name": origin_name or None,
        "dest_name": dest_name or None,
        "approx_distance": _human_distance(meters),
        "approx_time": _human_duration(seconds),
        "step_summary": steps,
    }


def amap_distance_by_coords(
    *,
    origin_lng: float,
    origin_lat: float,
    dest_lng: float,
    dest_lat: float,
    mode: DistanceMode = "driving",
    origin_name: str = "",
    dest_name: str = "",
) -> dict[str, Any]:
    settings = get_settings()
    type_map = {"driving": "1", "walking": "3", "straight": "0"}
    path = settings.amap_distance_path or "/v3/distance"
    data = _get_json(
        path,
        {
            "origins": f"{origin_lng},{origin_lat}",
            "destination": f"{dest_lng},{dest_lat}",
            "type": type_map.get(mode, "1"),
        },
    )
    if str(data.get("status")) != "1":
        return {
            "ok": False,
            "error": data.get("info") or "amap_error",
            "origin_name": origin_name or None,
            "dest_name": dest_name or None,
        }
    results = data.get("results") or []
    if not results or not isinstance(results[0], dict):
        return {"ok": False, "error": "empty_results"}
    first = results[0]
    try:
        meters = float(first.get("distance") or 0)
    except (TypeError, ValueError):
        meters = 0.0
    try:
        seconds = float(first.get("duration") or 0)
    except (TypeError, ValueError):
        seconds = 0.0
    return {
        "ok": True,
        "mode": mode,
        "origin_name": origin_name or None,
        "dest_name": dest_name or None,
        "approx_distance": _human_distance(meters),
        "approx_time": _human_duration(seconds),
    }


def _normalize_snapshot_city(city: str) -> str:
    """Drop unusable city hints (slash multi-city, bare 县/地区等) that break place/text."""
    c = (city or "").strip()
    if not c:
        return ""
    if "/" in c or "\\" in c or "、" in c:
        return ""
    # 高德 city 更吃「市/区」名；纯「xx县」偶发 USER_ABROAD_* 限额，交给无 city 回退
    if c.endswith("县") and len(c) <= 4:
        return ""
    return c


def extract_photo_urls(photos: Any, *, limit: int = 3) -> list[str]:
    """高德 photos 数组（或 url 列表）→ 去重 URL。"""
    out: list[str] = []
    seen: set[str] = set()
    if not isinstance(photos, list):
        return out
    for ph in photos:
        url = ""
        if isinstance(ph, dict):
            url = str(ph.get("url") or "").strip()
        elif isinstance(ph, str):
            url = ph.strip()
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(url)
        if len(out) >= max(1, limit):
            break
    return out


def photos_to_markdown(spot_name: str, photos: Any, *, limit: int = 3) -> str:
    """把高德 photos 转成 ![](URL) 多行 Markdown（无 alt，直接出图）。"""
    _ = spot_name  # 保留入参兼容调用方；alt 故意留空
    urls = extract_photo_urls(photos, limit=limit)
    if not urls and isinstance(photos, list):
        # 已是纯 URL 列表时 extract 已覆盖；再兜底一次
        urls = [str(u).strip() for u in photos if isinstance(u, str) and str(u).strip()][:limit]
    return "\n".join(f"![]({url})" for url in urls)


def amap_poi_snapshot_by_name(*, name: str, city: str = "") -> dict[str, Any]:
    city_use = _normalize_snapshot_city(city)
    snap_key = f"poi_snapshot_v3:{normalize_cache_name(name)}|{normalize_cache_name(city_use)}"
    cached = _cache_get(snap_key)
    if isinstance(cached, dict):
        return cached

    settings = get_settings()
    path = getattr(settings, "amap_place_text_path", None) or "/v3/place/text"

    def _search(city_arg: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        params: dict[str, Any] = {"keywords": name, "offset": 5, "page": 1, "extensions": "all"}
        if city_arg:
            params["city"] = city_arg
            params["citylimit"] = "true"
        data = _get_json(path, params)
        if str(data.get("status")) != "1":
            return data, []
        return data, [p for p in (data.get("pois") or []) if isinstance(p, dict)]

    data, pois = _search(city_use)
    # 带 city 失败或空结果时，无 city 再试一次（异常 city_norm 常触发限额/空集）
    if city_use and (str(data.get("status")) != "1" or not pois):
        data2, pois2 = _search("")
        if str(data2.get("status")) == "1" and pois2:
            data, pois = data2, pois2
        elif str(data.get("status")) != "1" and str(data2.get("status")) != "1":
            return {"ok": False, "error": data2.get("info") or data.get("info") or "poi_search_failed", "name": name}

    if str(data.get("status")) != "1":
        return {"ok": False, "error": data.get("info") or "poi_search_failed", "name": name}
    if not pois:
        empty = {
            "ok": True,
            "name": name,
            "opening_hours": None,
            "adult_price": None,
            "is_free": None,
            "ticket_remark": None,
            "photos": [],
            "photos_md": "",
            "note": "高德未返回匹配，开放时间/票价未知",
        }
        _cache_set(snap_key, empty)
        return empty

    def score(p: dict[str, Any]) -> int:
        title = str(p.get("name") or "")
        s = 100 if title == name else (40 if name in title else 0)
        if str(p.get("type") or "").startswith("旅游景点"):
            s += 20
        return s

    poi = sorted(pois, key=score, reverse=True)[0]
    biz = poi.get("biz_ext") if isinstance(poi.get("biz_ext"), dict) else {}
    opening = (
        _blank(biz.get("opentime2"))
        or _blank(biz.get("open_time"))
        or _blank(biz.get("opentime"))
        or _blank(poi.get("opentime2"))
        or _blank(poi.get("opentime"))
    )
    cost = _blank(biz.get("cost")) or _blank(poi.get("cost"))
    price, is_free, remark = _parse_cost(cost)
    lat = lng = None
    loc = (_blank(poi.get("location")) or "").split(",")
    if len(loc) == 2:
        try:
            lng, lat = float(loc[0]), float(loc[1])
        except ValueError:
            lat = lng = None
    poi_name = _blank(poi.get("name")) or name
    photo_urls = extract_photo_urls(poi.get("photos"), limit=3)
    photos_md = photos_to_markdown(poi_name, photo_urls, limit=3)
    result = {
        "ok": True,
        "name": poi_name,
        "address": _blank(poi.get("address")),
        "opening_hours": opening,
        "adult_price": price,
        "is_free": is_free,
        "ticket_remark": remark,
        "duration_hours": None,
        "lat": lat,
        "lng": lng,
        "adcode": _blank(poi.get("adcode")),
        "pname": _blank(poi.get("pname")),
        "cityname": _blank(poi.get("cityname")),
        "photos": photo_urls,
        "photos_md": photos_md,
    }
    _cache_set(snap_key, result)
    return result


def amap_nearby_by_coords(
    *,
    lng: float,
    lat: float,
    self_name: str = "",
    radius_m: int = 3000,
    keywords: str = "风景名胜",
) -> dict[str, Any]:
    settings = get_settings()
    path = getattr(settings, "amap_place_around_path", None) or "/v3/place/around"
    radius_m = max(500, min(int(radius_m or 3000), 5000))
    kw = (keywords or "风景名胜").strip() or "风景名胜"
    # 美食/餐饮不限制旅游景点 typecode
    types = "" if any(x in kw for x in ("美食", "餐饮", "小吃", "餐厅")) else "110000"
    params: dict[str, Any] = {
        "location": f"{lng},{lat}",
        "keywords": kw,
        "radius": radius_m,
        "offset": 10,
        "page": 1,
        "extensions": "all",
    }
    if types:
        params["types"] = types
    data = _get_json(path, params)
    if str(data.get("status")) != "1":
        return {"ok": False, "error": data.get("info") or "nearby_failed", "items": []}
    self_n = re.sub(r"\s+", "", self_name or "")
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for poi in data.get("pois") or []:
        if not isinstance(poi, dict):
            continue
        name = _blank(poi.get("name"))
        if not name:
            continue
        key = re.sub(r"\s+", "", name)
        if self_n and (key == self_n or self_n in key or key in self_n):
            continue
        if key in seen:
            continue
        try:
            dist_m = float(poi.get("distance")) if poi.get("distance") not in (None, "") else None
        except (TypeError, ValueError):
            dist_m = None
        if dist_m is not None and dist_m < 200:
            continue
        biz = poi.get("biz_ext") if isinstance(poi.get("biz_ext"), dict) else {}
        opening = _blank(biz.get("opentime2")) or _blank(biz.get("opentime"))
        price, is_free, remark = _parse_cost(_blank(biz.get("cost")))
        seen.add(key)
        items.append(
            {
                "name": name,
                "approx_distance": _human_distance(dist_m),
                "opening_hours": opening,
                "adult_price": price,
                "is_free": is_free,
                "ticket_remark": remark,
            }
        )
        if len(items) >= 5:
            break
    return {"ok": True, "center": self_name or None, "items": items}


class SpotRegistry:
    def __init__(self, spots: list[dict[str, Any]]):
        self.by_id = {str(s.get("id")).strip(): s for s in spots if s.get("id")}

    def get(self, spot_id: str) -> dict[str, Any] | None:
        return self.by_id.get(str(spot_id or "").strip())


def make_tool_dispatcher(registry: SpotRegistry):
    def _resolve(spot_id: str):
        spot = registry.get(spot_id)
        if not spot:
            return None, {"ok": False, "error": f"unknown_spot_id:{spot_id}"}
        if spot.get("lat") is None or spot.get("lng") is None:
            return spot, {"ok": False, "error": f"spot_missing_location:{spot.get('name')}"}
        return spot, None

    def dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
        city_info_res = dispatch_city_info_tool(name, args)
        if city_info_res is not None:
            return city_info_res
        airport_res = dispatch_airport_service_tool(name, args)
        if airport_res is not None:
            return airport_res
        weather_res = dispatch_weather_tool(name, args)
        if weather_res is not None:
            return weather_res
        if name == "amap_route":
            o, err = _resolve(str(args.get("origin_id") or ""))
            if err:
                return err
            d, err = _resolve(str(args.get("dest_id") or ""))
            if err:
                return err
            assert o and d
            return amap_route_by_coords(
                origin_lng=float(o["lng"]),
                origin_lat=float(o["lat"]),
                dest_lng=float(d["lng"]),
                dest_lat=float(d["lat"]),
                mode=str(args.get("mode") or "driving"),  # type: ignore[arg-type]
                origin_name=str(o.get("name") or ""),
                dest_name=str(d.get("name") or ""),
            )
        if name == "amap_distance":
            o, err = _resolve(str(args.get("origin_id") or ""))
            if err:
                return err
            d, err = _resolve(str(args.get("dest_id") or ""))
            if err:
                return err
            assert o and d
            return amap_distance_by_coords(
                origin_lng=float(o["lng"]),
                origin_lat=float(o["lat"]),
                dest_lng=float(d["lng"]),
                dest_lat=float(d["lat"]),
                mode=str(args.get("mode") or "driving"),  # type: ignore[arg-type]
                origin_name=str(o.get("name") or ""),
                dest_name=str(d.get("name") or ""),
            )
        if name == "amap_poi_snapshot":
            spot = registry.get(str(args.get("spot_id") or ""))
            if not spot:
                return {"ok": False, "error": "unknown_spot_id"}
            # 名片已有开放时间：跳过高德，省一次 geo/place 查询
            visit = spot.get("visit_info") if isinstance(spot.get("visit_info"), dict) else {}
            opening = _blank(visit.get("opening_hours"))
            if not opening:
                content = spot.get("content") if isinstance(spot.get("content"), dict) else {}
                opening = _blank(content.get("opening_hours"))
            ticket = visit.get("ticket") if isinstance(visit.get("ticket"), dict) else {}
            if opening:
                price = spot.get("adult_price")
                if price is None:
                    price = ticket.get("adult_price")
                return {
                    "ok": True,
                    "name": spot.get("name"),
                    "opening_hours": opening,
                    "adult_price": price,
                    "is_free": spot.get("is_free") if spot.get("is_free") is not None else ticket.get("is_free"),
                    "ticket_remark": ticket.get("remark"),
                    "source": "kb_card",
                    "note": "名片已有开放时间，未重复请求高德",
                }
            return amap_poi_snapshot_by_name(
                name=str(spot.get("name") or ""),
                city=str(spot.get("city") or spot.get("city_norm") or ""),
            )
        if name == "amap_nearby_pois":
            spot, err = _resolve(str(args.get("spot_id") or ""))
            if err:
                return err
            assert spot
            return amap_nearby_by_coords(
                lng=float(spot["lng"]),
                lat=float(spot["lat"]),
                self_name=str(spot.get("name") or ""),
                radius_m=int(args.get("radius_m") or 3000),
                keywords=str(args.get("keywords") or "风景名胜"),
            )
        return {"ok": False, "error": f"unknown_tool:{name}"}

    return dispatch


def amap_distance(**kwargs: Any) -> dict[str, Any]:
    return amap_distance_by_coords(**kwargs)


def dispatch_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name in {"amap_distance", "amap_route"} and "origin_lng" in args:
        fn = amap_route_by_coords if name == "amap_route" else amap_distance_by_coords
        return fn(
            origin_lng=float(args["origin_lng"]),
            origin_lat=float(args["origin_lat"]),
            dest_lng=float(args["dest_lng"]),
            dest_lat=float(args["dest_lat"]),
            mode=str(args.get("mode") or "driving"),  # type: ignore[arg-type]
            origin_name=str(args.get("origin_name") or ""),
            dest_name=str(args.get("dest_name") or ""),
        )
    return {"ok": False, "error": f"unknown_tool:{name}"}

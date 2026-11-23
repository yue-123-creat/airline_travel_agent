"""QWeather daily forecast + LLM tool `weather_query`.

调用时机由 Travel Prompt 约束、模型自主决定；本模块不写死「if 天气」门控。
Auth: X-QW-Api-Key against project API Host.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

# 天气 API 失败时对用户统一口径（Prompt 与工具返回共用）
WEATHER_UNAVAILABLE_REPLY = "暂未获取到实时天气，建议出行前关注天气预报"

WEATHER_QUERY_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "weather_query",
        "description": (
            "查询指定城市的天气预报。"
            "仅当用户明确问天气，或问爬山/户外是否合适、穿搭建议、带伞等受天气影响的问题时调用；"
            "问票价、开放时间、怎么走、历史介绍时严禁调用。"
            "必须传入用户提到的真实城市名；禁止默认填北京或其它城市。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "城市名（如杭州、上海）。禁止留空，禁止擅自填北京。",
                },
                "days": {
                    "type": "integer",
                    "description": "预报天数 1～7，默认 3",
                },
            },
            "required": ["city"],
        },
    },
}

_cache_lock = threading.Lock()
_weather_cache: dict[str, tuple[float, Any]] = {}
_http_lock = threading.Lock()
_http_client: httpx.Client | None = None

_CACHE_MAX = 256
_MAX_WORKERS = 4


def _shared_client(timeout: float) -> httpx.Client:
    global _http_client
    with _http_lock:
        if _http_client is None or _http_client.is_closed:
            _http_client = httpx.Client(timeout=timeout)
        return _http_client


def _cache_get(key: str, ttl: float) -> Any | None:
    now = time.time()
    with _cache_lock:
        item = _weather_cache.get(key)
        if not item:
            return None
        ts, value = item
        if now - ts > ttl:
            _weather_cache.pop(key, None)
            return None
        return value


def _cache_set(key: str, value: Any) -> None:
    with _cache_lock:
        if len(_weather_cache) >= _CACHE_MAX:
            oldest = sorted(_weather_cache.items(), key=lambda kv: kv[1][0])[: _CACHE_MAX // 2]
            for k, _ in oldest:
                _weather_cache.pop(k, None)
        _weather_cache[key] = (time.time(), value)


def _configured() -> tuple[str, str, str, float, int] | None:
    s = get_settings()
    key = (s.qweather_api_key or "").strip()
    base = (s.qweather_base_url or "").strip().rstrip("/")
    path = (s.qweather_daily_path or "/weather/v1/daily").strip().rstrip("/")
    if not key or not base:
        return None
    timeout = float(getattr(s, "qweather_timeout_sec", 4.0) or 4.0)
    ttl = int(getattr(s, "qweather_cache_ttl_sec", 1800) or 1800)
    return key, base, path, timeout, ttl


def _round_coord(v: float) -> float:
    return round(float(v), 2)


def _num(obj: Any, *keys: str, default: float | None = None) -> float | None:
    cur = obj
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    try:
        return float(cur) if cur is not None else default
    except (TypeError, ValueError):
        return default


def _date_label(iso: str) -> str:
    """Extract YYYY-MM-DD or MM-DD from forecastStartTime."""
    s = (iso or "").strip()
    if not s:
        return ""
    # 2026-11-16T00:00+08:00
    day = s[:10] if len(s) >= 10 else s
    if len(day) == 10 and day[4] == "-" and day[7] == "-":
        return day
    return day


def _compact_day(day: dict[str, Any], index: int) -> dict[str, Any]:
    daytime = day.get("daytime") if isinstance(day.get("daytime"), dict) else {}
    nighttime = day.get("nighttime") if isinstance(day.get("nighttime"), dict) else {}
    day_cond = daytime.get("condition") if isinstance(daytime.get("condition"), dict) else {}
    night_cond = nighttime.get("condition") if isinstance(nighttime.get("condition"), dict) else {}
    rain_day = _num(daytime, "precipitation", "probability", default=0.0) or 0.0
    rain_night = _num(nighttime, "precipitation", "probability", default=0.0) or 0.0
    rain_prob = max(rain_day, rain_night)
    tmax = _num(day, "temperatureMax", "value")
    tmin = _num(day, "temperatureMin", "value")
    text_day = str(day_cond.get("text") or "").strip() or "未知"
    text_night = str(night_cond.get("text") or "").strip() or "未知"
    date = _date_label(str(day.get("forecastStartTime") or ""))
    outdoor_ok = rain_prob < 0.55 and "雨" not in text_day and "雪" not in text_day
    return {
        "day_index": index + 1,
        "date": date,
        "text_day": text_day,
        "text_night": text_night,
        "tmax": round(tmax, 1) if tmax is not None else None,
        "tmin": round(tmin, 1) if tmin is not None else None,
        "rain_prob": round(rain_prob, 2),
        "outdoor_ok": outdoor_ok,
    }


def _planning_hint(days: list[dict[str, Any]]) -> str:
    wet = [d for d in days if not d.get("outdoor_ok")]
    if not wet:
        return "未来几天降水压力不大，可优先安排户外景点；注意温差添衣。"
    labels = "、".join(
        f"第{d.get('day_index')}天({d.get('date') or '?'})" for d in wet[:4]
    )
    return f"{labels}降水/天气不佳，宜安排室内或机动景点，户外改到晴好日。"


def fetch_daily_forecast(
    *,
    lat: float,
    lng: float,
    days: int = 3,
    place: str = "",
) -> dict[str, Any]:
    """Fetch compact daily forecast for one location. Soft-fails with ok=False."""
    cfg = _configured()
    if not cfg:
        return {"ok": False, "error": "qweather_not_configured", "place": place}
    key, base, path, timeout, ttl = cfg
    days_n = max(1, min(int(days or 3), 10))
    lat_r, lng_r = _round_coord(lat), _round_coord(lng)
    cache_key = f"qw:daily:{lat_r}:{lng_r}:{days_n}"
    cached = _cache_get(cache_key, float(ttl))
    if isinstance(cached, dict):
        out = dict(cached)
        if place:
            out["place"] = place
        return out

    url = f"{base}{path}/{lat_r}/{lng_r}"
    try:
        client = _shared_client(timeout)
        resp = client.get(
            url,
            params={"days": days_n, "localTime": "true", "lang": "zh"},
            headers={"X-QW-Api-Key": key},
        )
        if resp.status_code >= 400:
            return {
                "ok": False,
                "error": f"http_{resp.status_code}",
                "place": place,
                "lat": lat_r,
                "lng": lng_r,
            }
        data = resp.json()
    except Exception as e:
        logger.warning("qweather request failed: %s", e)
        return {"ok": False, "error": f"request_failed:{type(e).__name__}", "place": place}

    raw_days = data.get("days") if isinstance(data, dict) else None
    if not isinstance(raw_days, list) or not raw_days:
        return {"ok": False, "error": "empty_forecast", "place": place}

    compact = [_compact_day(d, i) for i, d in enumerate(raw_days[:days_n]) if isinstance(d, dict)]
    attributions = []
    meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    for a in meta.get("attributions") or []:
        if a:
            attributions.append(str(a))
    result = {
        "ok": True,
        "place": place or f"{lat_r},{lng_r}",
        "lat": lat_r,
        "lng": lng_r,
        "days": compact,
        "hint": _planning_hint(compact),
        "source": "qweather",
        "attributions": attributions[:2],
    }
    _cache_set(cache_key, result)
    return result


def _city_of(item: dict[str, Any]) -> str:
    return str(item.get("city_norm") or item.get("city") or item.get("region") or "").strip()


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


def weather_briefs_for_spots(
    spots: list[dict[str, Any]],
    *,
    days: int = 3,
    max_cities: int | None = None,
) -> list[dict[str, Any]]:
    """One forecast per city (first spot with coords). Parallel + soft-fail."""
    settings = get_settings()
    limit = max_cities
    if limit is None:
        limit = int(getattr(settings, "qweather_max_cities", 4) or 4)
    limit = max(1, min(int(limit), 6))

    # city -> (lat, lng)
    targets: list[tuple[str, float, float]] = []
    seen: set[str] = set()
    for s in spots:
        city = _city_of(s) or "未知地点"
        if city in seen:
            continue
        lat, lng = _coords(s)
        if lat is None or lng is None:
            continue
        seen.add(city)
        targets.append((city, lat, lng))
        if len(targets) >= limit:
            break

    if not targets:
        return []

    briefs: list[dict[str, Any]] = []
    # 同城多点时 targets 通常很少；线程池并行压延迟
    workers = min(_MAX_WORKERS, len(targets))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                fetch_daily_forecast, lat=lat, lng=lng, days=days, place=city
            ): city
            for city, lat, lng in targets
        }
        for fut in as_completed(futures):
            try:
                res = fut.result()
            except Exception as e:
                briefs.append({"ok": False, "place": futures[fut], "error": str(e)})
                continue
            if res.get("ok"):
                # LLM 侧不需要坐标
                briefs.append(
                    {
                        "ok": True,
                        "place": res.get("place"),
                        "days": res.get("days") or [],
                        "hint": res.get("hint") or "",
                        "source": "qweather",
                    }
                )
            else:
                briefs.append(
                    {
                        "ok": False,
                        "place": res.get("place") or futures[fut],
                        "error": res.get("error") or "unknown",
                    }
                )
    # stable order by place name
    briefs.sort(key=lambda x: str(x.get("place") or ""))
    return briefs


def format_weather_briefs_human(briefs: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for b in briefs:
        place = b.get("place") or "?"
        if not b.get("ok"):
            lines.append(f"- {place}：天气暂不可用（{b.get('error') or 'unknown'}）")
            continue
        day_bits: list[str] = []
        for d in (b.get("days") or [])[:7]:
            date = d.get("date") or f"D{d.get('day_index')}"
            tmax, tmin = d.get("tmax"), d.get("tmin")
            temp = ""
            if tmax is not None and tmin is not None:
                temp = f"{tmin:.0f}~{tmax:.0f}℃"
            elif tmax is not None:
                temp = f"最高{tmax:.0f}℃"
            rain = d.get("rain_prob")
            rain_s = f"，降水约{int(round(float(rain) * 100))}%" if rain is not None else ""
            day_bits.append(
                f"{date} 白天{d.get('text_day')}/夜间{d.get('text_night')}"
                + (f" {temp}" if temp else "")
                + rain_s
            )
        hint = str(b.get("hint") or "").strip()
        body = "；".join(day_bits) if day_bits else "无预报"
        lines.append(f"- {place}：{body}" + (f"。建议：{hint}" if hint else ""))
    return lines


def _geocode_city(city: str) -> tuple[float | None, float | None, str]:
    """用高德地名检索解析城市坐标；失败返回 (None, None, city)。"""
    from app.tools.amap import amap_poi_snapshot_by_name

    place = (city or "").strip()
    if not place:
        return None, None, ""
    snap = amap_poi_snapshot_by_name(name=place, city=place)
    lat, lng = snap.get("lat"), snap.get("lng")
    try:
        lat_f = float(lat) if lat is not None else None
    except (TypeError, ValueError):
        lat_f = None
    try:
        lng_f = float(lng) if lng is not None else None
    except (TypeError, ValueError):
        lng_f = None
    label = str(snap.get("cityname") or snap.get("name") or place).strip() or place
    return lat_f, lng_f, label


def run_weather_query(*, city: str = "", days: int = 3) -> dict[str, Any]:
    """LLM tool 实现：无城市不查；API 失败统一降级文案。"""
    place = (city or "").strip()
    if not place:
        return {
            "ok": False,
            "error": "missing_city",
            "reply": "您想查哪个城市的天气？",
        }
    days_n = max(1, min(int(days or 3), 7))
    lat, lng, label = _geocode_city(place)
    if lat is None or lng is None:
        return {
            "ok": False,
            "error": "geocode_failed",
            "place": place,
            "reply": WEATHER_UNAVAILABLE_REPLY,
        }
    result = fetch_daily_forecast(lat=lat, lng=lng, days=days_n, place=label or place)
    if not result.get("ok"):
        return {
            "ok": False,
            "error": result.get("error") or "weather_unavailable",
            "place": label or place,
            "reply": WEATHER_UNAVAILABLE_REPLY,
        }
    return result


def dispatch_weather_tool(name: str, args: dict[str, Any]) -> dict[str, Any] | None:
    if name not in {"weather_query", "qweather_daily"}:
        return None
    return run_weather_query(
        city=str(args.get("city") or args.get("place") or ""),
        days=int(args.get("days") or 3),
    )

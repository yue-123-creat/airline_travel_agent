#!/usr/bin/env python3
"""
将 attraction_name_city.json 补全为完整景点结构，
输出到 data/knowledge/attractions_enriched.json（不覆盖源文件）。

规则（与产品约定一致）：
- location / 营业时间 / 门票：尽量走高德；门票未知则 adult_price=null，remark=以景区为准
- tags / category（若灵犀无 category）：可用千问做简单分类
- intro / highlights / tips：只从 travel_knowledge.json（灵犀）搬；没有则 null / []
- rag_links.guide_chunk_ids 固定 []，只写 last_updated
- 禁止用千问生成 intro/highlights/tips
- national-5a 模板文案不搬入 content

依赖：httpx、python-dotenv（项目已有）；拼音缩写建议 pip install pypinyin

用法：
  # 推荐：每次只扩 2 个 region
  python scripts/enrich_attractions_pilot.py --regions 上海,江苏 --resume
  python scripts/enrich_attractions_pilot.py --regions 浙江,安徽 --resume

  # 只看名单
  python scripts/enrich_attractions_pilot.py --dry-run --regions 四川,重庆

  # 重跑该 region 中缺地图结果的条目
  python scripts/enrich_attractions_pilot.py --regions 香港,澳门 --refresh-missing

  # 按城市（兼容旧参数）
  python scripts/enrich_attractions_pilot.py --cities 成都,上海 --resume

合并规则：重跑时坐标/地址可用腾讯更新；opening_hours 与门票若旧有新无则保留旧值。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

# Windows 控制台常见 GBK，避免景点名中的 • 等字符打崩 dry-run
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = ROOT / "data" / "knowledge" / "attraction_name_city.json"
LINGXI_PATH = ROOT / "data" / "knowledge" / "travel_knowledge.json"
OUT_PATH = ROOT / "data" / "knowledge" / "attractions_enriched.json"

# 常见「市名嵌在更长 city 字段」里的底座（按长度降序匹配）
_CITY_BASES = tuple(
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

# 城市中文 -> id 用拼音缩写（不足再退回自动拼音）
CITY_ABBR: dict[str, str] = {
    "上海": "sh",
    "成都": "cd",
    "北京": "bj",
    "杭州": "hz",
    "重庆": "cq",
    "西安": "xa",
    "广州": "gz",
    "深圳": "sz",
    "南京": "nj",
    "苏州": "szu",
    "武汉": "wh",
    "长沙": "cs",
    "昆明": "km",
    "丽江": "lj",
    "大理": "dl",
    "桂林": "gl",
    "厦门": "xm",
    "青岛": "qd",
    "天津": "tj",
    "三亚": "sy",
}


def normalize_city(city: str) -> str:
    """上海浦东 / 成都都江堰 -> 上海 / 成都；未知城市原样返回。"""
    city = (city or "").strip()
    if not city:
        return ""
    for base in _CITY_BASES:
        if city == base or city.startswith(base) or base in city:
            return base
    return city


def load_env() -> None:
    load_dotenv(ROOT / ".env")


# ---------- id / 拼音 ----------

def _pinyin_initials(text: str, max_len: int = 8) -> str:
    text = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", text or "")
    if not text:
        return "unk"
    try:
        from pypinyin import Style, lazy_pinyin

        parts = lazy_pinyin(text, style=Style.FIRST_LETTER)
        abbr = "".join(p[0] for p in parts if p).lower()
        abbr = re.sub(r"[^a-z0-9]", "", abbr)
        return (abbr or "unk")[:max_len]
    except ImportError:
        # 无 pypinyin 时退化为 unicode 码位短哈希，可读性差，仅保证可跑通
        digest = abs(hash(text)) % 100000
        return f"n{digest}"[:max_len]


def make_spot_id(city: str, name: str, seq: int) -> str:
    city_abbr = CITY_ABBR.get(normalize_city(city)) or _pinyin_initials(normalize_city(city), 4)
    name_abbr = _pinyin_initials(name, 8)
    return f"spot_{city_abbr}_{name_abbr}_{seq:03d}"


# ---------- 灵犀匹配 ----------

def _norm_name(name: str) -> str:
    return re.sub(r"[\s·•\-－—_/｜|]", "", name or "")


def build_lingxi_index(data: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = {}
    for item in data.get("attractions") or []:
        key = _norm_name(item.get("name") or "")
        if key:
            index.setdefault(key, []).append(item)
    for region in data.get("regions") or []:
        for item in region.get("attractions") or []:
            merged = {
                **item,
                "region": item.get("region") or region.get("region"),
                "source": item.get("source") or "region",
            }
            key = _norm_name(merged.get("name") or "")
            if key:
                index.setdefault(key, []).append(merged)
    return index


_SOURCE_RANK = {"detail": 3, "national-5a": 2, "province": 1, "region": 0}

# national-5a 批量模板文案特征：可匹配到记录，但不把这类内容搬进 intro/highlights/tips
_TEMPLATE_MARKERS = (
    "是国家5A级旅游景区",
    "适合作为目的地核心景点",
    "辨识度高，适合作为目的地",
    "如果用户第一次到当地，可以把这里作为了解地方风景",
)


def is_lingxi_template(lx: dict[str, Any] | None) -> bool:
    if not lx:
        return False
    text = " ".join(
        [
            str(lx.get("intro") or ""),
            str(lx.get("summary") or ""),
            " ".join(lx.get("highlights") or []),
            str(lx.get("tips") or ""),
        ]
    )
    return any(m in text for m in _TEMPLATE_MARKERS)


def pick_lingxi(index: dict[str, list[dict[str, Any]]], name: str) -> dict[str, Any] | None:
    """仅精确（归一化后）匹配；不把「青城山－都江堰」强行拆成单景点以免搬错内容。"""
    key = _norm_name(name)
    candidates = list(index.get(key) or [])
    if not candidates:
        # 别名反查：灵犀 aliases 命中当前 name
        for items in index.values():
            for item in items:
                aliases = item.get("aliases") or []
                if any(_norm_name(a) == key for a in aliases):
                    candidates.append(item)
                if _norm_name(item.get("name") or "") == key:
                    candidates.append(item)
    if not candidates:
        return None
    # 同名多源时：优先 detail；其次非模板的 national-5a / province
    candidates.sort(
        key=lambda x: (
            0 if is_lingxi_template(x) else 1,
            _SOURCE_RANK.get(str(x.get("source") or ""), -1),
        ),
        reverse=True,
    )
    return candidates[0]


def content_from_lingxi(lx: dict[str, Any] | None) -> dict[str, Any]:
    empty = {"intro": None, "highlights": [], "tips": None}
    if not lx:
        return empty
    # 模板文案视为「没有可用内容」，留空（禁止把错位模板当真实介绍）
    if is_lingxi_template(lx):
        return empty
    intro = lx.get("intro") or lx.get("summary") or None
    # summary 可作 intro 兜底（灵犀 province 源常见只有 summary），仍属搬迁而非生成
    highlights = lx.get("highlights") or []
    if not isinstance(highlights, list):
        highlights = []
    tips = lx.get("tips") or None
    return {"intro": intro, "highlights": highlights, "tips": tips}


# ---------- 地图 POI（默认腾讯；高德作后备） ----------

def map_provider() -> str:
    """优先腾讯（TENCENT_MAP_KEY），否则高德。"""
    forced = (os.getenv("MAP_PROVIDER") or "").strip().lower()
    if forced in {"tencent", "amap"}:
        return forced
    if os.getenv("TENCENT_MAP_KEY", "").strip():
        return "tencent"
    if os.getenv("AMAP_API_KEY", "").strip():
        return "amap"
    return "tencent"


def _blank_to_none(value: Any) -> str | None:
    if value in (None, "", [], {}, "[]"):
        return None
    if isinstance(value, list):
        joined = "；".join(str(x).strip() for x in value if x not in (None, "", [], {}))
        return joined or None
    text = str(value).strip()
    return text or None


def _normalize_tencent_poi(item: dict[str, Any]) -> dict[str, Any]:
    """统一成脚本内部 POI 结构，便于 location/visit 解析。"""
    ad = item.get("ad_info") or {}
    loc = item.get("location") or {}
    lat, lng = loc.get("lat"), loc.get("lng")
    return {
        "id": item.get("id"),
        "name": item.get("title") or item.get("name"),
        "address": item.get("address"),
        "type": item.get("category"),
        "adcode": ad.get("adcode"),
        "pname": ad.get("province"),
        "cityname": ad.get("city"),
        "adname": ad.get("district"),
        "lat": lat,
        "lng": lng,
        "distance": item.get("distance") or item.get("_distance"),
        "biz_ext": {},
        "_provider": "tencent",
        "_raw": item,
    }


def _normalize_amap_poi(item: dict[str, Any]) -> dict[str, Any]:
    loc = (item.get("location") or "").split(",")
    lng = lat = None
    if len(loc) == 2:
        try:
            lng, lat = float(loc[0]), float(loc[1])
        except ValueError:
            pass
    return {
        "id": item.get("id"),
        "name": item.get("name"),
        "address": item.get("address"),
        "type": item.get("type"),
        "adcode": item.get("adcode"),
        "pname": item.get("pname"),
        "cityname": item.get("cityname"),
        "adname": item.get("adname"),
        "lat": lat,
        "lng": lng,
        "distance": item.get("distance"),
        "biz_ext": item.get("biz_ext") if isinstance(item.get("biz_ext"), dict) else {},
        "_provider": "amap",
        "_raw": item,
    }


def _poi_match_score(keyword: str, poi: dict[str, Any]) -> int:
    """给候选 POI 打分，避免「青海湖小区」「香港海洋公园学院」这类误匹配。"""
    title = str(poi.get("name") or "")
    category = str(poi.get("type") or "")
    address = str(poi.get("address") or "")
    kw = _norm_name(keyword)
    title_n = _norm_name(title)
    score = 0
    if category.startswith("房产小区") or "住宅区" in category or "商务楼宇" in category:
        return -100
    if "产业园区" in category and "旅游" not in category and "旅游" not in title:
        return -50
    # 精确同名优先于一切分类加权（否则「xxx学院」旅游景点会压过「xxx」本体）
    if title_n == kw:
        score += 200
    elif kw and title_n.startswith(kw):
        suffix = title_n[len(kw) :]
        if suffix in {"馆", "区", "景区", "公园"}:
            score += 160
        else:
            score += 40
            for bad in ("学院", "酒店", "宾馆", "地铁站", "公交站", "停车场", "入口", "万豪"):
                if bad in suffix or bad in title:
                    score -= 120
                    break
    elif kw and kw in title_n:
        score += 40
        for bad in ("学院", "酒店", "宾馆", "地铁站", "公交站", "停车场", "入口", "万豪"):
            if bad in title:
                score -= 120
                break
    if category.startswith("旅游景点") or "游乐场" in category or "主题乐园" in category:
        score += 50
    if "自然地名" in category or "湖泊" in category or "世界遗产" in category:
        score += 70
    if "国家级" in category or "国家级" in title:
        score += 20
    if title.endswith(("小区", "大厦", "花园", "家苑", "农场")):
        score -= 80
    if any(bad in address for bad in ("工业园", "同安路")) and "自然保护区" not in title:
        score -= 30
    return score


def _pick_best_pois(keyword: str, pois: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ranked = sorted(pois, key=lambda p: _poi_match_score(keyword, p), reverse=True)
    ranked = [p for p in ranked if _poi_match_score(keyword, p) > 0]
    return ranked


def _tencent_suggestion(
    client: httpx.Client, *, keyword: str, region: str, key: str, base: str
) -> list[dict[str, Any]]:
    """地名/湖泊等 spot search 常为空，用输入提示兜底。"""
    resp = client.get(
        f"{base}/ws/place/v1/suggestion",
        params={
            "key": key,
            "keyword": keyword,
            "region": region or "全国",
            "region_fix": 0,
            "policy": 1,
            "page_index": 1,
            "page_size": 10,
        },
        timeout=20.0,
    )
    resp.raise_for_status()
    payload = resp.json()
    if int(payload.get("status", -1)) != 0:
        return []
    out: list[dict[str, Any]] = []
    for item in payload.get("data") or []:
        # suggestion 的省市区在顶层，归一成 ad_info
        normalized = {
            **item,
            "ad_info": {
                "adcode": item.get("adcode"),
                "province": item.get("province"),
                "city": item.get("city"),
                "district": item.get("district"),
            },
        }
        out.append(_normalize_tencent_poi(normalized))
    return _pick_best_pois(keyword, out)


def tencent_place_search(
    client: httpx.Client,
    *,
    keyword: str,
    city: str = "",
    region: str = "",
    lat: float | None = None,
    lng: float | None = None,
    page_size: int = 10,
) -> list[dict[str, Any]]:
    key = os.getenv("TENCENT_MAP_KEY", "").strip()
    base = os.getenv("TENCENT_MAP_BASE_URL", "https://apis.map.qq.com").rstrip("/")
    path = os.getenv("TENCENT_PLACE_SEARCH_PATH", "/ws/place/v1/search")
    if not key:
        raise RuntimeError("TENCENT_MAP_KEY missing in .env")

    city_name = normalize_city(city) or (region or "").strip() or "全国"
    region_name = (region or "").strip() or city_name

    def _search(kw: str, boundary: str, extra: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "key": key,
            "keyword": kw,
            "boundary": boundary,
            "page_size": min(page_size, 20),
            "page_index": 1,
        }
        if extra:
            params.update(extra)
        resp = client.get(f"{base}{path}", params=params, timeout=20.0)
        resp.raise_for_status()
        payload = resp.json()
        if int(payload.get("status", -1)) != 0:
            print(f"  [tencent] fail {kw}: {payload.get('message')}")
            return []
        return [_normalize_tencent_poi(x) for x in (payload.get("data") or [])]

    if lat is not None and lng is not None:
        # 腾讯 nearby 半径上限 1000m；只作近距补充
        return _search(keyword, f"nearby({lat},{lng},1000,1)", {"filter": "category=旅游景点"})

    # 1) 城市限制 2) 加「景区」后缀 3) suggestion 兜底（并做景点相关性打分）
    candidates = [
        keyword,
        f"{keyword}景区" if not keyword.endswith(("景区", "公园", "寺", "湖")) else None,
    ]
    for kw in candidates:
        if not kw:
            continue
        for boundary in (f"region({city_name},1)", f"region({region_name},1)"):
            pois = _pick_best_pois(keyword, _search(kw, boundary))
            if pois:
                return pois

    suggested = _tencent_suggestion(
        client, keyword=keyword, region=region_name, key=key, base=base
    )
    if suggested:
        return suggested
    return []


def amap_place_search(
    client: httpx.Client,
    *,
    keyword: str,
    city: str = "",
    lat: float | None = None,
    lng: float | None = None,
) -> list[dict[str, Any]]:
    key = os.getenv("AMAP_API_KEY", "").strip()
    base = os.getenv("AMAP_BASE_URL", "https://restapi.amap.com").rstrip("/")
    if not key:
        return []
    if lat is not None and lng is not None:
        path = os.getenv("AMAP_PLACE_AROUND_PATH", "/v3/place/around")
        params = {
            "key": key,
            "location": f"{lng},{lat}",
            "types": "110000",
            "radius": 30000,
            "offset": 20,
            "page": 1,
            "extensions": "base",
            "output": "JSON",
            "city": normalize_city(city),
        }
    else:
        path = os.getenv("AMAP_PLACE_TEXT_PATH", "/v3/place/text")
        params = {
            "key": key,
            "keywords": keyword,
            "city": normalize_city(city),
            "citylimit": "true",
            "offset": 1,
            "page": 1,
            "extensions": os.getenv("AMAP_EXTENSIONS", "all"),
            "output": "JSON",
        }
    resp = client.get(f"{base}{path}", params=params, timeout=20.0)
    resp.raise_for_status()
    payload = resp.json()
    if str(payload.get("status")) != "1":
        print(f"  [amap] fail {keyword}: {payload.get('info')}")
        return []
    return [_normalize_amap_poi(x) for x in (payload.get("pois") or [])]


def map_place_text(
    client: httpx.Client, name: str, city: str, region: str = ""
) -> dict[str, Any] | None:
    provider = map_provider()
    if provider == "tencent":
        pois = tencent_place_search(client, keyword=name, city=city, region=region, page_size=5)
    else:
        pois = amap_place_search(client, keyword=name, city=city)
    return pois[0] if pois else None


def map_place_around(
    client: httpx.Client, lng: float, lat: float, city: str
) -> list[dict[str, Any]]:
    provider = map_provider()
    if provider == "tencent":
        return tencent_place_search(
            client,
            keyword="景点",
            city=city,
            lat=lat,
            lng=lng,
            page_size=20,
        )
    return amap_place_search(client, keyword="景点", city=city, lat=lat, lng=lng)


def parse_location(poi: dict[str, Any] | None) -> dict[str, Any]:
    empty = {"lat": None, "lng": None, "address": None, "geo_code": None}
    if not poi:
        return empty
    lat = poi.get("lat")
    lng = poi.get("lng")
    parts: list[str] = []
    for key in ("pname", "cityname", "adname", "address"):
        part = _blank_to_none(poi.get(key))
        if part and part not in parts:
            # address 已含省市区时避免重复拼接
            if key != "address" and poi.get("address") and part in str(poi.get("address")):
                continue
            parts.append(part)
    address = _blank_to_none(poi.get("address"))
    if parts and address and address not in "".join(parts):
        full = "".join(parts)
    else:
        full = address or ("".join(parts) if parts else None)
    geo = poi.get("adcode")
    return {
        "lat": float(lat) if lat is not None else None,
        "lng": float(lng) if lng is not None else None,
        "address": full,
        "geo_code": str(geo) if geo not in (None, "") else None,
    }


def parse_visit_info(poi: dict[str, Any] | None) -> dict[str, Any]:
    """腾讯地点搜索通常无营业时间/门票；高德则读 biz_ext。没有就空 / 以景区为准。"""
    opening = None
    adult_price = None
    is_free = None
    remark = "以景区为准"

    if poi:
        biz = poi.get("biz_ext") or {}
        raw = poi.get("_raw") or {}
        opening = (
            _blank_to_none(biz.get("opentime2"))
            or _blank_to_none(biz.get("open_time"))
            or _blank_to_none(raw.get("opentime2"))
            or _blank_to_none(raw.get("opentime"))
            or _blank_to_none(biz.get("opentime"))
        )
        cost = _blank_to_none(biz.get("cost")) or _blank_to_none(raw.get("cost"))
        if cost:
            if "免费" in cost or cost in {"0", "0.00", "0元"}:
                is_free = True
                adult_price = 0.0
                remark = cost
            else:
                m = re.search(r"(\d+(?:\.\d+)?)", cost)
                if m:
                    adult_price = float(m.group(1))
                    is_free = adult_price == 0
                    remark = cost
                else:
                    remark = cost

    return {
        "duration_hours": None,
        "opening_hours": opening,
        "ticket": {
            "is_free": is_free,
            "adult_price": adult_price,
            "currency": "CNY",
            "remark": remark,
        },
        "facilities": [],
        "transport": None,
    }


def _is_same_or_sub_spot(self_name: str, other_name: str) -> bool:
    a, b = _norm_name(self_name), _norm_name(other_name)
    if not a or not b:
        return True
    if a == b:
        return True
    # 子点 / 别名变体：都江堰 vs 都江堰景区；外滩 vs 外滩-信号台
    if a in b or b in a:
        return True
    return False


def nearby_from_map(
    self_name: str, pois: list[dict[str, Any]], *, min_distance_m: float = 300.0
) -> list[dict[str, Any]]:
    """过滤同名/子点/过近 POI。腾讯 nearby 半径小，阈值放宽到 300m。"""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for p in pois:
        name = (p.get("name") or "").strip()
        if not name or _is_same_or_sub_spot(self_name, name):
            continue
        key = _norm_name(name)
        if key in seen:
            continue
        dist_raw = p.get("distance")
        try:
            dist_m = float(dist_raw) if dist_raw not in (None, "", []) else None
        except ValueError:
            dist_m = None
        # 腾讯周边常不带 distance：保留有名字的候选，距离标 None
        if dist_m is not None and dist_m < min_distance_m:
            continue
        dist_km = round(dist_m / 1000.0, 1) if dist_m is not None else None
        drive = max(1, int(round(dist_km * 2.0))) if dist_km is not None else None
        seen.add(key)
        out.append({"name": name, "distance_km": dist_km, "drive_minutes": drive})
        if len(out) >= 3:
            break
    return out


def nearby_from_lingxi(lx: dict[str, Any] | None, self_name: str) -> list[dict[str, Any]]:
    if not lx:
        return []
    names = lx.get("nearby") or []
    out: list[dict[str, Any]] = []
    for n in names:
        if not n or _is_same_or_sub_spot(self_name, str(n)):
            continue
        # 排除过泛的城市名
        if str(n) in {"成都", "上海", "北京", "杭州", "重庆", "四川", "云南"}:
            continue
        out.append({"name": n, "distance_km": None, "drive_minutes": None})
    return out[:3]


# ---------- 千问：仅 tags ----------

TAG_CANDIDATES = [
    "亲子游",
    "适合老人",
    "历史人文",
    "自然风光",
    "避暑",
    "夜景",
    "拍照",
    "博物馆",
    "古镇",
    "主题乐园",
    "登山徒步",
    "宗教文化",
    "动物观赏",
    "城市地标",
]


def qwen_tags(client: httpx.Client, name: str, city: str, category: str | None) -> list[str]:
    api_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
    base = os.getenv("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
    model = os.getenv("QWEN_CHAT_MODEL", "qwen-flash")
    if not api_key:
        return []

    prompt = (
        f"景点：{name}；城市：{city}；类别：{category or '未知'}。\n"
        f"请从下列标签中选 2~5 个最贴切的，只返回 JSON 数组，不要其它文字：\n"
        f"{json.dumps(TAG_CANDIDATES, ensure_ascii=False)}"
    )
    resp = client.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": "你只做景点标签分类，禁止编造景点介绍。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    m = re.search(r"\[.*?\]", content, re.S)
    if not m:
        return []
    try:
        tags = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    return [t for t in tags if t in TAG_CANDIDATES][:5]


# ---------- 组装 ----------

def has_map_poi(rec: dict[str, Any]) -> bool:
    meta = rec.get("_meta") or {}
    if meta.get("map_poi_id") or meta.get("amap_poi_id"):
        return True
    return rec.get("location", {}).get("lat") is not None


def _is_empty_value(value: Any) -> bool:
    return value in (None, "", [], {})


def merge_preserve_visit_info(
    old: dict[str, Any] | None, new: dict[str, Any]
) -> dict[str, Any]:
    """
    重跑合并规则：
    - location（坐标/地址/geo_code）用新值（有则覆盖）
    - opening_hours / 门票：旧有新无时保留旧值（避免腾讯空字段盖掉高德结果）
    - duration_hours / facilities / transport：同样旧有新无则保留
    """
    if not old:
        return new

    old_visit = old.get("visit_info") or {}
    new_visit = dict(new.get("visit_info") or {})
    old_ticket = old_visit.get("ticket") or {}
    new_ticket = dict(new_visit.get("ticket") or {})

    # 营业时间：新为空则保留旧
    if _is_empty_value(new_visit.get("opening_hours")) and not _is_empty_value(
        old_visit.get("opening_hours")
    ):
        new_visit["opening_hours"] = old_visit.get("opening_hours")

    # 门票：新侧无有效价格/免费标记时，整段保留旧 ticket（含 remark）
    new_has_ticket = (
        new_ticket.get("adult_price") is not None or new_ticket.get("is_free") is not None
    )
    old_has_ticket = (
        old_ticket.get("adult_price") is not None or old_ticket.get("is_free") is not None
    )
    if not new_has_ticket and old_has_ticket:
        new_ticket = dict(old_ticket)
    else:
        # 部分字段：新空旧有则补
        for k in ("is_free", "adult_price", "currency", "remark"):
            if _is_empty_value(new_ticket.get(k)) and not _is_empty_value(old_ticket.get(k)):
                # remark 默认「以景区为准」不算有效旧值覆盖目标时：若旧 remark 更具体也保留
                if k == "remark" and new_ticket.get("remark") == "以景区为准":
                    if old_ticket.get("remark") and old_ticket.get("remark") != "以景区为准":
                        new_ticket[k] = old_ticket.get(k)
                    continue
                new_ticket[k] = old_ticket.get(k)
    new_visit["ticket"] = new_ticket

    for k in ("duration_hours", "transport"):
        if _is_empty_value(new_visit.get(k)) and not _is_empty_value(old_visit.get(k)):
            new_visit[k] = old_visit.get(k)
    if _is_empty_value(new_visit.get("facilities")) and not _is_empty_value(
        old_visit.get("facilities")
    ):
        new_visit["facilities"] = old_visit.get("facilities")

    new["visit_info"] = new_visit

    # location：新有坐标才覆盖；新坐标为空则保留旧 location
    new_loc = new.get("location") or {}
    old_loc = old.get("location") or {}
    if new_loc.get("lat") is None and old_loc.get("lat") is not None:
        new["location"] = old_loc
    else:
        # 地址/geo_code 新空旧有时补上
        merged_loc = dict(new_loc)
        for k in ("address", "geo_code"):
            if _is_empty_value(merged_loc.get(k)) and not _is_empty_value(old_loc.get(k)):
                merged_loc[k] = old_loc.get(k)
        new["location"] = merged_loc

    # _meta：保留曾用来源信息，便于追溯
    new_meta = dict(new.get("_meta") or {})
    old_meta = old.get("_meta") or {}
    if old_meta.get("amap_poi_id") and not new_meta.get("amap_poi_id"):
        new_meta["previous_amap_poi_id"] = old_meta.get("amap_poi_id")
        new_meta["previous_amap_poi_name"] = old_meta.get("amap_poi_name")
    if old_visit.get("opening_hours") and new_visit.get("opening_hours") == old_visit.get(
        "opening_hours"
    ):
        new_meta["opening_hours_preserved"] = True
    new["_meta"] = new_meta
    return new


def enrich_one(
    raw: dict[str, Any],
    seq: int,
    lingxi_index: dict[str, list[dict[str, Any]]],
    http: httpx.Client,
    *,
    skip_map: bool,
    skip_qwen: bool,
) -> dict[str, Any]:
    name = raw["name"]
    city = raw.get("city") or ""
    region = raw.get("region") or ""
    spot_id = make_spot_id(city, name, seq)

    lx = pick_lingxi(lingxi_index, name)
    template = is_lingxi_template(lx)
    content = content_from_lingxi(lx)
    aliases = list(lx.get("aliases") or []) if lx else []
    # 模板源的 category 常错位，不用；非模板才搬灵犀 category
    category = (lx.get("category") if lx and not template else None) or None

    poi = None
    if not skip_map:
        poi = map_place_text(http, name, city, region)
        time.sleep(0.25)  # 腾讯默认约 5 QPS，略留余量

    location = parse_location(poi)
    visit_info = parse_visit_info(poi)

    nearby: list[dict[str, Any]] = []
    if poi and location.get("lat") is not None and location.get("lng") is not None and not skip_map:
        around = map_place_around(http, location["lng"], location["lat"], city)
        time.sleep(0.25)
        nearby = nearby_from_map(name, around)
    if not nearby:
        nearby = nearby_from_lingxi(lx, name)

    # category 兜底：地图 type/category 最后一段
    if not category and poi:
        type_text = _blank_to_none(poi.get("type"))
        if type_text:
            category = type_text.split(":")[-1].split(";")[-1].strip() or type_text

    tags: list[str] = []
    if not skip_qwen:
        tags = qwen_tags(http, name, city, category)
        time.sleep(0.2)

    provider = (poi or {}).get("_provider") or map_provider()
    return {
        "id": spot_id,
        "name": name,
        "aliases": aliases,
        "city": city,
        "region": region,
        "category": category,
        "tags": tags,
        "location": location,
        "visit_info": visit_info,
        "content": content,
        "relations": {
            "nearby_spots": nearby,
            "recommended_routes": [],
        },
        "rag_links": {
            "guide_chunk_ids": [],
            "last_updated": date.today().isoformat(),
        },
        "_meta": {
            "lingxi_matched": bool(lx),
            "lingxi_name": (lx or {}).get("name"),
            "lingxi_source": (lx or {}).get("source"),
            "lingxi_template_skipped": template,
            "map_provider": provider,
            "map_poi_id": (poi or {}).get("id"),
            "map_poi_name": (poi or {}).get("name"),
            # 兼容旧字段
            "amap_poi_id": (poi or {}).get("id") if provider == "amap" else None,
            "amap_poi_name": (poi or {}).get("name") if provider == "amap" else None,
        },
    }


def record_key(item: dict[str, Any]) -> str:
    return f"{item.get('city') or ''}|{item.get('name') or ''}"


def select_items(
    items: list[dict[str, Any]],
    *,
    cities: str | None = None,
    regions: str | None = None,
) -> list[dict[str, Any]]:
    """按 region / city 过滤。regions 与 cities 可同时用（取交集）。"""
    selected = list(items)

    if regions and regions.strip() and regions.strip().lower() != "all":
        keys = [k.strip() for k in regions.split(",") if k.strip()]
        selected = [x for x in selected if any(k == (x.get("region") or "") for k in keys)]

    if cities and cities.strip() and cities.strip().lower() != "all":
        keys = [k.strip() for k in cities.split(",") if k.strip()]
        selected = [x for x in selected if any(k in (x.get("city") or "") for k in keys)]

    return selected


def save_enriched(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Enrich attractions from name/city list")
    parser.add_argument(
        "--regions",
        default="",
        help="按 region 过滤，逗号分隔，建议每次 2 个，如 上海,江苏",
    )
    parser.add_argument(
        "--cities",
        default="",
        help="按城市过滤（可选）：逗号分隔，如 成都,上海；与 --regions 可同时用",
    )
    parser.add_argument("--dry-run", action="store_true", help="只列出名单，不调 API、不写文件")
    parser.add_argument(
        "--skip-map",
        "--skip-amap",
        dest="skip_map",
        action="store_true",
        help="跳过地图 POI（腾讯/高德）",
    )
    parser.add_argument("--skip-qwen", action="store_true", help="跳过千问 tags")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="若输出文件已存在，跳过已有 name+city，追加新结果",
    )
    parser.add_argument(
        "--refresh-missing",
        action="store_true",
        help="重跑已有结果中缺地图 POI / 无坐标的条目，并原地替换",
    )
    parser.add_argument(
        "--checkpoint-every",
        type=int,
        default=10,
        help="每处理 N 条落盘一次（默认 10）",
    )
    parser.add_argument("--limit", type=int, default=0, help="最多处理 N 条（0=不限制）")
    parser.add_argument("--out", type=Path, default=OUT_PATH, help="输出路径")
    args = parser.parse_args()

    if not args.regions and not args.cities:
        print("请指定 --regions（推荐每次 2 个）或 --cities，避免一次全量。")
        print("示例: --regions 上海,江苏 --resume")
        return 2

    load_env()
    print(f"map provider: {map_provider()}")
    raw_items = json.loads(SRC_PATH.read_text(encoding="utf-8"))
    selected = select_items(raw_items, cities=args.cities or None, regions=args.regions or None)
    if args.limit and args.limit > 0:
        selected = selected[: args.limit]
    print(
        f"selected: {len(selected)} "
        f"(regions={args.regions or '-'}, cities={args.cities or '-'})"
    )

    if args.dry_run:
        for i, item in enumerate(selected, 1):
            print(f"  {i:03d}. [{item.get('region')}] {item.get('city')} | {item.get('name')}")
        print("dry-run done; no files written.")
        return 0

    lingxi = json.loads(LINGXI_PATH.read_text(encoding="utf-8"))
    lingxi_index = build_lingxi_index(lingxi)

    enriched: list[dict[str, Any]] = []
    done_keys: set[str] = set()
    counters: dict[str, int] = {}
    index_by_key: dict[str, int] = {}

    if args.out.exists() and (args.resume or args.refresh_missing):
        try:
            enriched = json.loads(args.out.read_text(encoding="utf-8"))
            if not isinstance(enriched, list):
                enriched = []
        except json.JSONDecodeError:
            enriched = []
        for i, rec in enumerate(enriched):
            key = record_key(rec)
            done_keys.add(key)
            index_by_key[key] = i
            city_key = normalize_city(rec.get("city") or "") or "unk"
            counters[city_key] = counters.get(city_key, 0) + 1
        print(f"loaded existing: {len(enriched)} records")

    if args.refresh_missing:
        missing_keys = {record_key(rec) for rec in enriched if not has_map_poi(rec)}
        todo = [x for x in selected if record_key(x) in missing_keys]
        print(f"refresh-missing: {len(todo)}")
    elif args.resume:
        todo = [x for x in selected if record_key(x) not in done_keys]
        print(f"to enrich (new only): {len(todo)}")
    else:
        # 非 resume：只处理 selected，结果与文件中其他 region 合并
        if args.out.exists() and not enriched:
            try:
                enriched = json.loads(args.out.read_text(encoding="utf-8"))
                if not isinstance(enriched, list):
                    enriched = []
            except json.JSONDecodeError:
                enriched = []
            for i, rec in enumerate(enriched):
                index_by_key[record_key(rec)] = i
                city_key = normalize_city(rec.get("city") or "") or "unk"
                counters[city_key] = counters.get(city_key, 0) + 1
        todo = list(selected)
        print(f"to enrich (replace selected): {len(todo)}")

    if not todo:
        print("nothing to do.")
        return 0

    with httpx.Client() as http:
        for i, raw in enumerate(todo, 1):
            city_key = normalize_city(raw.get("city") or "") or "unk"
            key = record_key(raw)
            if key in index_by_key:
                # 重跑时沿用原序号（从已有 id 解析失败则按城市计数）
                seq = counters.get(city_key, 0) or 1
                old = enriched[index_by_key[key]]
                old_id = str(old.get("id") or "")
                m = re.search(r"_(\d+)$", old_id)
                if m:
                    seq = int(m.group(1))
            else:
                counters[city_key] = counters.get(city_key, 0) + 1
                seq = counters[city_key]

            print(f"[{i}/{len(todo)}] [{raw.get('region')}] {raw.get('city')} · {raw.get('name')}")
            try:
                record = enrich_one(
                    raw,
                    seq,
                    lingxi_index,
                    http,
                    skip_map=args.skip_map,
                    skip_qwen=args.skip_qwen,
                )
            except Exception as exc:  # noqa: BLE001 - 单条失败不中断批次
                print(f"  ! failed: {exc}")
                record = {
                    "id": make_spot_id(raw.get("city") or "", raw.get("name") or "", seq),
                    "name": raw.get("name"),
                    "aliases": [],
                    "city": raw.get("city"),
                    "region": raw.get("region"),
                    "category": None,
                    "tags": [],
                    "location": {"lat": None, "lng": None, "address": None, "geo_code": None},
                    "visit_info": parse_visit_info(None),
                    "content": {"intro": None, "highlights": [], "tips": None},
                    "relations": {"nearby_spots": [], "recommended_routes": []},
                    "rag_links": {"guide_chunk_ids": [], "last_updated": date.today().isoformat()},
                    "_meta": {"error": str(exc)},
                }

            if key in index_by_key:
                old_rec = enriched[index_by_key[key]]
                enriched[index_by_key[key]] = merge_preserve_visit_info(old_rec, record)
            else:
                index_by_key[key] = len(enriched)
                enriched.append(record)

            if args.checkpoint_every > 0 and i % args.checkpoint_every == 0:
                save_enriched(args.out, enriched)
                print(f"  checkpoint -> {args.out} ({len(enriched)} records)")

    save_enriched(args.out, enriched)
    print(f"wrote {len(enriched)} records -> {args.out}")
    matched = sum(1 for r in enriched if r.get("_meta", {}).get("lingxi_matched"))
    open_ok = sum(1 for r in enriched if r.get("visit_info", {}).get("opening_hours"))
    missing = sum(1 for r in enriched if not has_map_poi(r))
    print(
        f"lingxi matched: {matched}/{len(enriched)}; "
        f"opening_hours: {open_ok}; missing_map: {missing}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

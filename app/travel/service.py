from __future__ import annotations

import inspect
import json
import logging
import random
import re
from collections.abc import Awaitable, Callable
from typing import Any

from app.config import get_settings
from app.intent_guards import is_identity_or_capability_query
from app.llm.qwen import QwenClient, QwenError
from app.memory.context import build_llm_context_text
from app.models import Entity, Source, SubgraphResult
from app.reply_templates import build_travel_short_fact_draft, is_travel_short_fact
from app.render.markdown_text import (
    extract_markdown_images,
    markdown_to_plain,
    strip_markdown_images,
)
from app.tools.amap import amap_poi_snapshot_by_name, photos_to_markdown
from app.tools.city_info import get_city_info
from app.tools.weather import WEATHER_QUERY_TOOL, WEATHER_UNAVAILABLE_REPLY, dispatch_weather_tool
from app.travel.itinerary import (
    format_tool_trace_human,
    plan_multi_day_itinerary,
    plan_structured_itinerary,
)
from app.travel.knowledge import KnowledgeSource, search_attractions, search_regions
from app.travel.photo_policy import (
    MAX_PHOTO_ROUNDS_PER_SPOT,
    attraction_spot_names,
    context_bits_from_slots,
    enrich_query_with_context,
    gate_photos_by_cooldown,
    merge_photos_shown_patch,
    next_photo_turn_index,
    normalize_photos_shown,
    record_spot_photos,
    spot_photo_budget,
    stamp_photos_last_turn,
    wants_attraction_photos,
)
from app.travel.query_intent import TravelIntent, parse_travel_intent

logger = logging.getLogger(__name__)

TokenCallback = Callable[[str], Awaitable[None] | None]

# 旅游支线独立收尾：灵活口语，不整段粘贴检索卡；图片由接口 images 字段展示
TRAVEL_REPLY_SYSTEM = """【角色】
你是航途小程序出行助手「途途」，本轮负责旅游咨询：景点推荐、行程规划、交通衔接与玩法建议（语气轻松，勿整段粘贴资料）。

【首要任务】
你的首要任务是回答最新提问（见「=== 用户最新提问 ===」之后），禁止重提历史已答问题；会话摘要与最近对话仅用于理解指代。

【天气工具 weather_query】
1. 按需调用：只有当用户明确问天气，或询问爬山/户外是否合适、穿搭建议、带伞等受天气影响的问题时，才调用 weather_query。
2. 强事实不查：问票价、开放时间、怎么走、历史介绍，绝对不准调天气工具，以节省响应时间。
3. 无城市不查：如果用户没提城市，工具参数禁止瞎填「北京」或其它默认城；必须先反问用户「您想查哪个城市的天气？」。
4. 超时降级：如果天气 API 挂了或工具返回失败，统一回复「暂未获取到实时天气，建议出行前关注天气预报」。

【自我介绍】
当用户打招呼、问「你是谁/你能做什么」、或尚未进入具体问题时，先亲切亮明身份，可参考（勿死记硬背）：
「你好，我是途途～主要帮你做旅游方面的咨询，比如去哪玩、怎么安排行程、当地吃住行小贴士。机场进出港、值机设施这类也可以问我，我会按机场指引来答。」
用户已在问具体景点/行程时，直接作答，不要每轮都自我介绍。

【任务】
依据资料要点与用户问题，用自己的话直观回答。内容要充实一点，但勿注水；仅当用户明确要行程/攻略/N日游时才用日程排版。

【文风】
1. 像懂行的朋友聊天，语义连贯、语气自然；先答问题，再把亮点、怎么玩、吃的/贴士写丰满。
2. 信息嵌进句子，不要资料卡堆砌。地址、开放时间、票价、建议游玩时长等自然带过，禁止「📍地址…开放时间…记得预留…」标签式连贴。
3. 适当分段：默认 2～4 个短段落，段与段空一行。常见结构可参考——首段点题推荐；次段看点/玩法与实用信息；再有余力写美食或小贴士。过渡要顺，勿生硬跳切。
4. 写丰富，不写空：多用要点里的具体看点、当地特色、注意事项；少「超震撼」「看懂更过瘾」等空泛夸饰。
5. 适量 emoji（🗺️🍜🍵☀️🌙），点缀即可，勿句句堆、勿当段落标题滥用。

【规则】
1. 只用要点信息转述，禁止复述成资料卡；禁内部标签；行首不要加「-」。
2. 图文混排：资料里若有 `![](URL)`，必须保留该语法，紧跟在对应景点文字旁；禁止把全部图片挪到文末，禁止改 URL。
3. 城市锁定：标题与正文须与用户目的地一致，禁止换成其它城市。
4. 票价、开放时间、地址以要点为准，没有则写以现场/官网为准。
5. 默认（推荐值不值、介绍、门票、怎么去、好不好玩等）：直接口语分段回答，禁止写成「1日游/N日游/Day」模板；单点答疑也可 2 段左右写清楚。
6. 仅当用户明确提到行程/攻略/怎么安排/N日游/几日游时，才用日程排版：
   - 总标题只写一次（首行）：可选 emoji +【目的地+天数】，例如「🗺️【北京三天两晚】」；后面各天禁止再重复该总标题
   - 每天一行小标题：「第N天 景点A → 景点B → 景点C」（用当天核心点串联，醒目；不要写成「第N天」后重贴总标题）
   - 每天内必须按行写出「上午：」「下午：」「晚上：」三段（可含约Xh）；景点仅用同城候选，勿编外地
   - 有行程草稿则改写为上述结构，勿堆工具日志/坐标；日程正文口语连贯，勿干巴清单
   - 提到某景点时，把该景点的 `![](URL)` 紧跟该时段段落后，不要堆到全文最后

【输出】
纯中文、多短段落（段间空行），可含行内图片 Markdown；仅明确行程题才用「总标题一次 + 第N天 景点→景点 + 上午/下午/晚上」。不要输出 chunk_id / 检索字段。
"""

# 「亲子游」作标签检索，不单独触发攻略；多日/安排类进入行程工具
_GUIDE_HINT = re.compile(
    r"攻略|怎么玩|一日游|两日游|三日游|五日游|多日游|行程|路线|安排|规划|"
    r"怎么安排|排行程|怎么走|环线|跨城|自由行"
)
_TRANSPORT_HINT = re.compile(r"怎么去|如何到达|交通|怎么走")


def _resolve_source(source: KnowledgeSource | None) -> KnowledgeSource:
    if source in {"legacy", "enriched"}:
        return source
    configured = (get_settings().travel_knowledge_source or "legacy").strip().lower()
    return "enriched" if configured == "enriched" else "legacy"


def _intro_of(item: dict[str, Any]) -> str:
    content = item.get("content") if isinstance(item.get("content"), dict) else {}
    return str(item.get("summary") or item.get("intro") or content.get("intro") or "")


def _tips_of(item: dict[str, Any]) -> str:
    content = item.get("content") if isinstance(item.get("content"), dict) else {}
    return str(item.get("tips") or content.get("tips") or "")


def _opening_of(item: dict[str, Any]) -> str:
    visit = item.get("visit_info") if isinstance(item.get("visit_info"), dict) else {}
    return str(visit.get("opening_hours") or "")


def _ticket_of(item: dict[str, Any]) -> str:
    visit = item.get("visit_info") if isinstance(item.get("visit_info"), dict) else {}
    ticket = visit.get("ticket") if isinstance(visit.get("ticket"), dict) else {}
    price = item.get("adult_price")
    if price is None:
        price = ticket.get("adult_price")
    if price is not None:
        currency = ticket.get("currency") or "CNY"
        return f"{price} {currency}"
    remark = ticket.get("remark")
    return str(remark) if remark else ""


def _address_of(item: dict[str, Any]) -> str:
    location = item.get("location") if isinstance(item.get("location"), dict) else {}
    return str(location.get("address") or "")


def _transport_of(item: dict[str, Any]) -> str:
    visit = item.get("visit_info") if isinstance(item.get("visit_info"), dict) else {}
    t = visit.get("transport")
    if isinstance(t, str) and t.strip():
        return t.strip()
    if isinstance(t, dict):
        bits = [str(v).strip() for v in t.values() if v]
        return "；".join(bits) if bits else ""
    return ""


def _region_foods_for_item(item: dict[str, Any]) -> list[str]:
    for place in (
        item.get("region"),
        item.get("city_norm"),
        item.get("city"),
    ):
        if not place:
            continue
        info = get_city_info(str(place))
        if info.get("ok") and info.get("foods"):
            return [str(x) for x in info["foods"] if x][:6]
    return []


def _is_spot_detail(intent: TravelIntent, *, guide_mode: bool) -> bool:
    """景点名命中且非攻略/多日列表 → 景区详情卡（与省市卡同级展示）。"""
    if not (intent.name_query or "").strip():
        return False
    if guide_mode or intent.trip_mode == "multi_day":
        return False
    return True


def _nearby_of(item: dict[str, Any]) -> list[str]:
    relations = item.get("relations") if isinstance(item.get("relations"), dict) else {}
    spots = relations.get("nearby_spots") or []
    names: list[str] = []
    for spot in spots[:3]:
        if isinstance(spot, dict) and spot.get("name"):
            names.append(str(spot["name"]))
        elif isinstance(spot, str):
            names.append(spot)
    return names


def _photos_of(item: dict[str, Any], *, limit: int = 2) -> list[dict[str, str]]:
    media = item.get("media") if isinstance(item.get("media"), dict) else {}
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    cover = media.get("cover")
    if isinstance(cover, str) and cover.strip():
        url = cover.strip()
        seen.add(url)
        out.append({"name": str(item.get("name") or ""), "url": url, "role": "cover"})
    for ph in media.get("photos") or []:
        if len(out) >= limit:
            break
        url = ph.get("url") if isinstance(ph, dict) else ph if isinstance(ph, str) else None
        if not url or url in seen:
            continue
        seen.add(url)
        out.append({"name": str(item.get("name") or ""), "url": str(url), "role": "gallery"})
    return out[:limit]


def _photo_pool(attractions: list[dict[str, Any]], *, per_item: int = 6) -> list[dict[str, str]]:
    """跨命中按 URL 去重后的图池。"""
    images: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in attractions:
        for photo in _photos_of(item, limit=per_item):
            url = str(photo.get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            images.append(photo)
    return images


def sample_travel_images(
    attractions: list[dict[str, Any]],
    *,
    min_n: int = 1,
    max_n: int = 3,
) -> list[dict[str, str]]:
    """普通推荐：随机 1～3 张，底部展示（placement=footer）。"""
    pool = _photo_pool(attractions)
    if not pool:
        return []
    hi = min(max(1, max_n), len(pool))
    lo = min(max(1, min_n), hi)
    k = random.randint(lo, hi)
    out: list[dict[str, str]] = []
    for photo in random.sample(pool, k):
        row = dict(photo)
        row["placement"] = "footer"
        out.append(row)
    return out


# 允许「🗺️【北京三天两晚】第1天 …」这类前缀，只要行内出现第N天/DayN
_DAY_SPLIT = re.compile(
    r"(?:^|\n)[^\n]*?(?:Day\s*(\d+)|【第\s*(\d+)\s*天】|第\s*(\d+)\s*天)(?!\s*[·・.])",
    re.I,
)
_SLOT_SPLIT = re.compile(r"(?:^|\n)\s*(?:[-•*]?\s*)?(上午|下午|晚上)\s*[:：]?")
_EVENING_IMG_HINT = re.compile(r"早中晚|含晚上|晚上也|晚上安排|夜间|夜游|夜景安排")
_NAME_SUFFIXES = ("博物院", "博物馆", "遗址公园", "风景区", "旅游区", "文化区", "公园", "景区", "大街", "步行街")


def _parse_itinerary_sections(text: str) -> list[dict[str, str]]:
    """解析行程正文为时段块：[{slot, text, start, end}]，slot 形如「第1天·上午」。"""
    body = text or ""
    if not body.strip():
        return []
    day_parts: list[tuple[str, int, int]] = []
    matches = list(_DAY_SPLIT.finditer(body))
    if matches:
        for i, m in enumerate(matches):
            day_no = m.group(1) or m.group(2) or m.group(3) or "1"
            # 从「第N天」本身起算，保留当天标题行
            day_token = m.start(1) if m.group(1) is not None else (
                m.start(2) if m.group(2) is not None else m.start(3)
            )
            # 回退到该行行首，便于保留「第N天 A→B→C」
            line_start = body.rfind("\n", 0, day_token) + 1
            start = line_start
            end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
            # 下一天也对齐到其行首
            if i + 1 < len(matches):
                nxt = matches[i + 1]
                nxt_token = nxt.start(1) if nxt.group(1) is not None else (
                    nxt.start(2) if nxt.group(2) is not None else nxt.start(3)
                )
                end = body.rfind("\n", 0, nxt_token) + 1
                if end <= start:
                    end = matches[i + 1].start()
            day_parts.append((f"第{day_no}天", start, end))
    else:
        day_parts.append(("", 0, len(body)))

    sections: list[dict[str, Any]] = []
    for day_label, d0, d1 in day_parts:
        chunk = body[d0:d1]
        slot_ms = list(_SLOT_SPLIT.finditer(chunk))
        if not slot_ms:
            if chunk.strip():
                sections.append(
                    {"slot": day_label or "行程", "text": chunk, "start": d0, "end": d1}
                )
            continue
        # 时段前的标题行
        head_end = slot_ms[0].start()
        if head_end > 0 and chunk[:head_end].strip():
            sections.append(
                {
                    "slot": f"{day_label}·标题" if day_label else "标题",
                    "text": chunk[:head_end],
                    "start": d0,
                    "end": d0 + head_end,
                }
            )
        for i, m in enumerate(slot_ms):
            period = m.group(1)
            start = m.start()
            end = slot_ms[i + 1].start() if i + 1 < len(slot_ms) else len(chunk)
            slot = f"{day_label}·{period}" if day_label else period
            sections.append(
                {
                    "slot": slot,
                    "text": chunk[start:end],
                    "start": d0 + start,
                    "end": d0 + end,
                }
            )
    return sections


def _attractions_by_name(attractions: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    rows: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for item in attractions:
        name = str(item.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        rows.append((name, item))
    rows.sort(key=lambda x: len(x[0]), reverse=True)
    return rows


def _spots_mentioned_in_text(
    text: str, attractions: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    return _spots_mentioned_fuzzy(text, attractions)


def _take_photos_from_items(
    items: list[dict[str, Any]],
    *,
    need: int,
    seen: set[str],
) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for item in items:
        if len(out) >= need:
            break
        for photo in _photos_of(item, limit=6):
            if len(out) >= need:
                break
            url = str(photo.get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            out.append(dict(photo))
    return out


def pick_itinerary_images(
    answer: str,
    attractions: list[dict[str, Any]],
    *,
    query: str = "",
    per_slot_min: int = 2,
    per_slot_max: int = 3,
) -> list[dict[str, str]]:
    """行程类：按时段匹配正文景点取图 2～3 张；默认仅上午/下午插图。"""
    sections = _parse_itinerary_sections(answer)
    if not sections:
        return sample_travel_images(attractions, min_n=1, max_n=3)

    include_evening = bool(_EVENING_IMG_HINT.search(query or ""))
    pool = _photo_pool(attractions)
    if not pool:
        return []

    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for sec in sections:
        slot = sec["slot"]
        if ("晚上" in slot) and not include_evening:
            continue
        # 跳过无时段的整天兜底块（除非完全解析不到上午/下午）
        if "·" not in slot and any("·" in s["slot"] for s in sections):
            continue
        chunk = sec.get("text") or ""
        mentioned = _spots_mentioned_in_text(chunk, attractions)
        # 该时段正文未命中候选景点名 → 不配图，避免错配
        if not mentioned:
            continue
        need = random.randint(per_slot_min, per_slot_max)
        picked = _take_photos_from_items(mentioned, need=need, seen=seen)
        if len(picked) < per_slot_min:
            day_prefix = slot.split("·", 1)[0] if "·" in slot else ""
            day_text = " ".join(
                s.get("text") or "" for s in sections if s["slot"].startswith(day_prefix)
            )
            day_spots = _spots_mentioned_in_text(day_text, attractions)
            picked.extend(
                _take_photos_from_items(day_spots, need=per_slot_min - len(picked), seen=seen)
            )
        for photo in picked[:need]:
            row = dict(photo)
            row["slot"] = slot
            row["placement"] = "inline"
            out.append(row)
    return out or sample_travel_images(attractions, min_n=1, max_n=3)


def collect_travel_images(attractions: list[dict[str, Any]], query: str) -> list[dict[str, str]]:
    """兼容旧调用：非行程默认随机 1～3 张。"""
    _ = query
    return sample_travel_images(attractions, min_n=1, max_n=3)


def select_reply_images(
    *,
    query: str,
    answer: str,
    attractions: list[dict[str, Any]],
    has_itinerary_draft: bool = False,
) -> list[dict[str, str]]:
    """按题型选图：普通底部 1～3；行程按上午/下午（可选晚上）与景点匹配穿插。"""
    guide_like = bool(_GUIDE_HINT.search(query or "")) or has_itinerary_draft
    looks_itinerary = bool(
        re.search(r"(?:Day\s*\d+|第\s*\d+\s*天|上午\s*[:：]|下午\s*[:：]|晚上\s*[:：])", answer or "", re.I)
    )
    if guide_like or looks_itinerary:
        return pick_itinerary_images(
            answer, attractions, query=query, per_slot_min=2, per_slot_max=3
        )
    return sample_travel_images(attractions, min_n=1, max_n=3)


def prefetch_amap_snapshots_parallel(
    attractions: list[dict[str, Any]],
    *,
    max_workers: int = 3,
) -> None:
    """多景点高德 POI 并发预热（走 amap 缓存，供后续 _spot_photo_urls 命中）。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    jobs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in attractions or []:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        city = str(item.get("city_norm") or item.get("city") or "").strip()
        key = (name, city)
        if key in seen:
            continue
        seen.add(key)
        jobs.append(key)
    if not jobs:
        return
    workers = max(1, min(int(max_workers), len(jobs), 4))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(amap_poi_snapshot_by_name, name=n, city=c) for n, c in jobs]
        for fut in as_completed(futs):
            try:
                fut.result()
            except Exception:
                pass


def _spot_photo_urls(
    item: dict[str, Any],
    *,
    limit: int = 3,
    exclude: set[str] | None = None,
    allow_amap: bool = True,
) -> list[str]:
    """优先高德 photos，缺则用库内 media；可排除本会话已用过的 URL。"""
    ban = {u for u in (exclude or set()) if u}
    name = str(item.get("name") or "").strip()
    city = str(item.get("city_norm") or item.get("city") or "").strip()
    if allow_amap and name:
        try:
            snap = amap_poi_snapshot_by_name(name=name, city=city)
            urls = [
                str(u).strip()
                for u in (snap.get("photos") or [])
                if str(u).strip() and str(u).strip() not in ban
            ]
            if urls:
                return urls[:limit]
        except Exception:
            pass
    media = item.get("media") if isinstance(item.get("media"), dict) else {}
    out: list[str] = []
    seen: set[str] = set(ban)
    cover = media.get("cover")
    if isinstance(cover, str) and cover.strip():
        u = cover.strip()
        if u not in seen:
            seen.add(u)
            out.append(u)
    for ph in media.get("photos") or []:
        if len(out) >= limit:
            break
        url = ph.get("url") if isinstance(ph, dict) else ph if isinstance(ph, str) else None
        if not url:
            continue
        u = str(url).strip()
        if not u or u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out[:limit]


def _spot_photos_md(
    item: dict[str, Any],
    *,
    limit: int = 3,
    exclude: set[str] | None = None,
    allow_amap: bool = True,
) -> str:
    name = str(item.get("name") or "").strip() or "景点"
    urls = _spot_photo_urls(item, limit=limit, exclude=exclude, allow_amap=allow_amap)
    return photos_to_markdown(name, urls, limit=limit)


def mix_spots_text_with_photos(attractions: list[dict[str, Any]], *, per_spot: int = 3) -> str:
    """按景点名排序，检索简介与 ![名](URL) 图文混排。"""
    items = [a for a in attractions if str(a.get("name") or "").strip()]
    items.sort(key=lambda a: str(a.get("name") or ""))
    prefetch_amap_snapshots_parallel(items, max_workers=3)
    blocks: list[str] = []
    for item in items:
        name = str(item.get("name") or "").strip()
        intro = (_intro_of(item) or "").strip()
        opening = (_opening_of(item) or "").strip()
        lines = [name]
        if intro:
            lines.append(intro[:200])
        if opening:
            lines.append(f"开放：{opening[:80]}")
        photo_md = _spot_photos_md(item, limit=per_spot)
        if photo_md:
            lines.append(photo_md)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _spot_aliases(name: str) -> list[str]:
    """用于正文匹配的别名（故宫博物院 → 故宫 等）。"""
    n = (name or "").strip()
    if not n:
        return []
    aliases = [n]
    for suf in _NAME_SUFFIXES:
        if n.endswith(suf) and len(n) > len(suf) + 1:
            aliases.append(n[: -len(suf)])
    # 去重，长名优先
    seen: set[str] = set()
    out: list[str] = []
    for a in sorted(aliases, key=len, reverse=True):
        if a and a not in seen:
            seen.add(a)
            out.append(a)
    return out


def _find_alias_in_text(text: str, name: str) -> tuple[int, str] | None:
    for alias in _spot_aliases(name):
        idx = text.find(alias)
        if idx >= 0:
            return idx, alias
    return None


def _spots_mentioned_fuzzy(text: str, attractions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    body = text or ""
    hit: list[dict[str, Any]] = []
    used: set[str] = set()
    named = sorted(
        ((str(a.get("name") or "").strip(), a) for a in attractions if str(a.get("name") or "").strip()),
        key=lambda x: len(x[0]),
        reverse=True,
    )
    for name, item in named:
        if name in used:
            continue
        found = _find_alias_in_text(body, name)
        if found:
            used.add(name)
            hit.append(item)
    return hit


def _prefer_attractions_for_query(
    query: str,
    attractions: list[dict[str, Any]],
    intent: TravelIntent | None = None,
    *,
    source: KnowledgeSource | str | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """
    本轮已点名景点/城市时，检索结果优先落在该主题上。
    避免「上海迪士尼」会话里问西湖，仍用外滩/陆家嘴图冒充西湖。
    """
    intent = intent or parse_travel_intent(query)
    needles = [
        str(p).strip()
        for p in (intent.name_query, intent.name_norm, intent.city_norm)
        if str(p or "").strip()
    ]
    if not needles:
        return list(attractions or [])

    def _match(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        hit: list[dict[str, Any]] = []
        for a in items or []:
            name = str(a.get("name") or "")
            city = str(a.get("city_norm") or a.get("city") or "")
            blob = f"{name} {city}"
            if any(n in blob for n in needles if len(n) >= 2):
                hit.append(a)
        return hit

    matched = _match(list(attractions or []))
    if matched:
        return matched

    # 旧上下文污染检索时：用本轮原句 / 景点名重搜
    retry_q = (intent.name_query or intent.city_norm or query or "").strip()
    if not retry_q:
        return list(attractions or [])
    kb = _resolve_source(source)
    retried = search_attractions(retry_q, limit=max(3, int(limit) or 3), source=kb)
    matched2 = _match(retried)
    return matched2 or retried or list(attractions or [])


def weave_photos_by_spot_name(
    text: str,
    attractions: list[dict[str, Any]],
    *,
    per_spot: int = 2,
    photos_shown: dict[str, dict[str, Any]] | None = None,
    allow_amap: bool = True,
    turn_index: int | None = None,
) -> tuple[str, dict[str, dict[str, Any]]]:
    """
    图文混排：行程按时段段末插图；否则按景点名出现处插图。
    返回 (正文, 更新后的 photos_shown)。
    会话约束：同景点最多 MAX_PHOTO_ROUNDS_PER_SPOT 轮；新一轮须有未用过 URL，否则该景点跳过。
    """
    body = text or ""
    shown = normalize_photos_shown(photos_shown)
    if not body.strip() or not attractions:
        return body, shown

    used_urls_global: set[str] = set()
    inserted_spots: set[str] = set()

    def _pick_urls(item: dict[str, Any]) -> list[str]:
        nonlocal shown
        name = str(item.get("name") or "").strip()
        if name and name in inserted_spots:
            return []
        ok, used = spot_photo_budget(name, shown, max_rounds=MAX_PHOTO_ROUNDS_PER_SPOT)
        if not ok:
            return []
        ban = set(used) | used_urls_global
        urls = _spot_photo_urls(
            item, limit=per_spot, exclude=ban, allow_amap=allow_amap
        )
        # 没有新图 → 本轮该景点不出图（避免两次一样）
        if not urls:
            return []
        # URL 已在正文（含 ![](url)）则跳过
        urls = [u for u in urls if u not in body]
        if not urls:
            return []
        for u in urls:
            used_urls_global.add(u)
        shown = record_spot_photos(
            shown, name=name, urls=urls, turn_index=turn_index
        )
        if name:
            inserted_spots.add(name)
        return urls

    sections = _parse_itinerary_sections(body)
    period_secs = [s for s in sections if "·" in s["slot"] and not s["slot"].endswith("·标题")]
    if period_secs:
        inserts: list[tuple[int, str]] = []
        for sec in period_secs:
            if "晚上" in sec["slot"]:
                continue  # 默认晚上不配图
            chunk = sec.get("text") or ""
            if "![" in chunk and "](" in chunk:
                continue
            mentioned = _spots_mentioned_fuzzy(chunk, attractions)
            if not mentioned:
                continue
            md_parts: list[str] = []
            for item in mentioned[:3]:
                name = str(item.get("name") or "").strip()
                if name and name in inserted_spots:
                    continue
                urls = _pick_urls(item)
                if not urls:
                    continue
                md = photos_to_markdown(name, urls, limit=per_spot)
                if md:
                    md_parts.append(md)
            if not md_parts:
                continue
            end = int(sec["end"])
            inserts.append((end, "\n" + "\n".join(md_parts) + "\n"))
        for pos, chunk in sorted(inserts, key=lambda x: x[0], reverse=True):
            body = body[:pos] + chunk + body[pos:]
        return body, shown

    named = sorted(
        ((str(a.get("name") or "").strip(), a) for a in attractions if str(a.get("name") or "").strip()),
        key=lambda x: len(x[0]),
        reverse=True,
    )
    inserts2: list[tuple[int, str]] = []
    occupied: list[tuple[int, int]] = []

    def _overlaps(start: int, end: int) -> bool:
        return any(start < b and end > a for a, b in occupied)

    for name, item in named:
        if name in inserted_spots:
            continue
        found = _find_alias_in_text(body, name)
        if not found:
            continue
        idx, alias = found
        end = idx + len(alias)
        if _overlaps(idx, end):
            continue
        urls = _pick_urls(item)
        if not urls:
            continue
        photo_md = photos_to_markdown(name, urls, limit=per_spot)
        if not photo_md:
            continue
        occupied.append((idx, end))
        line_end = body.find("\n", end)
        if line_end < 0:
            line_end = len(body)
        inserts2.append((line_end, "\n" + photo_md))

    for pos, chunk in sorted(inserts2, key=lambda x: x[0], reverse=True):
        body = body[:pos] + chunk + body[pos:]
    return body, shown


def _attach_travel_photos(
    *,
    query: str,
    answer: str,
    attractions: list[dict[str, Any]],
    slots: dict[str, Any] | None,
    has_itinerary_draft: bool = False,
) -> tuple[str, list[dict[str, Any]], dict[str, Any], list[str]]:
    """
    按意图 + last_turn 图库冷却 + 会话配额决定是否插图。
    返回 (answer, images, slots_patch, extra_flags)。
    """
    flags: list[str] = []
    turn_index = next_photo_turn_index(slots)
    shown0 = normalize_photos_shown((slots or {}).get("photos_shown"))
    turn_spots = attraction_spot_names(attractions)
    intent_wants = wants_attraction_photos(query)
    allow, cool_flag = gate_photos_by_cooldown(
        query=query,
        turn_spots=turn_spots,
        photos_shown=shown0,
        turn_index=turn_index,
        intent_wants=intent_wants,
        slots=slots,
    )

    if not intent_wants or not allow:
        clean = strip_markdown_images(answer)
        if not intent_wants:
            flags.append("photos_skipped_no_intent")
        elif cool_flag:
            flags.append(cool_flag)
        # 推进 turn，冷却窗按 last_turn 自然过期
        patch = merge_photos_shown_patch(
            slots, shown0, photo_turn_index=turn_index
        )
        return clean, [], patch, flags

    # 先去掉模型正文里自带的图链，再按本轮 attractions 挂高德/库内图，避免串上轮景点 URL
    answer_clean = strip_markdown_images(answer)

    # 多景点高德查询并发预热，避免 weave 内串行打 POI
    prefetch_amap_snapshots_parallel(attractions, max_workers=3)

    woven, shown1 = weave_photos_by_spot_name(
        answer_clean,
        attractions,
        per_spot=2,
        photos_shown=shown0,
        allow_amap=True,
        turn_index=turn_index,
    )
    md_images = extract_markdown_images(woven or "")
    if md_images:
        # ![](url) 无 alt：用 weave 已写入的 photos_shown 按 URL 回填景点名
        url_to_spot: dict[str, str] = {}
        for spot_name, entry in shown1.items():
            for u in entry.get("urls") or []:
                u_s = str(u).strip()
                if u_s and spot_name:
                    url_to_spot[u_s] = str(spot_name)
        for img in md_images:
            url = str(img.get("url") or "").strip()
            alt = str(img.get("name") or "").strip()
            if (not alt or alt == "景点") and url in url_to_spot:
                img["name"] = url_to_spot[url]
        images = md_images
        flags.append("mixed_md_images")
        for img in md_images:
            name = str(img.get("name") or "").strip()
            url = str(img.get("url") or "").strip()
            # weave 已记过；仅当有真实景点名时再对齐一轮，避免空 alt 记成「景点」
            if name and name != "景点" and url:
                shown1 = record_spot_photos(
                    shown1, name=name, urls=[url], turn_index=turn_index
                )
    else:
        # 底部图也走同一套 URL/轮次约束
        images = []
        pool = select_reply_images(
            query=query,
            answer=woven or answer,
            attractions=attractions,
            has_itinerary_draft=has_itinerary_draft,
        )
        ban_all: set[str] = set()
        for entry in shown0.values():
            ban_all.update(str(u) for u in (entry.get("urls") or []) if u)
        for photo in pool:
            name = str(photo.get("name") or "").strip()
            url = str(photo.get("url") or "").strip()
            if not url or url in ban_all:
                continue
            ok, used = spot_photo_budget(name, shown1)
            if not ok or url in used:
                continue
            images.append(photo)
            ban_all.add(url)
            shown1 = record_spot_photos(
                shown1, name=name, urls=[url], turn_index=turn_index
            )
            if len(images) >= 3:
                break
        if images:
            flags.append("has_images")

    if images and "has_images" not in flags:
        flags.append("has_images")
    if not images and intent_wants and shown0:
        flags.append("photos_skipped_quota_or_dup")

    shown_names = [
        str(img.get("name") or "").strip()
        for img in images
        if str(img.get("name") or "").strip()
    ]
    if not shown_names and images:
        shown_names = [
            name
            for name, entry in shown1.items()
            if int((entry or {}).get("rounds") or 0)
            > int((shown0.get(name) or {}).get("rounds") or 0)
        ]
    if images:
        shown1 = stamp_photos_last_turn(
            shown1,
            names=shown_names or turn_spots[:3],
            turn_index=turn_index,
        )
        patch = merge_photos_shown_patch(
            slots, shown1, photo_turn_index=turn_index
        )
    else:
        patch = merge_photos_shown_patch(
            slots, shown0, photo_turn_index=turn_index
        )
    return woven, images, patch, flags


def _spot_evidence_card(
    item: dict[str, Any],
    *,
    transport_note: str = "",
    foods: list[str] | None = None,
    include_photos: bool = True,
) -> dict[str, Any]:
    name = str(item.get("name") or "")
    intro = (_intro_of(item) or "")[:180]
    photo_md = _spot_photos_md(item, limit=3, allow_amap=include_photos) if include_photos else ""
    intro_mixed = f"{intro}\n{photo_md}" if intro and photo_md else (intro or photo_md)
    card: dict[str, Any] = {
        "name": name,
        "city": item.get("city_norm") or item.get("city") or "",
        "region": item.get("region") or "",
        "category": item.get("category") or "",
        "intro": intro_mixed,
        "opening": (_opening_of(item) or "")[:100],
        "ticket": (_ticket_of(item) or "")[:80],
        "address": (_address_of(item) or "")[:120],
        "transport": (_transport_of(item) or transport_note or "")[:140],
        "tips": (_tips_of(item) or "")[:100],
        "tags": [t for t in (item.get("tags") or []) if t][:5],
        "nearby": _nearby_of(item),
    }
    if foods:
        card["foods"] = foods[:5]
    return {k: v for k, v in card.items() if v}


def _build_travel_evidence(
    *,
    query: str,
    attractions: list[dict[str, Any]],
    regions: list[dict[str, Any]],
    itinerary: str = "",
    itinerary_city: str = "",
    gate_notes: list[str] | None = None,
    tool_trace: list[dict[str, Any]] | None = None,
    spot_detail: bool = False,
    region_foods: list[str] | None = None,
    transport_note: str = "",
    include_photos: bool = True,
) -> str:
    """给收尾 LLM 的精简要点（非用户可见全文粘贴）。"""
    intent = parse_travel_intent(query)
    show = attractions[:3] if spot_detail else attractions[:5]
    payload: dict[str, Any] = {"user_question": query}
    if show:
        cards = []
        for i, item in enumerate(show):
            foods = region_foods if (spot_detail and i == 0) else None
            note = transport_note if (spot_detail and i == 0) else ""
            cards.append(
                _spot_evidence_card(
                    item,
                    transport_note=note,
                    foods=foods,
                    include_photos=include_photos,
                )
            )
        payload["spots"] = cards
        if include_photos:
            mixed = mix_spots_text_with_photos(show, per_spot=3)
            if mixed:
                payload["spots_mixed_md"] = mixed[:3500]
    if regions and not attractions:
        payload["regions"] = [
            {
                "name": r.get("region") or "",
                "season": r.get("season") or "",
                "foods": (r.get("foods") or [])[:5],
                "sample_spots": [
                    a.get("name") for a in (r.get("attractions") or [])[:6] if a.get("name")
                ],
            }
            for r in regions[:2]
        ]
    if itinerary.strip():
        payload["itinerary_draft"] = {
            "city": itinerary_city,
            "days": intent.trip_days if intent.trip_mode == "multi_day" else 1,
            "body": itinerary.strip()[:2500],
        }
    if gate_notes:
        payload["notes"] = gate_notes[:4]
    if tool_trace:
        human = format_tool_trace_human(tool_trace)[:8]
        if human:
            payload["trip_facts"] = human
    return json.dumps(payload, ensure_ascii=False)


async def _compose_travel_reply(
    *,
    query: str,
    evidence: str,
    attractions: list[dict[str, Any]],
    base: SubgraphResult,
    qwen: QwenClient,
    on_token: TokenCallback | None = None,
    has_itinerary_draft: bool = False,
    slots: dict[str, Any] | None = None,
    history: list[dict[str, str]] | None = None,
    stm_summary: str = "",
    ltm_context: list[dict[str, Any]] | None = None,
    short_fact: bool = False,
) -> SubgraphResult:
    guide_like = bool(
        re.search(
            r"攻略|怎么玩|一日游|两日游|三日游|五日游|多日游|行程|路线|安排|规划|怎么安排|排行程",
            query or "",
        )
    ) or has_itinerary_draft
    intent = parse_travel_intent(query)
    anchor_city = (
        intent.city_norm
        or intent.province_norm
        or ("、".join(intent.province_norms) if intent.province_norms else "")
    )
    if short_fact:
        layout_hint = (
            "本题是短事实草稿润色：保留 📍🕒🎫🏠🚗💡 结构与要点，口语轻度润色即可；"
            "不要扩写成攻略或 N 日游，不要删掉缺失字段的兜底说明。"
        )
    else:
        layout_hint = (
            "本题是明确行程/攻略：总标题只写一次；每天小标题写成「第N天 景点A → 景点B → 景点C」，"
            "禁止每天重复总标题；每天内用「上午：」「下午：」「晚上：」分段；天数跟用户走。"
            if guide_like
            else "本题不是行程题：直接口语分段回答即可，禁止输出「N日游」「Day」模板。"
        )
    city_lock = (
        f"目的地锁定：{anchor_city}。正文须属该地，禁止写成其它城市。"
        if anchor_city
        else "目的地以用户问题与要点为准，禁止擅自换成其它城市。"
    )
    # 助手旧答可能含半截图片 MD：注入 recent 时去掉图片，只保留纯文本指代线索
    cleaned_recent: list[dict[str, str]] = []
    for m in list(history or []):
        role = str(m.get("role") or "")
        content = strip_markdown_images(str(m.get("content") or "")).strip()
        if not content:
            continue
        if role == "assistant":
            content = content[:160]
        else:
            content = content[:200]
        cleaned_recent.append({"role": role, "content": content})
    ctx_prefix = build_llm_context_text(
        message=query,
        summary=stm_summary or "",
        recent=cleaned_recent,
        ltm=ltm_context,
        recent_user_only=False,
    )
    # build_llm_context_text 已含当前用户；下面再附检索要点与版式约束
    if short_fact:
        user = (
            f"{ctx_prefix}\n\n"
            f"{city_lock}\n"
            f"短事实草稿（请润色，勿改事实）：\n{evidence}\n\n"
            f"{layout_hint}\n"
            "请直接对旅客说话；你的首要任务是回答最新提问，禁止重提历史已答问题。"
        )
    else:
        user = (
            f"{ctx_prefix}\n\n"
            f"{city_lock}\n"
            f"要点（请转述，勿粘贴）：\n{evidence}\n\n"
            f"{layout_hint}\n"
            "请直接对旅客说话：内容稍丰富、语义连贯，适当分 2～4 段（段间空行），"
            "把实用信息写进句子里，不要标签式堆砌；"
            "你的首要任务是回答最新提问，禁止重提历史已答问题；"
            "仅当本轮明显在指代上文地点时才沿用目的地；"
            "资料中的 ![](URL) 请保留在对应景点旁，不要全部挪到最后；行首不要加「-」。"
            "不要输出半截图片链接。"
        )
    settings = get_settings()
    # 短事实润色更短；出图/普通收尾收紧；明确攻略题才放开
    if short_fact:
        max_tokens = min(360, max(128, int(settings.qwen_travel_reply_max_tokens)))
    else:
        max_tokens = (
            max(128, int(settings.qwen_travel_guide_max_tokens))
            if has_itinerary_draft or guide_like
            else max(128, int(settings.qwen_travel_reply_max_tokens))
        )
    try:
        weather_trace: list[dict[str, Any]] = []
        # 短事实（票价/开放等）不挂天气工具；其余由模型按 Prompt 自主决定是否 weather_query
        if short_fact:
            if on_token is not None:
                text, _pin, _pout = await qwen.chat_stream(
                    system=TRAVEL_REPLY_SYSTEM,
                    user=user,
                    temperature=0.55,
                    max_tokens=max_tokens,
                    on_token=on_token,
                )
            else:
                text, _pin, _pout = await qwen.chat(
                    system=TRAVEL_REPLY_SYSTEM,
                    user=user,
                    temperature=0.55,
                    max_tokens=max_tokens,
                )
        else:
            text, _pin, _pout, weather_trace = await qwen.chat_with_tools(
                system=TRAVEL_REPLY_SYSTEM,
                user=user,
                tools=[WEATHER_QUERY_TOOL],
                tool_dispatcher=lambda name, args: dispatch_weather_tool(name, args)
                or {"ok": False, "error": f"unknown_tool:{name}", "reply": WEATHER_UNAVAILABLE_REPLY},
                temperature=0.55,
                max_tokens=max_tokens,
                max_rounds=2,
                final_nudge=(
                    "工具结果已齐。请不要再调用工具；若天气失败请原样使用「"
                    + WEATHER_UNAVAILABLE_REPLY
                    + "」；否则基于工具结果直接口语回答最新提问。"
                ),
            )
            if on_token is not None and text:
                maybe = on_token(text)
                if inspect.isawaitable(maybe):
                    await maybe
        answer = markdown_to_plain(text) or text.strip()
        # 工具已给统一降级文案时，避免模型空话
        if not answer:
            for t in weather_trace:
                res = (t or {}).get("result") or {}
                if isinstance(res, dict) and res.get("reply"):
                    answer = str(res.get("reply"))
                    break
        answer, images, slots_patch, photo_flags = _attach_travel_photos(
            query=query,
            answer=answer or base.answer,
            attractions=attractions,
            slots=slots,
            has_itinerary_draft=has_itinerary_draft,
        )
        flags = list(base.domain_flags or [])
        flags.append("travel_reply_llm")
        if short_fact:
            flags.append("travel_short_fact_polish")
        if on_token is not None:
            flags.append("travel_streamed")
        if any(
            (t.get("name") in {"weather_query", "qweather_daily"}) for t in weather_trace
        ):
            flags.append("weather_query")
        flags.extend(photo_flags)
        return SubgraphResult(
            answer=answer or base.answer,
            entities=base.entities,
            constraints=base.constraints,
            sources=base.sources,
            confidence=max(float(base.confidence or 0.6), 0.78),
            domain_flags=flags,
            images=images,
            slots_patch=slots_patch,
        )
    except QwenError as e:
        logger.warning("travel reply llm failed: %s", e)
        # 短事实润色失败：直接回草稿模板
        fallback_raw = evidence if short_fact else base.answer
        fallback_answer = markdown_to_plain(fallback_raw) or fallback_raw
        fallback_answer, images, slots_patch, photo_flags = _attach_travel_photos(
            query=query,
            answer=fallback_answer,
            attractions=attractions,
            slots=slots,
            has_itinerary_draft=has_itinerary_draft,
        )
        flags = list(base.domain_flags or []) + ["travel_reply_fallback"] + photo_flags
        if short_fact:
            flags.append("travel_short_fact_draft")
        return SubgraphResult(
            answer=fallback_answer,
            entities=base.entities,
            constraints=base.constraints,
            sources=base.sources,
            confidence=base.confidence,
            domain_flags=flags,
            images=images,
            slots_patch=slots_patch,
        )


def _format_kb_answer(
    *,
    query: str,
    kb: KnowledgeSource,
    attractions: list[dict[str, Any]],
    regions: list[dict[str, Any]],
    itinerary: str = "",
    tool_trace: list[dict[str, Any]] | None = None,
    gate_notes: list[str] | None = None,
    itinerary_city: str = "",
    spot_detail: bool = False,
    region_foods: list[str] | None = None,
    transport_note: str = "",
    include_photos: bool = True,
) -> SubgraphResult:
    if kb == "legacy":
        label = "旧库 travel_knowledge（研发对比）"
    else:
        label = "正式库 attraction_kb（Postgres）"

    guide_mode = bool(_GUIDE_HINT.search(query or ""))
    intent = parse_travel_intent(query)
    lines: list[str] = [f"【旅游支线 · {label}】"]
    if (
        intent.city_norm
        or intent.province_norm
        or intent.province_norms
        or intent.tags
        or intent.max_price is not None
        or intent.name_query
    ):
        bits = []
        if intent.name_query:
            bits.append(f"景区={intent.name_query}")
        if intent.city_norm:
            bits.append(f"城市={intent.city_norm}")
        elif intent.province_norms:
            bits.append("省份=" + "、".join(intent.province_norms))
        elif intent.province_norm:
            bits.append(f"省份={intent.province_norm}")
        if intent.tags:
            bits.append("标签=" + "∩".join(intent.tags))
        if intent.max_price is not None:
            bits.append(f"票价<{intent.max_price}")
        if intent.price_sort != "none":
            bits.append(f"排序={intent.price_sort}")
        lines.append("筛选：" + "，".join(bits))
    if spot_detail:
        lines.append("（景区详情）")
    if guide_mode:
        lines.append("（攻略模式）")

    entities: list[Entity] = []
    sources: list[Source] = []
    show_items = attractions[:3] if spot_detail else attractions
    images = collect_travel_images(show_items, query) if include_photos else []

    if show_items:
        lines.append("")
        lines.append("景区详情：" if spot_detail else "相关景点：")
        for i, item in enumerate(show_items, 1):
            name = item.get("name") or "未命名"
            city = item.get("city_norm") or item.get("city") or ""
            region = item.get("region") or ""
            category = item.get("category") or ""
            summary = _intro_of(item)
            tips = _tips_of(item)
            opening = _opening_of(item)
            ticket = _ticket_of(item)
            address = _address_of(item)
            transport = _transport_of(item)
            nearby = _nearby_of(item)
            tags = [t for t in (item.get("tags") or []) if t][:4]
            photos = (
                _photos_of(item, limit=2 if guide_mode or spot_detail else 1)
                if include_photos
                else []
            )

            loc = " · ".join([p for p in [city, region] if p])
            head = f"{i}. {name}" + (f"（{loc}）" if loc else "")
            if category:
                head += f" · {category}"
            lines.append(head)
            if summary:
                lines.append(f"   简介：{summary[:120]}")
            if opening:
                lines.append(f"   开放：{opening[:100]}")
            if ticket:
                lines.append(f"   门票：{ticket[:60]}")
            if address:
                lines.append(f"   地址：{address[:100]}")
            if transport:
                lines.append(f"   交通：{transport[:120]}")
            elif spot_detail and i == 1 and transport_note:
                lines.append(f"   交通：{transport_note[:160]}")
            if tags:
                lines.append(f"   标签：{'、'.join(tags)}")
            if tips:
                lines.append(f"   建议：{tips[:80]}")
            if nearby:
                lines.append(f"   附近：{'、'.join(nearby)}")
            if spot_detail and i == 1 and region_foods:
                lines.append(f"   当地美食参考：{'、'.join(region_foods[:5])}")
            if include_photos:
                photo_md = _spot_photos_md(
                    item, limit=2 if guide_mode or spot_detail else 2, allow_amap=True
                )
                if photo_md:
                    lines.append(photo_md)

            entities.append(
                Entity(
                    type="attraction" if spot_detail else ("poi" if city else "city"),
                    code=None,
                    name=name if spot_detail else (f"{city}{name}" if city else name),
                )
            )
            if city:
                entities.append(Entity(type="city", code=None, name=city))
            if region:
                entities.append(Entity(type="province", code=None, name=str(region)))
            cover_url = photos[0]["url"] if photos else None
            sources.append(
                Source(
                    chunk_id=f"{kb}:attraction:{item.get('id') or name}:{city}",
                    source_url=cover_url,
                    effective_at=None,
                )
            )

    if regions and not attractions:
        lines.append("")
        lines.append("相关地区：")
        for region in regions:
            rname = region.get("region") or ""
            season = region.get("season") or ""
            foods = region.get("foods") or []
            attrs = region.get("attractions") or []
            lines.append(f"- {rname}" + (f"：{season}" if season else ""))
            if foods:
                lines.append(f"  美食参考：{'、'.join(foods[:5])}")
            names = [a.get("name") for a in attrs[:8] if a.get("name")]
            if names:
                lines.append(f"  景点示例：{'、'.join(names)}")
            entities.append(Entity(type="city", code=None, name=rname))

    if itinerary:
        lines.append("")
        if intent.trip_mode == "multi_day":
            title = (
                f"{intent.trip_days}日行程参考 · {itinerary_city}"
                if itinerary_city
                else f"{intent.trip_days}日行程参考"
            )
        else:
            title = f"一日游参考 · {itinerary_city}" if itinerary_city else "一日游参考"
        lines.append(title)
        lines.append(itinerary.strip())

    if gate_notes:
        lines.append("")
        lines.append("筛选说明：")
        for n in gate_notes[:6]:
            lines.append(f"- {n}")

    if tool_trace:
        lines.append("")
        human_trace = format_tool_trace_human(tool_trace)
        weather_lines = [t for t in human_trace if t.startswith("- 天气")]
        other_lines = [t for t in human_trace if not t.startswith("- 天气")]
        if weather_lines:
            lines.append("天气依据（和风预报，供行程参考）：")
            lines.extend(weather_lines)
            lines.append("")
        if other_lines:
            lines.append("行程依据（大约时间/距离，来自高德）：")
            lines.extend(other_lines)

    if not show_items and not regions:
        return SubgraphResult(
            answer=(
                f"暂时没找到相关景点或地区。"
                f"可以换个更具体的城市或景点名称再问（当前问题：「{query[:60]}」）。"
            ),
            confidence=0.25,
            domain_flags=["low_rag", f"kb:{kb}"],
            images=[],
        )

    conf = 0.82 if spot_detail and show_items else (0.75 if show_items else 0.55)
    flags = [f"kb:{kb}"]
    if kb == "enriched":
        flags.append("kb:postgres")
    if spot_detail:
        flags.append("spot_detail")
    if region_foods:
        flags.append("region_foods")
    if transport_note or any(_transport_of(x) for x in show_items[:1]):
        flags.append("spot_transport")
    if guide_mode:
        flags.append("guide_mode")
    if images:
        flags.append("has_images")
    if tool_trace:
        if any(t.get("name") in {"qweather_daily", "weather_query"} for t in tool_trace):
            flags.append("qweather")
        if any(str(t.get("name") or "").startswith("amap_") for t in tool_trace):
            flags.append("amap_itinerary_tools")
    if itinerary:
        flags.append("structured_itinerary")
        if intent.trip_mode == "multi_day":
            flags.append("multi_day_itinerary")
    if intent.tags:
        flags.append("tag_filter")
    if intent.name_query:
        flags.append("name_filter")
    if (intent.province_norms or intent.province_norm) and not intent.city_norm:
        flags.append("province_filter")
        if len(intent.province_norms) >= 2:
            flags.append("multi_province")
    elif intent.city_norm:
        flags.append("city_filter")
    return SubgraphResult(
        answer="\n".join(lines),
        entities=_uniq_entities(entities),
        constraints=[],
        sources=sources[:8],
        confidence=conf,
        domain_flags=flags,
        images=images,
    )


async def answer_travel(
    query: str,
    *,
    source: KnowledgeSource | None = None,
    qwen: QwenClient | None = None,
    on_token: TokenCallback | None = None,
    slots: dict[str, Any] | None = None,
    history: list[dict[str, str]] | None = None,
    stm_summary: str = "",
    ltm_context: list[dict[str, Any]] | None = None,
) -> SubgraphResult:
    """旅游支线。enriched=Postgres 正式库；legacy=文件旧库（仅研发对比）。"""
    # 兜底：身份问若仍误入 travel，直接自我介绍，禁止检索串题（如酥油茶）
    if is_identity_or_capability_query(query):
        return SubgraphResult(
            answer=(
                "你好，我是途途～主要帮你做旅游方面的咨询，比如去哪玩、怎么安排行程、"
                "当地吃住行小贴士。机场进出港、值机设施这类也可以问我，我会按机场指引来答。"
            ),
            confidence=0.9,
            domain_flags=["travel_identity_shortcircuit", f"kb:{_resolve_source(source)}"],
        )
    kb = _resolve_source(source)
    # 追问补目的地，避免短句检索空 → low_rag；本轮点名新地点时 enrich 不会拼旧城
    raw_intent = parse_travel_intent(query)
    search_query = enrich_query_with_context(query, slots=slots, history=history)
    intent = parse_travel_intent(search_query)
    intent_wants_photos = wants_attraction_photos(query)
    guide_mode = bool(_GUIDE_HINT.search(query or ""))
    spot_detail = _is_spot_detail(intent, guide_mode=guide_mode)
    if kb == "enriched":
        if spot_detail:
            limit = 3
        elif intent.trip_mode == "multi_day" or (
            (intent.province_norms or intent.province_norm) and not intent.city_norm
        ):
            limit = 12 if len(intent.province_norms) >= 2 else 10
        else:
            limit = 5
    else:
        limit = 3 if spot_detail else 6
    attractions = search_attractions(search_query, limit=limit, source=kb)
    if not attractions and search_query != (query or "").strip():
        attractions = search_attractions(query, limit=limit, source=kb)
    # 出图/换题：检索结果优先保留本轮问句点名的景点，避免上海会话里给西湖挂外滩图
    attractions = _prefer_attractions_for_query(
        query, attractions, raw_intent, source=kb, limit=limit
    )
    regions = search_regions(search_query, limit=2) if kb == "legacy" else []
    # 图库冷却：同话题追问即使命中出图白名单也不预取/混排高德图
    want_photos, _ = gate_photos_by_cooldown(
        query=query,
        turn_spots=attraction_spot_names(attractions),
        photos_shown=(slots or {}).get("photos_shown"),
        turn_index=next_photo_turn_index(slots),
        intent_wants=intent_wants_photos,
        slots=slots,
    )

    itinerary = ""
    tool_trace: list[dict[str, Any]] = []
    gate_notes: list[str] = []
    itinerary_city = ""
    region_foods: list[str] = []
    transport_note = ""

    # 景区详情：本地美食卡 + 可选 1 次高德补交通/地址；不进行程工具
    if spot_detail and attractions:
        top = attractions[0]
        region_foods = _region_foods_for_item(top)
        want_transport = bool(_TRANSPORT_HINT.search(query or ""))
        kb_transport = _transport_of(top)
        address = _address_of(top)
        if want_transport and not kb_transport:
            # 库内已有地址则不再打高德；仅缺地址时最多补 1 次 snapshot
            if not address:
                name = str(top.get("name") or intent.name_query or "")
                city = str(top.get("city_norm") or top.get("city") or "")
                try:
                    snap = amap_poi_snapshot_by_name(name=name, city=city)
                except Exception:
                    snap = {"ok": False}
                snap_addr = str((snap or {}).get("address") or "").strip()
                if snap_addr:
                    loc = top.get("location") if isinstance(top.get("location"), dict) else {}
                    if not isinstance(loc, dict):
                        loc = {}
                    loc = dict(loc)
                    loc["address"] = snap_addr
                    top["location"] = loc
                    address = snap_addr
            if address:
                transport_note = (
                    f"导航至「{address}」附近景区入口；"
                    "公共交通/自驾班次与路况以出行当天为准"
                )
            else:
                transport_note = "建议导航至景区官方入口；具体班次与路况以出行当天为准"

    if (
        kb == "enriched"
        and guide_mode
        and not spot_detail
        and qwen is not None
        and qwen.configured
    ):
        if intent.province_norms:
            place_hint = "、".join(intent.province_norms)
        else:
            place_hint = intent.province_norm or intent.city_norm or ""
        # 用户已点名城市时，行程候选先锁同城，避免草稿/收尾串到其它城市
        plan_pool = attractions
        if intent.city_norm:
            same = [
                a
                for a in attractions
                if (a.get("city_norm") or a.get("city") or "") == intent.city_norm
                or intent.city_norm in str(a.get("region") or "")
            ]
            if same:
                plan_pool = same
        if intent.trip_mode == "multi_day":
            itinerary, tool_trace, gate_notes, itinerary_city = await plan_multi_day_itinerary(
                query,
                plan_pool,
                qwen,
                trip_days=intent.trip_days,
                place_hint=place_hint or intent.city_norm,
            )
        else:
            itinerary, tool_trace, gate_notes, itinerary_city = await plan_structured_itinerary(
                query,
                plan_pool,
                qwen,
                preferred_city=intent.city_norm or "",
            )

    base = _format_kb_answer(
        query=query,
        kb=kb,
        attractions=attractions,
        regions=regions,
        itinerary=itinerary,
        tool_trace=tool_trace,
        gate_notes=gate_notes,
        itinerary_city=itinerary_city,
        spot_detail=bool(spot_detail and attractions),
        region_foods=region_foods,
        transport_note=transport_note,
        include_photos=want_photos,
    )
    # 独立收尾 LLM：依据要点直观回答（不与 aviation/chat 共用）
    low_rag = "low_rag" in (base.domain_flags or [])
    has_ctx = bool(history) or bool(stm_summary) or bool(context_bits_from_slots(slots))
    # 短事实：模板草稿 → 千问轻度润色（不跳过 compose）
    if (
        qwen is not None
        and qwen.configured
        and attractions
        and is_travel_short_fact(query, spot_detail=bool(spot_detail and attractions), guide_mode=guide_mode)
    ):
        top = attractions[0]
        draft = build_travel_short_fact_draft(
            top,
            transport_note=transport_note,
            opening=_opening_of(top),
            ticket=_ticket_of(top),
            address=_address_of(top),
            transport=_transport_of(top),
        )
        soft = SubgraphResult(
            answer=draft,
            confidence=0.8,
            domain_flags=["travel_short_fact", f"kb:{kb}"],
            images=[],
        )
        return await _compose_travel_reply(
            query=query,
            evidence=draft,
            attractions=attractions[:1],
            base=soft,
            qwen=qwen,
            on_token=on_token,
            has_itinerary_draft=False,
            slots=slots,
            history=history,
            stm_summary=stm_summary,
            ltm_context=ltm_context,
            short_fact=True,
        )
    # 短追问检索为空时，仍用对话上下文收尾，避免「找不到景点」
    if qwen is not None and qwen.configured and (not low_rag or has_ctx):
        if low_rag and has_ctx:
            evidence = json.dumps(
                {
                    "user_question": query,
                    "search_query": search_query,
                    "note": "本轮检索未命中新景点卡片；请仅根据近期对话与 slots 回答追问，禁止说找不到景点。",
                    "slots": {
                        k: v
                        for k, v in (slots or {}).items()
                        if k not in {"photos_shown", "photo_cooldown", "photo_turn_index"}
                        and v is not None
                    },
                },
                ensure_ascii=False,
            )
            soft = SubgraphResult(
                answer="",
                confidence=0.55,
                domain_flags=["travel_followup_ctx", f"kb:{kb}"],
                images=[],
            )
            return await _compose_travel_reply(
                query=query,
                evidence=evidence,
                attractions=[],
                base=soft,
                qwen=qwen,
                on_token=on_token,
                has_itinerary_draft=False,
                slots=slots,
                history=history,
                stm_summary=stm_summary,
                ltm_context=ltm_context,
            )
        evidence = _build_travel_evidence(
            query=search_query,
            attractions=attractions,
            regions=regions,
            itinerary=itinerary,
            itinerary_city=itinerary_city,
            gate_notes=gate_notes,
            tool_trace=tool_trace,
            spot_detail=bool(spot_detail and attractions),
            region_foods=region_foods,
            transport_note=transport_note,
            include_photos=want_photos,
        )
        return await _compose_travel_reply(
            query=query,
            evidence=evidence,
            attractions=attractions,
            base=base,
            qwen=qwen,
            on_token=on_token,
            has_itinerary_draft=bool(itinerary.strip()),
            slots=slots,
            history=history,
            stm_summary=stm_summary,
            ltm_context=ltm_context,
        )
    plain = markdown_to_plain(base.answer) or base.answer
    plain, images, slots_patch, photo_flags = _attach_travel_photos(
        query=query,
        answer=plain,
        attractions=attractions,
        slots=slots,
        has_itinerary_draft=bool(itinerary.strip()),
    )
    return SubgraphResult(
        answer=plain,
        entities=base.entities,
        constraints=base.constraints,
        sources=base.sources,
        confidence=base.confidence,
        domain_flags=list(base.domain_flags or []) + photo_flags,
        images=images,
        slots_patch=slots_patch,
    )


def _uniq_entities(items: list[Entity]) -> list[Entity]:
    seen: set[str] = set()
    out: list[Entity] = []
    for e in items:
        key = f"{e.type}:{e.name}"
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out[:12]

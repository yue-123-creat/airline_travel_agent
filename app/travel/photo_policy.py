"""Travel reply photo gating: intent + per-spot rounds/URL + last_turn 图库冷却."""

from __future__ import annotations

import re
from typing import Any

# 同会话同一景点最多成功出图轮次
MAX_PHOTO_ROUNDS_PER_SPOT = 2

# 同话题出图后的冷却轮数：距 last_turn 在 (0, N] 内的追问即使命中白名单也不出图
PHOTO_COOLDOWN_TURNS = 3

# 事务/信息意图：硬拦截，优先于视觉与探索（即使同句有「好玩/景点」也不出图）
_TX_INTENT = re.compile(
    r"(?:"
    r"门票|票价|价格|多少钱|收费|费用|预订|预约|订票|"
    r"怎么去|如何到达|导航|路线怎么走|多远|怎么走|"
    r"几点|开放时间|营业时间|交通|地铁|公交|大巴|"
    r"酒店|住宿|订房|民宿"
    r")"
)

# 强视觉意图：直接出图
_VISUAL_INTENT = re.compile(
    r"(?:"
    r"照片|图片|实景|街景|风景照|发几张|来几张|看看图|景点图|外观图|夜景图|"
    r"写真|代拍|长什么样|什么样子"
    r")"
)

# 探索/介绍意图：无事务词时才出图（不单独用「景点」，避免与事务问混淆）
_EXPLORE_INTENT = re.compile(
    r"(?:"
    r"好玩|怎么玩|介绍一下|看点|打卡|风景|夜景|"
    r"一日游|两日游|三日游|多日游|行程|攻略|怎么安排|"
    r"有什么玩|玩什么|推荐景点|推荐玩|推荐几个|推荐一下|带我看看|值得去吗|去哪玩|我想看看"
    r")"
)


def wants_attraction_photos(query: str) -> bool:
    """
    探索/视觉可出图；事务/信息强制不出图。
    判定顺序：事务硬拦 → 强视觉 → 探索介绍 → 默认 False。
    """
    q = (query or "").strip()
    if not q:
        return False
    if _TX_INTENT.search(q):
        return False
    if _VISUAL_INTENT.search(q):
        return True
    if _EXPLORE_INTENT.search(q):
        return True
    return False


def normalize_photos_shown(raw: Any) -> dict[str, dict[str, Any]]:
    """
    stm_slots.photos_shown →
      { "<景点名>": { "rounds": 1, "last_turn": 5, "urls": ["http://..."] } }
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for name, val in raw.items():
        key = str(name or "").strip()
        if not key:
            continue
        if isinstance(val, dict):
            urls = [
                str(u).strip()
                for u in (val.get("urls") or [])
                if str(u).strip()
            ]
            rounds = int(val.get("rounds") or 0)
            if rounds <= 0 and urls:
                rounds = 1
            last_turn = max(0, int(val.get("last_turn") or 0))
            out[key] = {
                "rounds": max(0, rounds),
                "last_turn": last_turn,
                "urls": list(dict.fromkeys(urls)),
            }
        elif isinstance(val, list):
            urls = [str(u).strip() for u in val if str(u).strip()]
            out[key] = {
                "rounds": 1 if urls else 0,
                "last_turn": 0,
                "urls": list(dict.fromkeys(urls)),
            }
        elif isinstance(val, int):
            out[key] = {"rounds": max(0, val), "last_turn": 0, "urls": []}
    return out


def next_photo_turn_index(slots: dict[str, Any] | None) -> int:
    """本轮 travel 出图用的 turn_index（在上一轮 photo_turn_index 上 +1）。"""
    return max(0, int((slots or {}).get("photo_turn_index") or 0)) + 1


def _apply_legacy_cooldown_to_shown(
    photos_shown: dict[str, dict[str, Any]],
    photo_cooldown: Any,
    *,
    turn_index: int,
) -> dict[str, dict[str, Any]]:
    """兼容旧 photo_cooldown{spots,remaining}：补 last_turn 后由轮次差接管。"""
    if not isinstance(photo_cooldown, dict):
        return photos_shown
    spots = [str(s).strip() for s in (photo_cooldown.get("spots") or []) if str(s).strip()]
    legacy = str(photo_cooldown.get("spot") or "").strip()
    if legacy and legacy not in spots:
        spots.append(legacy)
    remaining = max(0, int(photo_cooldown.get("remaining") or 0))
    if not spots or remaining <= 0:
        return photos_shown
    # remaining=R → 视作距上次出图已过 (N-R) 轮，last_turn = turn - (N-R)
    elapsed = max(0, int(PHOTO_COOLDOWN_TURNS) - remaining)
    last_turn = max(1, turn_index - elapsed)
    out = {
        k: {
            "rounds": int(v.get("rounds") or 0),
            "last_turn": int(v.get("last_turn") or 0),
            "urls": list(v.get("urls") or []),
        }
        for k, v in photos_shown.items()
    }
    for name in spots:
        entry = out.get(name) or {"rounds": 1, "last_turn": 0, "urls": []}
        if int(entry.get("last_turn") or 0) <= 0:
            entry["last_turn"] = last_turn
            if int(entry.get("rounds") or 0) <= 0:
                entry["rounds"] = 1
            out[name] = entry
    return out


def attraction_spot_names(attractions: list[dict[str, Any]] | None) -> list[str]:
    names: list[str] = []
    for item in attractions or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def _spots_fuzzy_match(a: str, b: str) -> bool:
    x, y = (a or "").strip(), (b or "").strip()
    if not x or not y:
        return False
    return x == y or x in y or y in x


def is_same_photo_topic(
    query: str,
    turn_spots: list[str],
    cooldown_spots: list[str],
) -> bool:
    """
    是否仍在冷却景点的话题内。
    - 问句点名冷却景点 → 同话题
    - 本轮检索景点与冷却景点有交集 → 同话题
    - 本轮有其它景点且无交集 → 新实体（异话题）
    - 本轮无景点名（短追问门票/天气等）→ 视为同话题续问
    """
    cool = [s for s in (cooldown_spots or []) if str(s).strip()]
    if not cool:
        return False
    q = (query or "").strip()
    if any(len(s) >= 2 and s in q for s in cool):
        return True
    turns = [s for s in (turn_spots or []) if str(s).strip()]
    if turns:
        for t in turns:
            for s in cool:
                if _spots_fuzzy_match(t, s):
                    return True
        return False
    return True


def spots_in_photo_cooldown(
    photos_shown: dict[str, dict[str, Any]],
    *,
    turn_index: int,
    window: int = PHOTO_COOLDOWN_TURNS,
) -> list[str]:
    """
    仍处冷却窗的景点：0 < (turn_index - last_turn) <= window。
    本轮刚出图 (delta=0) 不算「冷却拦截」对象（由本轮流程自己出图）。
    """
    cool: list[str] = []
    w = max(1, int(window))
    t = max(0, int(turn_index))
    for name, entry in (photos_shown or {}).items():
        last = int((entry or {}).get("last_turn") or 0)
        if last <= 0:
            continue
        delta = t - last
        if 0 < delta <= w:
            cool.append(str(name))
    return cool


def gate_photos_by_cooldown(
    *,
    query: str,
    turn_spots: list[str] | None,
    photos_shown: dict[str, dict[str, Any]] | None,
    turn_index: int,
    intent_wants: bool,
    slots: dict[str, Any] | None = None,
) -> tuple[bool, str | None]:
    """
    出图前冷却门控（基于 photos_shown[].last_turn）。
    返回 (允许出图?, 跳过标记)。
    原则：是否出图由用户意图决定；探索/介绍不冷启动、也不被同话题冷却拦住。
    冷却只拦强视觉追问刷图（照片/再发几张…），避免同景点连刷。
    """
    if not intent_wants:
        return False, None
    q = (query or "").strip()
    # 探索意图（好玩/推荐/攻略…）→ 直接允许；无「先出一次才算冷启动」
    if _EXPLORE_INTENT.search(q) and not _VISUAL_INTENT.search(q):
        return True, None
    shown = normalize_photos_shown(photos_shown)
    # 兼容旧 photo_cooldown
    if slots is not None:
        shown = _apply_legacy_cooldown_to_shown(
            shown,
            (slots or {}).get("photo_cooldown"),
            turn_index=turn_index,
        )
    cool = spots_in_photo_cooldown(shown, turn_index=turn_index)
    if not cool:
        return True, None
    if is_same_photo_topic(query, list(turn_spots or []), cool):
        return False, "photos_skipped_cooldown"
    return True, None


def spot_photo_budget(
    name: str,
    photos_shown: dict[str, dict[str, Any]],
    *,
    max_rounds: int = MAX_PHOTO_ROUNDS_PER_SPOT,
) -> tuple[bool, set[str]]:
    """
    返回 (还可出图?, 本景点已用过的 URL)。
    已满轮次 → 不可再出；未满则排除已用 URL（尽量两次不一样，没有新图则调用方应跳过）。
    """
    key = (name or "").strip()
    entry = photos_shown.get(key) or {"rounds": 0, "last_turn": 0, "urls": []}
    rounds = int(entry.get("rounds") or 0)
    used = {str(u).strip() for u in (entry.get("urls") or []) if str(u).strip()}
    if rounds >= max_rounds:
        return False, used
    return True, used


def record_spot_photos(
    photos_shown: dict[str, dict[str, Any]],
    *,
    name: str,
    urls: list[str],
    turn_index: int | None = None,
) -> dict[str, dict[str, Any]]:
    """合并本轮成功出图记录（有新 URL 才记一轮；可写 last_turn）。"""
    key = (name or "").strip()
    fresh = [str(u).strip() for u in urls if str(u).strip()]
    if not key or not fresh:
        return photos_shown
    out = {
        k: {
            "rounds": int(v.get("rounds") or 0),
            "last_turn": int(v.get("last_turn") or 0),
            "urls": list(v.get("urls") or []),
        }
        for k, v in photos_shown.items()
    }
    entry = out.get(key) or {"rounds": 0, "last_turn": 0, "urls": []}
    prev = set(entry["urls"])
    added = [u for u in fresh if u not in prev]
    if not added:
        return photos_shown
    entry["urls"] = list(entry["urls"]) + added
    entry["rounds"] = int(entry.get("rounds") or 0) + 1
    if turn_index is not None and int(turn_index) > 0:
        entry["last_turn"] = int(turn_index)
    out[key] = entry
    return out


def stamp_photos_last_turn(
    photos_shown: dict[str, dict[str, Any]],
    *,
    names: list[str],
    turn_index: int,
) -> dict[str, dict[str, Any]]:
    """给本轮实际出图的景点盖 last_turn（rounds/urls 已由 record 更新时也可再盖）。"""
    if turn_index <= 0:
        return photos_shown
    out = {
        k: {
            "rounds": int(v.get("rounds") or 0),
            "last_turn": int(v.get("last_turn") or 0),
            "urls": list(v.get("urls") or []),
        }
        for k, v in photos_shown.items()
    }
    for name in names:
        key = str(name or "").strip()
        if not key:
            continue
        entry = out.get(key) or {"rounds": 1, "last_turn": 0, "urls": []}
        if int(entry.get("rounds") or 0) <= 0:
            entry["rounds"] = 1
        entry["last_turn"] = int(turn_index)
        out[key] = entry
    return out


def merge_photos_shown_patch(
    slots: dict[str, Any] | None,
    photos_shown: dict[str, dict[str, Any]],
    *,
    photo_turn_index: int | None = None,
) -> dict[str, Any]:
    """生成写入 stm_slots 的 patch（photos_shown + photo_turn_index）。"""
    _ = slots
    patch: dict[str, Any] = {"photos_shown": normalize_photos_shown(photos_shown)}
    if photo_turn_index is not None and int(photo_turn_index) > 0:
        patch["photo_turn_index"] = int(photo_turn_index)
    # 新逻辑不再维护 photo_cooldown；显式清掉旧字段避免残留误导
    patch["photo_cooldown"] = {}
    return patch


def context_bits_from_slots(slots: dict[str, Any] | None) -> list[str]:
    """从 STM slots 取出目的地/景点，供追问检索与作答。"""
    if not isinstance(slots, dict):
        return []
    keys = (
        "spot",
        "attraction",
        "poi",
        "place",
        "destination",
        "city",
        "city_norm",
        "province",
    )
    out: list[str] = []
    seen: set[str] = set()
    for k in keys:
        v = slots.get(k)
        if v is None:
            continue
        if isinstance(v, (list, tuple)):
            parts = [str(x).strip() for x in v if str(x).strip()]
        else:
            parts = [str(v).strip()] if str(v).strip() else []
        for p in parts:
            if p not in seen:
                seen.add(p)
                out.append(p)
    return out


def _place_overlaps_context(place: str, bits: list[str]) -> bool:
    p = (place or "").strip()
    if not p:
        return False
    for b in bits:
        if _spots_fuzzy_match(p, b):
            return True
    return False


def enrich_query_with_context(
    query: str,
    *,
    slots: dict[str, Any] | None = None,
    history: list[dict[str, str]] | None = None,
) -> str:
    """
    短追问补上会话目的地，避免检索空、答非所问。
    若本轮已含足够地名、或点名了与 slots 不同的新地点 → 原样返回（禁止串旧目的地）。
    """
    from app.intent_guards import is_identity_or_capability_query
    from app.travel.query_intent import parse_travel_intent

    q = (query or "").strip()
    if is_identity_or_capability_query(q):
        return q
    bits = context_bits_from_slots(slots)
    if not bits and history:
        for msg in reversed(list(history)[-6:]):
            if (msg or {}).get("role") != "user":
                continue
            text = str((msg or {}).get("content") or "")
            for m in re.finditer(r"[\u4e00-\u9fff]{2,12}", text):
                tok = m.group(0)
                if tok in {"怎么样", "适合去", "下个月", "能不能", "可不可以"}:
                    continue
                if tok not in bits and len(tok) >= 2:
                    bits.append(tok)
                    if len(bits) >= 3:
                        break
            if bits:
                break
    if not bits:
        return q
    if any(b in q for b in bits):
        return q

    # 本轮已点名城市/景点，且与会话旧上下文无交集 → 视为换题，不拼旧目的地
    # 例：slots=上海迪士尼，问「看一下西湖的图片」不得变成「…（上下文：上海…）」
    intent = parse_travel_intent(q)
    mentioned = [
        p
        for p in (intent.city_norm, intent.name_query, intent.province_norm)
        if str(p or "").strip()
    ]
    if mentioned and not any(_place_overlaps_context(p, bits) for p in mentioned):
        return q

    return f"{q}（上下文：{' / '.join(bits[:3])}）"

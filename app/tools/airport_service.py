"""search_airport_service：DB 精准命中短路；未命中或长流程才锚定 aviation_rag。"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import psycopg
from psycopg.rows import dict_row

from app.config import get_settings
from app.rag.search import search_aviation_rag

logger = logging.getLogger(__name__)

# 大类：英文存库；中文入参映射到英文
_CATEGORY_ALIASES: dict[str, str] = {
    "transport": "transport",
    "交通": "transport",
    "交通接驳": "transport",
    "接驳": "transport",
    "快线": "transport",
    "停车": "transport",
    "出租": "transport",
    "navigation": "navigation",
    "乘机指引": "navigation",
    "指引": "navigation",
    "航站楼": "navigation",
    "问讯": "navigation",
    "checkin": "checkin",
    "check-in": "checkin",
    "值机": "checkin",
    "值机与安检": "checkin",
    "安检": "checkin",
    "安检通道": "checkin",
    "登机牌": "checkin",
    "行李额": "checkin",
    "托运": "checkin",
    # 「登机口」属航站楼指引，勿落到值机/安检
    "登机口": "navigation",
    "候机厅": "navigation",
    "登机": "checkin",
    "facility": "facility",
    "设施": "facility",
    "便民设施": "facility",
    "母婴": "facility",
    "寄存": "facility",
    "轮椅": "facility",
    "无障碍": "facility",
    "wifi": "facility",
    "WiFi": "facility",
    "出发": "navigation",
    "到达": "navigation",
    "lounge": "lounge",
    "休息室": "lounge",
    "贵宾": "lounge",
    "贵宾室": "lounge",
    "贵宾厅": "lounge",
    "贵宾与商业": "lounge",
    "商业": "lounge",
    "热线": "navigation",
    "兴滇": "lounge",
    "惠才": "lounge",
    "emergency": "emergency",
    "异常与保障": "emergency",
    "异常": "emergency",
    "保障": "emergency",
    "96566": "emergency",
    "延误": "emergency",
    "失物": "emergency",
    "policy": "policy",
    "规定": "policy",
    "净空": "policy",
    "机场规定": "policy",
    "禁飞": "policy",
}

_CATEGORY_LABELS: dict[str, str] = {
    "transport": "交通接驳",
    "navigation": "乘机指引",
    "checkin": "值机与安检",
    "facility": "便民设施",
    "lounge": "贵宾与商业",
    "emergency": "异常与保障",
    "policy": "机场规定/净空",
}

# 长流程：即使 DB 命中也要查 Qdrant 并拼接
_LONG_PROCESS_RE = re.compile(
    r"怎么走|如何走|怎么去航站|怎么过去|路线怎么走|乘机路线|到达路线|出发路线|"
    r"流程|步骤|先后顺序|怎么办理|如何办理|怎么办|"
    r"带轮椅|坐轮椅|无障碍.*怎么|无陪.*怎么|特殊旅客.*怎么|"
    r"中转.*怎么|延误.*手续|全程引导|手把手"
)

_PRICE_INTENT_RE = re.compile(r"多少钱|价格|费用|收费|票价|单价|元/|停车费")
_TIME_INTENT_RE = re.compile(r"几点|什么时候|截止|开放时间|班次|多久|时间")
_WHERE_INTENT_RE = re.compile(r"在哪|哪里|位置|地点|分布|几号门|哪个口")

_PRICE_KEYS = {
    "price",
    "fee",
    "fare",
    "cost",
    "ticket_price",
    "收费",
    "票价",
    "价格",
    "费用",
    "单价",
}
_TIME_KEYS = {
    "time",
    "hours",
    "open_hours",
    "截止",
    "时间",
    "开放",
    "班次",
    "时刻",
}
_LOC_KEYS = {"location", "addr", "address", "地址", "位置", "地点"}

SEARCH_AIRPORT_SERVICE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "search_airport_service",
        "description": (
            "查询机场基础信息与服务详情。"
            "仅当问句精准命中具体 service_name 或 details 字段时可短路（不查向量）；"
            "DB 未命中或长流程问才按 guide_chunk_ids 锚定 aviation_rag。"
            "入参 airport_code、category、可选 query。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "airport_code": {
                    "type": "string",
                    "description": "机场 IATA 三字码，如 LJG、PVG",
                },
                "category": {
                    "type": "string",
                    "description": (
                        "服务大类：transport|navigation|checkin|facility|lounge|emergency|policy"
                    ),
                },
                "query": {
                    "type": "string",
                    "description": "用户原问或检索关键词，用于精准匹配与向量排序",
                },
            },
            "required": ["airport_code", "category"],
        },
    },
}


def _norm_code(code: str) -> str:
    return (code or "").strip().upper()


def _norm_category(category: str) -> str | None:
    raw = (category or "").strip()
    if not raw:
        return None
    key = raw.lower() if raw.isascii() else raw
    if key in _CATEGORY_ALIASES:
        return _CATEGORY_ALIASES[key]
    compact = raw.replace(" ", "").replace("-", "").lower()
    return _CATEGORY_ALIASES.get(compact)


def infer_category(text: str) -> str | None:
    """从用户问句推断服务大类；无命中返回 None。"""
    raw = (text or "").strip()
    if not raw:
        return None
    keys = sorted(_CATEGORY_ALIASES.keys(), key=len, reverse=True)
    for key in keys:
        if key.isascii() and key.lower() in {
            "transport",
            "navigation",
            "checkin",
            "facility",
            "lounge",
            "emergency",
            "policy",
        }:
            if key.lower() in raw.lower():
                return _CATEGORY_ALIASES[key]
            continue
        if key in raw:
            return _CATEGORY_ALIASES[key]
    return None


def is_long_process_query(query: str) -> bool:
    q = (query or "").strip()
    return bool(q and _LONG_PROCESS_RE.search(q))


def _fmt_details(details: Any) -> str:
    if details is None:
        return ""
    if isinstance(details, str):
        try:
            details = json.loads(details)
        except json.JSONDecodeError:
            return details.strip()
    if isinstance(details, dict):
        if not details:
            return ""
        lines = []
        for k, v in details.items():
            if v is None or v == "":
                continue
            if isinstance(v, (dict, list)):
                v = json.dumps(v, ensure_ascii=False)
            lines.append(f"  - {k}: {v}")
        return "\n".join(lines)
    if isinstance(details, list):
        return "\n".join(f"  - {x}" for x in details)
    return str(details)


def _details_useful(details: Any) -> bool:
    return bool(_fmt_details(details).strip())


def _details_as_dict(details: Any) -> dict[str, Any]:
    if details is None:
        return {}
    if isinstance(details, dict):
        return details
    if isinstance(details, str):
        try:
            obj = json.loads(details)
        except json.JSONDecodeError:
            return {"text": details} if details.strip() else {}
        return obj if isinstance(obj, dict) else {"text": str(obj)}
    return {}


def _collect_guide_ids(services: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for svc in services:
        rag = svc.get("rag_links") if isinstance(svc.get("rag_links"), dict) else {}
        ids = rag.get("guide_chunk_ids") if isinstance(rag, dict) else None
        if not isinstance(ids, (list, tuple)):
            continue
        for x in ids:
            cid = str(x).strip()
            if cid and cid not in seen:
                seen.add(cid)
                out.append(cid)
    return out


def _name_parts(name: str) -> list[str]:
    raw = (name or "").strip()
    if not raw:
        return []
    parts = re.split(r"[\s/（）()、,，·\-—_]+", raw)
    out = [p for p in parts if len(p) >= 2]
    if raw not in out and len(raw) >= 2:
        out.append(raw)
    return out


def _longest_name_hit_in_query(name: str, query: str) -> str:
    """服务名中落在问句里的最长连续子串（≥2）。"""
    n = (name or "").strip()
    q = (query or "").strip()
    if not n or not q:
        return ""
    best = ""
    max_len = min(len(n), 16)
    for length in range(max_len, 1, -1):
        for i in range(0, len(n) - length + 1):
            sub = n[i : i + length]
            if sub in q and len(sub) > len(best):
                best = sub
                return best  # 已是当前 length 下首个，length 递减故即最长
    return best


# 问句同义词 → 用于抬升对应 service_name
_QUERY_NAME_HINTS: list[tuple[re.Pattern[str], tuple[str, ...]]] = [
    (re.compile(r"值机"), ("值机",)),
    (re.compile(r"停车|泊车"), ("停车",)),
    (re.compile(r"快线|民航班车|机场大巴|班车"), ("快线", "民航班车", "班车")),
    (re.compile(r"出租|打车|的士"), ("出租",)),
    (re.compile(r"网约车|滴滴"), ("网约",)),
    (re.compile(r"安检"), ("安检",)),
    (re.compile(r"母婴"), ("母婴",)),
    (re.compile(r"轮椅|无障碍"), ("无障碍", "轮椅")),
    (re.compile(r"失物|遗失"), ("失物",)),
    (re.compile(r"延误"), ("延误",)),
    (re.compile(r"出发"), ("出发",)),
    (re.compile(r"到达"), ("到达",)),
]


def _field_matches_intent(query: str, key: str) -> bool:
    k = (key or "").strip()
    if not k or k.lower() in {"text", "content", "body", "desc", "description"}:
        return False
    kl = k.lower()
    if _PRICE_INTENT_RE.search(query) and (kl in _PRICE_KEYS or k in _PRICE_KEYS):
        return True
    if _TIME_INTENT_RE.search(query) and (kl in _TIME_KEYS or k in _TIME_KEYS):
        return True
    if _WHERE_INTENT_RE.search(query) and (kl in _LOC_KEYS or k in _LOC_KEYS):
        return True
    # 问句直接点名字段
    if len(k) >= 2 and k in query:
        return True
    return False


def _score_service_match(query: str, svc: dict[str, Any]) -> tuple[float, str]:
    """问句 ↔ service_name / details 字段 精准打分；整类有 details 不算命中。"""
    q = (query or "").strip()
    if not q:
        return 0.0, ""
    name = str(svc.get("service_name") or "").strip()
    score = 0.0
    reasons: list[str] = []

    if name and name in q:
        score += 12.0 + min(len(name), 16) * 0.25
        reasons.append(f"name_full:{name}")
    else:
        hit = _longest_name_hit_in_query(name, q)
        if hit and hit not in {"服务", "相关", "其他", "信息", "机场"}:
            score += 5.0 + len(hit) * 1.2
            reasons.append(f"name_sub:{hit}")
        for part in _name_parts(name):
            if part in q and part != hit:
                score += min(3.0, len(part) * 0.4)
                reasons.append(f"name_part:{part}")

    # 问句同义词命中服务名
    for cre, stems in _QUERY_NAME_HINTS:
        if not cre.search(q):
            continue
        for stem in stems:
            if stem in name:
                score += 6.0
                reasons.append(f"syn:{stem}")
                break

    # 问「在哪」且名称含分布/位置/柜台 → 加分
    if _WHERE_INTENT_RE.search(q) and any(x in name for x in ("分布", "位置", "柜台", "地点")):
        score += 3.0
        reasons.append("where_name_hint")

    # 国内/国际互斥，避免「国内出发」同时命中国际行
    if "国内" in q and "国际" in name and "国内" not in name:
        score *= 0.35
        reasons.append("penalty_intl")
    if "国际" in q and "国内" in name and "国际" not in name:
        score *= 0.35
        reasons.append("penalty_dom")

    details = _details_as_dict(svc.get("details"))
    for key, val in details.items():
        if _field_matches_intent(q, str(key)):
            if val is None or val == "":
                continue
            score += 10.0
            reasons.append(f"field:{key}")
    if _PRICE_INTENT_RE.search(q) and score > 0:
        text = str(details.get("text") or "")
        if any(x in text for x in ("元", "票价", "收费", "免费")):
            score += 2.5
            reasons.append("price_in_text")

    loc = str(svc.get("location") or "").strip()
    if loc and _WHERE_INTENT_RE.search(q) and score > 0:
        score += 1.5
        reasons.append("has_location")

    # 必须有可用 details 或 location，否则不算硬事实命中
    if score > 0 and not (_details_useful(svc.get("details")) or loc):
        return 0.0, "no_payload"

    return score, "+".join(reasons)


def match_db_services(
    query: str,
    services: list[dict[str, Any]],
    *,
    min_score: float = 5.5,
) -> list[dict[str, Any]]:
    """精准匹配：返回得分足够的服务行（非整类兜底）。"""
    scored: list[tuple[float, dict[str, Any], str]] = []
    for svc in services:
        sc, reason = _score_service_match(query, svc)
        if sc >= min_score:
            scored.append((sc, svc, reason))
    if not scored:
        return []
    scored.sort(key=lambda x: (-x[0], str(x[1].get("service_name") or "")))
    best = scored[0][0]
    # 保留接近最高分的命中，避免同分近义多行被切掉
    kept = [s for s in scored if s[0] >= max(min_score, best * 0.75)]
    return [s[1] for s in kept]


def _rag_query(query: str, category: str, services: list[dict[str, Any]]) -> str:
    q = (query or "").strip()
    if q:
        return q
    label = _CATEGORY_LABELS.get(category, category)
    names = [str(s.get("service_name") or "").strip() for s in services]
    names = [n for n in names if n][:6]
    if names:
        return f"{label} {' '.join(names)}"
    return label


def _append_rag_section(lines: list[str], hits: list[Any], mode: str) -> None:
    if not hits:
        return
    lines.append("")
    lines.append("## 流程指引补充（向量文档，冲突时以数据库硬事实为准）")
    if mode:
        lines.append(f"- 检索模式：{mode}")
    for i, hit in enumerate(hits, 1):
        text = (getattr(hit, "text", None) or "").strip()
        if not text:
            continue
        if len(text) > 1800:
            text = text[:1800].rstrip() + "…"
        lines.append(f"### 摘录 {i}")
        lines.append(text)


def _to_markdown(
    airport: dict[str, Any],
    services: list[dict[str, Any]],
    category: str,
    *,
    rag_hits: list[Any] | None = None,
    rag_mode: str = "",
    db_section_title: str = "数据库硬事实（绝对权威）",
) -> str:
    code = airport.get("airport_code") or ""
    name = airport.get("name") or ""
    city = airport.get("city") or ""
    aliases = airport.get("aliases_airport") or []
    open_rule = airport.get("open_rule") or ""
    cat_label = _CATEGORY_LABELS.get(category, category)

    lines = [
        f"## 机场基础信息 · {code}",
        f"- 名称：{name}",
        f"- 城市：{city}",
    ]
    if aliases:
        if isinstance(aliases, (list, tuple)):
            alias_text = "、".join(str(a).strip() for a in aliases if str(a).strip())
        else:
            alias_text = str(aliases).strip()
        if alias_text:
            lines.append(f"- 别名：{alias_text}")
    if open_rule:
        lines.append(f"- 运营规则：{open_rule}")

    lines.append("")
    lines.append(f"## {db_section_title} · {cat_label}（{category}）")
    if not services:
        lines.append("（本问未精准命中库表服务行）")
    else:
        for i, svc in enumerate(services, 1):
            svc_name = svc.get("service_name") or "未命名服务"
            lines.append(f"### {i}. {svc_name}")
            loc = (svc.get("location") or "").strip()
            if loc:
                lines.append(f"- 位置：{loc}")
            detail_md = _fmt_details(svc.get("details"))
            if detail_md:
                lines.append("- 详情：")
                lines.append(detail_md)

    _append_rag_section(lines, rag_hits or [], rag_mode)
    return "\n".join(lines)


def search_airport_service(
    airport_code: str,
    category: str,
    query: str = "",
) -> dict[str, Any]:
    """DB 精准命中可短路；未命中或长流程才查 Qdrant；并存则拼接。"""
    code = _norm_code(airport_code)
    cat = _norm_category(category)
    if not code:
        return {"ok": False, "error": "missing_airport_code", "markdown": ""}
    if not cat:
        return {
            "ok": False,
            "error": "invalid_category",
            "category": category,
            "allowed": sorted(set(_CATEGORY_ALIASES.values())),
            "markdown": "",
        }

    url = get_settings().database_url
    try:
        with psycopg.connect(url, row_factory=dict_row) as conn:
            airport = conn.execute(
                """
                SELECT airport_code, aliases_airport, name, city, open_rule
                FROM airport_kb
                WHERE airport_code = %s
                """,
                (code,),
            ).fetchone()
            if not airport:
                return {
                    "ok": False,
                    "error": "airport_not_found",
                    "airport_code": code,
                    "markdown": f"未找到机场代码 `{code}` 的基础信息。",
                }

            services = conn.execute(
                """
                SELECT id, airport_code, service_category, service_name,
                       details, location, rag_links
                FROM airport_services
                WHERE airport_code = %s AND service_category = %s
                ORDER BY id
                """,
                (code, cat),
            ).fetchall()
    except Exception as e:
        logger.warning("search_airport_service failed: %s", e)
        return {"ok": False, "error": "db_error", "detail": str(e), "markdown": ""}

    svc_all = [dict(s) for s in services]
    q = (query or "").strip()
    long_process = is_long_process_query(q)
    matched = match_db_services(q, svc_all) if q else []
    db_precise_hit = bool(matched)

    # 短路：精准命中且非长流程 → 禁止 Qdrant
    short_circuit = db_precise_hit and not long_process
    # 查向量：未命中 或 长流程
    need_rag = (not db_precise_hit) or long_process

    rag_hits: list[Any] = []
    rag_mode = ""
    # 锚点：命中行优先，否则整类（仅用于允许查 RAG 时）
    anchor_svcs = matched if matched else svc_all
    guide_ids = _collect_guide_ids(anchor_svcs)
    if not guide_ids:
        guide_ids = _collect_guide_ids(svc_all)

    if need_rag:
        rag_q = _rag_query(q, cat, matched or svc_all)
        rag = search_aviation_rag(
            rag_q,
            guide_chunk_ids=guide_ids or None,
            airport_code=code,
        )
        rag_hits = list(rag.hits or [])
        rag_mode = str(rag.mode or "")
        if not rag_hits and rag.message:
            rag_mode = f"{rag_mode}:{rag.message}" if rag_mode else rag.message

    md = _to_markdown(
        dict(airport),
        matched if db_precise_hit else [],
        cat,
        rag_hits=rag_hits,
        rag_mode=rag_mode,
    )
    return {
        "ok": True,
        "airport_code": code,
        "category": cat,
        "service_count": len(svc_all),
        "matched_count": len(matched),
        "matched_service_names": [str(s.get("service_name") or "") for s in matched],
        "db_precise_hit": db_precise_hit,
        "long_process": long_process,
        "short_circuit": short_circuit,
        "guide_chunk_ids": guide_ids if need_rag else [],
        "rag_mode": rag_mode or None,
        "rag_hit_count": len(rag_hits),
        "markdown": md,
    }


def dispatch_airport_service_tool(name: str, args: dict[str, Any]) -> dict[str, Any] | None:
    """Return result if tool is search_airport_service, else None."""
    if name != "search_airport_service":
        return None
    return search_airport_service(
        airport_code=str(args.get("airport_code") or args.get("code") or ""),
        category=str(args.get("category") or args.get("service_category") or ""),
        query=str(args.get("query") or args.get("question") or ""),
    )

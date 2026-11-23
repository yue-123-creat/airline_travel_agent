"""航空支线：DB 精准命中短路（模板）；未命中/长流程再锚定 RAG；冲突以库表为准。"""

from __future__ import annotations

import logging
import re
from typing import Any

import psycopg
from psycopg.rows import dict_row

from app.config import get_settings
from app.llm.qwen import QwenClient, QwenError
from app.memory.context import build_llm_context_text
from app.models import Source, SubgraphResult
from app.reply_templates import build_aviation_short_fact_draft, is_aviation_short_fact
from app.render.markdown_text import markdown_to_plain
from app.tools.airport_service import (
    SEARCH_AIRPORT_SERVICE_TOOL,
    infer_category,
    search_airport_service,
)

logger = logging.getLogger(__name__)

_REALTIME_HINTS = ("航班动态", "实时航班", "延误多久", "登机口变更", "起飞时间", "降落时间", "准点率")
_IATA_RE = re.compile(r"\b([A-Za-z]{3})\b")

AVIATION_SYSTEM = """你是航途小程序出行助手「途途」，本轮负责机场与航站服务咨询。只根据下方「资料」用简洁、亲切的中文回答。

【首要任务】
你的首要任务是回答最新提问（见「=== 用户最新提问 ===」之后），禁止重提历史已答问题；会话摘要与最近对话仅用于理解指代。

【权威优先级 — 必须遵守】
- 「数据库硬事实」是绝对权威（价格、柜台号、时间、位置、电话等）。
- 若「流程指引补充 / 向量文档」中的描述与数据库硬事实有任何冲突，一律以数据库硬事实为准，不得用文档覆盖、调和或模糊带过。
- 未出现在数据库硬事实中的流程步骤，可以引用流程指引；但涉及数字、柜台、费用、时刻时，只能采用数据库写法。

规则：
- 优先使用资料中的机场基础信息与服务详情（名称、位置、details）。
- 允许在资料已给出的编号区间、枚举或分区说明上做数量与位置归纳；资料未写到的具体编号、票价、班次、开放时间不得补造。
- 仅当资料完全未提及相关线索时，才说明不足并建议问讯/看航班屏。
- 不要输出 chunk_id、rag_links、内部字段名或检索模式。
- 不要扮演航司/机场官方承诺；不提供实时航班动态。"""


def _slot_airport_code(slots: dict[str, Any] | None) -> str | None:
    """从 slots 取 IATA。兼容 Ingest 常见键：airport_code / airport / airport_name / city。"""
    slots = slots or {}
    for key in ("airport_code", "iata", "code"):
        raw = str(slots.get(key) or "").strip().upper()
        if len(raw) == 3 and raw.isalpha():
            return raw
    # Ingest 常写中文名到 airport / airport_name，偶发只写 city
    for key in ("airport_name", "airport", "city"):
        raw = str(slots.get(key) or "").strip()
        if not raw:
            continue
        if len(raw) == 3 and raw.isalpha():
            return raw.upper()
        hit = _match_airport_in_text(raw)
        if hit:
            return hit
    return None


def coerce_airport_slots(slots: dict[str, Any] | None) -> dict[str, Any]:
    """把 airport/city 等别名键规范成 airport_code + airport_name，便于 STM 续问。"""
    out = dict(slots or {})
    code = _slot_airport_code(out)
    if not code:
        return out
    out["airport_code"] = code
    name = str(out.get("airport_name") or out.get("airport") or "").strip()
    if name and not (len(name) == 3 and name.isalpha()):
        out["airport_name"] = name
    else:
        out["airport_name"] = _airport_label(code, out)
    return out


def _load_airport_kb_rows() -> list[dict[str, Any]]:
    url = get_settings().database_url
    try:
        with psycopg.connect(url, row_factory=dict_row) as conn:
            return list(
                conn.execute(
                    """
                    SELECT airport_code, name, city, aliases_airport
                    FROM airport_kb
                    """
                ).fetchall()
            )
    except Exception as e:
        logger.warning("load airport_kb failed: %s", e)
        return []


def _match_airport_in_text(text: str, rows: list[dict[str, Any]] | None = None) -> str | None:
    """从文本匹配机场：显式 IATA 或名称/城市/别名最长命中。"""
    q = (text or "").strip()
    if not q:
        return None

    for m in _IATA_RE.finditer(q):
        code = m.group(1).upper()
        if code.isalpha() and code.lower() not in {"the", "and", "for", "you", "are", "app"}:
            return code

    rows = rows if rows is not None else _load_airport_kb_rows()
    best: tuple[int, str] | None = None
    for row in rows:
        code = str(row.get("airport_code") or "").upper()
        if not code:
            continue
        needles = [str(row.get("name") or ""), str(row.get("city") or "")]
        aliases = row.get("aliases_airport") or []
        if isinstance(aliases, (list, tuple)):
            needles.extend(str(a) for a in aliases)
        score = 0
        for n in needles:
            n = (n or "").strip()
            if n and n in q:
                score = max(score, len(n))
        if score and (best is None or score > best[0]):
            best = (score, code)
    return best[1] if best else None


def _resolve_airport_code(
    query: str,
    slots: dict[str, Any] | None = None,
    history: list[dict[str, str]] | None = None,
) -> tuple[str | None, str]:
    """解析机场码。顺序：本轮问句 → slots → 近轮对话文本。返回 (code, source)。"""
    rows = _load_airport_kb_rows()
    from_query = _match_airport_in_text(query, rows)
    if from_query:
        return from_query, "query"

    from_slots = _slot_airport_code(slots)
    if from_slots:
        return from_slots, "slots"

    if history:
        # 最近轮优先：倒序拼接用户/助手正文
        parts: list[str] = []
        for msg in reversed(list(history)[-6:]):
            c = str((msg or {}).get("content") or "").strip()
            if c:
                parts.append(c)
        from_hist = _match_airport_in_text("\n".join(parts), rows)
        if from_hist:
            return from_hist, "history"

    return None, ""


def _airport_label(code: str, slots: dict[str, Any] | None = None) -> str:
    name = str(
        (slots or {}).get("airport_name") or (slots or {}).get("airport") or ""
    ).strip()
    if name and not (len(name) == 3 and name.isalpha()):
        return name
    for row in _load_airport_kb_rows():
        if str(row.get("airport_code") or "").upper() == code.upper():
            return str(row.get("name") or code).strip() or code
    return code


async def _organize_with_qwen(
    qwen: QwenClient,
    *,
    query: str,
    materials: str,
    history: list[dict[str, str]] | None = None,
    stm_summary: str = "",
    ltm_context: list[dict[str, Any]] | None = None,
    short_fact: bool = False,
    has_rag: bool = False,
) -> tuple[str, int, int]:
    ctx = build_llm_context_text(
        message=query,
        summary=stm_summary or "",
        recent=history,
        ltm=ltm_context,
    )
    conflict = (
        "再次强调：流程文档与数据库硬事实冲突时，必须采用数据库硬事实。"
        if has_rag
        else ""
    )
    if short_fact:
        user = (
            f"{ctx}\n\n"
            f"短事实草稿：\n{materials}\n\n"
            "请轻度润色：保留 ✈️ 与 💡 结构；把资料提炼成条目，总篇幅约不超过 300 字；"
            "语气亲切；会话摘要仅用于指代；硬事实（柜台、费用、时间、位置）不得改写数字或口径；"
            "不要扩写成无关攻略；不要输出内部字段名。"
            f"{conflict}"
        )
        max_tokens = min(360, max(128, int(get_settings().qwen_aviation_max_tokens)))
    else:
        user = (
            f"{ctx}\n\n"
            f"资料：\n{materials}\n\n"
            "请基于资料组织一段对旅客友好的回答。"
            "会话摘要与最近对话仅用于理解指代；回答以资料为准。"
            "若问「有几个/多少个」，请从资料中的编号区间或枚举归纳出数量或范围，不要空泛说未公布。"
            f"{conflict}"
        )
        max_tokens = max(128, int(get_settings().qwen_aviation_max_tokens))
    return await qwen.chat(
        system=AVIATION_SYSTEM,
        user=user,
        temperature=0.3,
        max_tokens=max_tokens,
        json_mode=False,
    )


async def answer_aviation(
    query: str,
    *,
    qwen: QwenClient | None = None,
    slots: dict[str, Any] | None = None,
    history: list[dict[str, str]] | None = None,
    stm_summary: str = "",
    ltm_context: list[dict[str, Any]] | None = None,
) -> SubgraphResult:
    q = (query or "").strip()
    flags: list[str] = []
    sources: list[Source] = []
    slots = coerce_airport_slots(slots)

    if any(h in q for h in _REALTIME_HINTS) or (
        ("延误" in q or "登机口" in q) and ("现在" in q or "实时" in q or "今天" in q)
    ):
        flags.append("realtime_claim")
        return SubgraphResult(
            answer=(
                "当前不支持实时航班、登机口变更或延误动态查询。"
                "可以问丽江等已入库机场的交通接驳、值机安检、便民设施、休息室或净空规定等静态指引。"
            ),
            entities=[],
            constraints=[],
            sources=[],
            confidence=0.55,
            domain_flags=flags,
        )

    code, code_src = _resolve_airport_code(q, slots, history)
    cat = infer_category(q)
    if slots:
        slot_cat = str(slots.get("category") or slots.get("service_category") or "").strip()
        if slot_cat:
            cat = cat or infer_category(slot_cat) or slot_cat

    if not code:
        flags.append("need_airport_code")
        return SubgraphResult(
            answer=(
                "请补充机场三字码或名称（例如 LJG / 丽江三义机场），"
                "并说明想了解交通、值机、设施还是休息室等哪一类。"
            ),
            confidence=0.45,
            domain_flags=flags,
        )

    if code_src and code_src != "query":
        flags.append(f"airport_from_{code_src}")
    airport_name = _airport_label(code, slots)
    slots_out = {"airport_code": code, "airport_name": airport_name}

    if not cat:
        cat = "navigation"
        flags.append("category_default_navigation")

    # 短追问未点名机场时，检索问句带上机场名，便于 service_name 匹配
    search_q = q
    if code_src in {"slots", "history"} and airport_name and airport_name not in q:
        search_q = f"{airport_name} {q}".strip()

    tool = search_airport_service(code, cat, query=search_q)
    if not tool.get("ok"):
        err = str(tool.get("error") or "lookup_failed")
        flags.append(f"airport_tool_{err}")
        md = str(tool.get("markdown") or "").strip()
        return SubgraphResult(
            answer=md or f"未能查询到机场 {code} 的服务信息（{err}）。",
            confidence=0.35,
            domain_flags=flags,
            slots_patch=slots_out,
        )

    materials = str(tool.get("markdown") or "").strip()
    db_hit = bool(tool.get("db_precise_hit"))
    short_circuit = bool(tool.get("short_circuit"))
    long_process = bool(tool.get("long_process"))
    rag_hits = int(tool.get("rag_hit_count") or 0)

    for name in tool.get("matched_service_names") or []:
        if name:
            flags.append(f"db_match:{name}")
    if db_hit:
        flags.append("db_precise_hit")
    if short_circuit:
        flags.append("db_short_circuit")
    if long_process:
        flags.append("long_process_query")
    for cid in tool.get("guide_chunk_ids") or []:
        sources.append(Source(chunk_id=str(cid)))
    if rag_hits > 0:
        flags.append("rag_anchored")
    else:
        flags.append("rag_skipped_or_empty")

    # 短路短事实：DB 精准命中且未走长流程 → 模板草稿 + 轻度润色
    use_short_fact = bool(
        short_circuit
        and db_hit
        and is_aviation_short_fact(q, materials=materials)
    )
    organize_input = materials
    if use_short_fact:
        organize_input = build_aviation_short_fact_draft(
            airport_name_or_code=code,
            materials=materials,
        )
        flags.append("aviation_short_fact")

    if qwen is not None and qwen.configured and organize_input:
        try:
            answer, _pin, _pout = await _organize_with_qwen(
                qwen,
                query=q,
                materials=organize_input,
                history=history,
                stm_summary=stm_summary,
                ltm_context=ltm_context,
                short_fact=use_short_fact,
                has_rag=rag_hits > 0,
            )
            answer = (answer or "").strip() or (
                markdown_to_plain(organize_input) if use_short_fact else materials
            )
            flags.append("qwen_organized")
            if use_short_fact:
                flags.append("aviation_short_fact_polish")
            return SubgraphResult(
                answer=answer,
                entities=[],
                constraints=[],
                sources=sources,
                confidence=0.82 if short_circuit else 0.78,
                domain_flags=flags,
                slots_patch=slots_out,
            )
        except QwenError as e:
            logger.warning("aviation qwen organize failed: %s", e)
            flags.append("llm_unavailable")
            if use_short_fact:
                return SubgraphResult(
                    answer=markdown_to_plain(organize_input) or organize_input,
                    entities=[],
                    constraints=[],
                    sources=sources,
                    confidence=0.65,
                    domain_flags=flags + ["aviation_short_fact_draft"],
                    slots_patch=slots_out,
                )

    fallback = organize_input if use_short_fact else materials
    return SubgraphResult(
        answer=(
            markdown_to_plain(fallback)
            if use_short_fact
            else (fallback or "暂无可用机场服务资料。")
        ),
        entities=[],
        constraints=[],
        sources=sources,
        confidence=0.62,
        domain_flags=flags,
        slots_patch=slots_out,
    )


def aviation_tools() -> list[dict[str, Any]]:
    return [SEARCH_AIRPORT_SERVICE_TOOL]

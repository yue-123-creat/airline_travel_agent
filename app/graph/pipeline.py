from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from app.aviation.service import answer_aviation, coerce_airport_slots
from app.chat.service import answer_chat
from app.config import Settings
from app.graph.state import GraphState
from app.intent_guards import is_greeting_only, is_identity_or_capability_query
from app.llm.qwen import QwenClient, QwenError, parse_json_object
from app.graph.topic_gate import (
    apply_slots_patch,
    dispatch_params_mismatch,
    sanitize_slots_for_turn,
)
from app.memory.context import (
    SUMMARY_SYSTEM,
    build_llm_context_payload,
    clamp_summary,
    format_messages_for_summary,
    stm_recent_messages,
    window_message_limit,
)
from app.models import SubgraphResult
from app.render.markdown_text import markdown_to_plain
from app.travel.service import answer_travel

TokenCallback = Callable[[str], Awaitable[None] | None]

INGEST_SYSTEM = """你是旅行问答系统的路由与槽位提取器。只输出 JSON，不要回答用户问题。
将用户问题分为: travel | aviation | chat | mixed。
aviation: 机场、航司、航站楼、行李额、值机、安检、登机手续与政策等。
travel: 景点、目的地、行程、酒店、美食、旅行向天气/汇率/地理等；含承接上文目的地的短追问。
chat: 问候、闲聊、问助手身份或能力、与任务无关。
mixed: 同时需要机场与旅游信息。

原则（优先于 slots/summary/recent）：
- 禁止从历史摘要中抓取旧主题回写，必须基于本轮最新输入提取。若用户转换话题，必须清空旧话题槽位（在 slots_patch 中把旧 city/spot/attraction/destination/airport_code/airport_name 等键设为 null）。
- 问助手身份或能力、或纯招呼 → route=chat；normalized_query 保持用户原句，不要改写成景点/目的地/美食。
- 本轮已是完整新主题 → normalized_query 只保留本轮原意，不要把上一轮目的地/机场拼进来。
- 旅游短追问：未再点名目的地时，slots_patch 仍应带回已有 city/spot（无冲突则原样），必要时写入 normalized_query。
- 航空短追问（如「热线多少」「贵宾服务热线」）：未再点名机场时，slots_patch 必须带回已有 airport_code（及已知 airport_name）；可将「机场名/三字码 + 原问」写入 normalized_query 便于检索。用户明确换机场时才覆盖 airport_code。
- 机场槽位键名：优先写 airport_code（IATA 三字码，如 WNZ、LJG）与 airport_name；不要只用 airport/city 代替 airport_code。不要编造未出现的机场 code。
输出: {
  "normalized_query":"...",
  "slots_patch":{},
  "entities":[],
  "route":"travel|aviation|chat|mixed",
  "confidence":0-1,
  "reasons":"..."
}"""

class Orchestrator:
    def __init__(
        self,
        settings: Settings,
        qwen: QwenClient,
        checkpointer: BaseCheckpointSaver | None = None,
    ):
        self.settings = settings
        self.qwen = qwen
        self.checkpointer = checkpointer
        self.graph = self._build_graph()
        self._token_sink: TokenCallback | None = None

    def _build_graph(self):
        g = StateGraph(GraphState)
        g.add_node("ingest_route", self._ingest_route)
        g.add_node("dispatch", self._dispatch)
        g.add_node("finalize", self._finalize)
        g.add_edge(START, "ingest_route")
        g.add_edge("ingest_route", "dispatch")
        g.add_edge("dispatch", "finalize")
        g.add_edge("finalize", END)
        if self.checkpointer is not None:
            return g.compile(checkpointer=self.checkpointer)
        return g.compile()

    def _thread_config(self, session_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": session_id}}

    def _window_turns(self) -> int:
        return max(1, int(self.settings.stm_window_turns))

    def _stm_window(self, state: GraphState) -> list[dict[str, str]]:
        return stm_recent_messages(
            list(state.get("stm_messages") or []),
            turns=self._window_turns(),
        )

    async def run(
        self,
        state: GraphState,
        *,
        on_token: TokenCallback | None = None,
    ) -> GraphState:
        session_id = state["session_id"]
        config = self._thread_config(session_id)
        turn_input: GraphState = {
            "request_id": state["request_id"],
            "user_id": state["user_id"],
            "session_id": session_id,
            "message": state["message"],
            "est_tokens": state.get("est_tokens") or 0,
            "reserve_id": state.get("reserve_id"),
            "quota_left": state.get("quota_left") or 0,
            "ltm_context": state.get("ltm_context") or [],
            "llm_calls": 0,
            "llm_call_breakdown": [],
            "tokens_in": 0,
            "tokens_out": 0,
            "abnormal_tags": [],
            "validation_pass": True,
            "clarify_only": False,
            "error_code": None,
            "error_message": None,
            "final_answer": "",
            "reply_images": [],
            "branch_results": {},
            "slots_patch": {},
        }
        self._token_sink = on_token
        try:
            result = await self.graph.ainvoke(turn_input, config)
        finally:
            self._token_sink = None
        return result  # type: ignore

    async def _ingest_route(self, state: GraphState) -> dict[str, Any]:
        tags = list(state.get("abnormal_tags") or [])
        breakdown = list(state.get("llm_call_breakdown") or [])
        tokens_in = int(state.get("tokens_in") or 0)
        tokens_out = int(state.get("tokens_out") or 0)
        llm_calls = int(state.get("llm_calls") or 0)

        slots = dict(state.get("stm_slots") or {})
        raw_message = str(state.get("message") or "")

        # 纯招呼：跳过 Ingest LLM，直接 chat（省一轮路由）
        if is_greeting_only(raw_message):
            tags = list(tags)
            if "ingest_skipped_greeting" not in tags:
                tags.append("ingest_skipped_greeting")
            return {
                "route": "chat",
                "route_confidence": 0.95,
                "normalized_query": raw_message.strip(),
                "slots_patch": {},
                "stm_slots": coerce_airport_slots(slots),
                "llm_calls": llm_calls,
                "llm_call_breakdown": breakdown,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "abnormal_tags": tags,
                "clarify_only": False,
            }

        history = self._stm_window(state)
        user_payload = build_llm_context_payload(
            message=state["message"],
            summary=state.get("stm_summary") or "",
            recent=history,
            ltm=state.get("ltm_context") or [],
            slots=slots,
        )
        try:
            content, pin, cout = await self.qwen.chat(
                system=INGEST_SYSTEM,
                user=json.dumps(user_payload, ensure_ascii=False),
                temperature=0.1,
                max_tokens=max(64, int(self.settings.qwen_ingest_max_tokens)),
                json_mode=True,
                model=self.settings.qwen_ingest_model or self.settings.qwen_chat_model,
            )
            data = parse_json_object(content)
            route = str(data.get("route") or "chat").lower()
            if route not in {"travel", "aviation", "chat", "mixed"}:
                route = "chat"
                tags.append("route_low_confidence")
            confidence = float(data.get("confidence") or 0.5)
            normalized = str(data.get("normalized_query") or state["message"])
            slots_patch = data.get("slots_patch") or {}
            if not isinstance(slots_patch, dict):
                slots_patch = {}
            llm_calls += 1
            breakdown.append("ingest_route")
            tokens_in += pin
            tokens_out += cout
        except (QwenError, json.JSONDecodeError, ValueError, TypeError, KeyError):
            # JSON 解析失败 / 字段类型异常 → 默认 chat，避免 500
            tags.append("route_low_confidence")
            tags.append("llm_unavailable_ingest")
            route = "chat"
            confidence = 0.3
            normalized = state["message"]
            slots_patch = {}

        # 硬门控：身份/能力问 → 强制 chat（仍可能已跑过 Ingest）
        if is_identity_or_capability_query(raw_message):
            route = "chat"
            normalized = raw_message.strip()
            confidence = max(float(confidence), 0.9)
            # 丢掉本轮误写入的目的地补丁，避免后续 enrich 串旧主题
            if isinstance(slots_patch, dict):
                for k in (
                    "city",
                    "city_norm",
                    "spot",
                    "attraction",
                    "poi",
                    "place",
                    "destination",
                    "province",
                ):
                    slots_patch.pop(k, None)
            tags = list(tags)
            if "identity_forced_chat" not in tags:
                tags.append("identity_forced_chat")

        # null/空串清槽；换题时 Ingest 应把旧主题键置 null
        slots = apply_slots_patch(slots, slots_patch)
        # Ingest 常写 airport/city；规范成 airport_code 供航空续问
        slots = coerce_airport_slots(slots)

        out: dict[str, Any] = {
            "route": route,
            "route_confidence": confidence,
            "normalized_query": normalized,
            "slots_patch": slots_patch,
            "stm_slots": slots,
            "llm_calls": llm_calls,
            "llm_call_breakdown": breakdown,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "abnormal_tags": tags,
            "clarify_only": False,
        }
        # 身份问不做低置信澄清（否则又拦在自我介绍前）
        if (
            confidence < self.settings.route_confidence_threshold
            and not is_identity_or_capability_query(raw_message)
        ):
            if "route_low_confidence" not in tags:
                tags.append("route_low_confidence")
            out["abnormal_tags"] = tags
            out["clarify_only"] = True
            out["final_answer"] = (
                "我需要确认一下你的意图：你是想了解旅游行程/景点，"
                "还是机场航司政策，或者只是随便聊聊？请补充一点信息。"
            )
            out["validation_pass"] = True
            out["branch_results"] = {}
        return out

    async def _run_branch(
        self,
        *,
        route: str,
        query: str,
        slots: dict[str, Any],
        history: list[dict[str, str]],
        summary: str,
        ltm: list[dict[str, Any]],
        tags: list[str],
        breakdown: list[str],
        llm_calls: int,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], list[str], list[str], int]:
        """按 route 调支线；返回 branch_results / reply_images / tags / breakdown / llm_calls。"""
        branch_results: dict[str, Any] = {}
        reply_images: list[dict[str, Any]] = []

        if route == "chat":
            chat = await answer_chat(
                query,
                qwen=self.qwen,
                on_token=self._token_sink,
                history=history,
                stm_summary=summary,
                ltm_context=ltm,
            )
            branch_results["chat"] = chat.model_dump()
            if "llm_unavailable" in (chat.domain_flags or []):
                tags.append("llm_unavailable")
            if "chat_reply_llm" in (chat.domain_flags or []):
                llm_calls += 1
                breakdown.append("chat")
        elif route == "travel":
            travel = await answer_travel(
                query,
                qwen=self.qwen,
                on_token=self._token_sink,
                slots=slots,
                history=history,
                stm_summary=summary,
                ltm_context=ltm,
            )
            branch_results["travel"] = travel.model_dump()
            reply_images = list(travel.images or [])
            if "amap_itinerary_tools" in (travel.domain_flags or []):
                llm_calls += 1
                breakdown.append("travel_itinerary_tools")
            if "travel_reply_llm" in (travel.domain_flags or []):
                llm_calls += 1
                breakdown.append("travel_reply")
        elif route == "aviation":
            result = await answer_aviation(
                query,
                qwen=self.qwen,
                slots=slots,
                history=history,
                stm_summary=summary,
                ltm_context=ltm,
            )
            branch_results["aviation"] = result.model_dump()
            tags.extend(result.domain_flags)
            if "qwen_organized" in (result.domain_flags or []):
                llm_calls += 1
                breakdown.append("aviation_organize")
        elif route == "mixed":
            travel, av = await asyncio.gather(
                answer_travel(
                    query,
                    qwen=self.qwen,
                    on_token=self._token_sink,
                    slots=slots,
                    history=history,
                    stm_summary=summary,
                    ltm_context=ltm,
                ),
                answer_aviation(
                    query,
                    qwen=self.qwen,
                    slots=slots,
                    history=history,
                    stm_summary=summary,
                    ltm_context=ltm,
                ),
            )
            branch_results["travel"] = travel.model_dump()
            reply_images = list(travel.images or [])
            if "amap_itinerary_tools" in (travel.domain_flags or []):
                llm_calls += 1
                breakdown.append("travel_itinerary_tools")
            if "travel_reply_llm" in (travel.domain_flags or []):
                llm_calls += 1
                breakdown.append("travel_reply")
            branch_results["aviation"] = av.model_dump()
            tags.extend(av.domain_flags)
            if "qwen_organized" in (av.domain_flags or []):
                llm_calls += 1
                breakdown.append("aviation_organize")
        else:
            branch_results["chat"] = SubgraphResult(
                answer="暂时无法识别意图。", confidence=0.2
            ).model_dump()

        return branch_results, reply_images, tags, breakdown, llm_calls

    async def _dispatch(self, state: GraphState) -> dict[str, Any]:
        if state.get("clarify_only"):
            return {}

        tags = list(state.get("abnormal_tags") or [])
        route = state.get("route") or "chat"
        raw_message = str(state.get("message") or "")
        query = state.get("normalized_query") or raw_message
        breakdown = list(state.get("llm_call_breakdown") or [])
        tokens_in = int(state.get("tokens_in") or 0)
        tokens_out = int(state.get("tokens_out") or 0)
        llm_calls = int(state.get("llm_calls") or 0)
        history = self._stm_window(state)
        summary = state.get("stm_summary") or ""
        ltm = state.get("ltm_context") or []
        slots = dict(state.get("stm_slots") or {})

        # 硬门控：工具/槽位参数（城/景/机场）与本轮原话不符 → 清槽 + 改用原话重试一次
        if route in {"travel", "aviation", "mixed"} and dispatch_params_mismatch(
            raw_message=raw_message,
            normalized_query=str(query or ""),
            slots=slots,
        ):
            tags.append("dispatch_topic_mismatch_retry")
            logger.info(
                "dispatch topic mismatch: raw=%r normalized=%r slots_places=%s",
                raw_message[:80],
                str(query)[:80],
                {k: slots.get(k) for k in ("city", "spot", "airport_code") if slots.get(k)},
            )
            slots = sanitize_slots_for_turn(raw_message, slots)
            query = raw_message.strip() or query

        branch_results, reply_images, tags, breakdown, llm_calls = await self._run_branch(
            route=route,
            query=str(query),
            slots=slots,
            history=history,
            summary=summary,
            ltm=ltm,
            tags=tags,
            breakdown=breakdown,
            llm_calls=llm_calls,
        )

        for br in branch_results.values():
            if not isinstance(br, dict):
                continue
            patch = br.get("slots_patch") or {}
            if not isinstance(patch, dict):
                continue
            slots = apply_slots_patch(slots, patch)

        slots = coerce_airport_slots(slots)

        return {
            "branch_results": branch_results,
            "reply_images": reply_images,
            "stm_slots": slots,
            "llm_calls": llm_calls,
            "llm_call_breakdown": breakdown,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "abnormal_tags": list(dict.fromkeys(tags)),
        }

    async def _summarize_overflow_bg(
        self,
        *,
        session_id: str,
        overflow: list[dict[str, str]],
        old_summary: str,
        max_chars: int,
    ) -> None:
        """溢出摘要后台计算，写回 checkpoint；不挡本轮回包。"""
        try:
            old_block = format_messages_for_summary(overflow)
            user = (
                f"旧摘要：\n{(old_summary or '（无）').strip()}\n\n"
                f"待压缩的旧对话：\n{old_block}\n\n"
                f"请输出更新后的会话摘要，尽量不超过 {max_chars} 字。"
            )
            content, _pin, _pout = await self.qwen.chat(
                system=SUMMARY_SYSTEM,
                user=user,
                temperature=0.1,
                max_tokens=max(64, int(self.settings.qwen_summary_max_tokens)),
                json_mode=False,
                model=self.settings.qwen_ingest_model or self.settings.qwen_chat_model,
            )
            new_summary = clamp_summary(content, max_chars) or old_summary
            await self.graph.aupdate_state(
                self._thread_config(session_id),
                {"stm_summary": new_summary},
            )
        except Exception as e:
            logger.warning("stm summary background failed session=%s: %s", session_id, e)

    def _trim_stm_on_overflow(
        self,
        *,
        messages: list[dict[str, str]],
        summary: str,
        tags: list[str],
        session_id: str,
    ) -> tuple[list[dict[str, str]], str, list[str]]:
        """
        仅窗口溢出时裁剪；摘要 LLM 后台跑，本轮不 await。
        未溢出：原样返回，绝不算摘要。
        """
        window_msgs = window_message_limit(self._window_turns())
        persist_msgs = window_message_limit(
            max(self._window_turns(), int(self.settings.stm_persist_max_turns))
        )
        max_chars = max(64, int(self.settings.stm_summary_max_chars))

        if len(messages) <= window_msgs:
            return messages, summary, tags

        overflow = messages[:-window_msgs]
        kept = messages[-window_msgs:]
        tags = list(tags)

        # 硬顶：极端膨胀时无无 LLM 应急裁剪文案并入 summary
        if len(messages) > persist_msgs:
            dropped = messages[:-persist_msgs]
            emergency = clamp_summary(
                ((summary + "\n") if summary else "")
                + format_messages_for_summary(dropped, per_msg_chars=80),
                max_chars,
            )
            tags.append("stm_summary_emergency_trim")
            kept = messages[-persist_msgs:]
            overflow = messages[:-persist_msgs]
            summary = emergency

        if session_id:
            if "stm_summary_scheduled" not in tags:
                tags.append("stm_summary_scheduled")
            asyncio.create_task(
                self._summarize_overflow_bg(
                    session_id=session_id,
                    overflow=overflow,
                    old_summary=summary,
                    max_chars=max_chars,
                )
            )
        return kept, summary, tags

    async def _finalize(self, state: GraphState) -> dict[str, Any]:
        tags = list(state.get("abnormal_tags") or [])
        breakdown = list(state.get("llm_call_breakdown") or [])
        tokens_in = int(state.get("tokens_in") or 0)
        tokens_out = int(state.get("tokens_out") or 0)
        llm_calls = int(state.get("llm_calls") or 0)

        if state.get("clarify_only") and state.get("final_answer"):
            # clarify 已是口语，仍走解析器去掉偶发 Markdown
            answer = markdown_to_plain(state["final_answer"]) or state["final_answer"]
        else:
            route = state.get("route") or "chat"
            branches = state.get("branch_results") or {}
            # 三支线答案汇合：travel/aviation 已由千问或检索生成；chat 不再二次千问
            if route == "mixed":
                parts = [
                    markdown_to_plain((branches.get("travel") or {}).get("answer") or ""),
                    markdown_to_plain((branches.get("aviation") or {}).get("answer") or ""),
                ]
                answer = "\n\n".join(p for p in parts if p).strip() or "暂无可用回复。"
            elif route in branches:
                raw = (branches[route] or {}).get("answer") or "暂无可用回复。"
                answer = markdown_to_plain(raw) or raw
            else:
                raw = next(
                    (v.get("answer") for v in branches.values() if v.get("answer")),
                    "暂无可用回复。",
                )
                answer = markdown_to_plain(raw) or raw

        prior = list(state.get("stm_messages") or [])
        prior.append({"role": "user", "content": state["message"]})
        prior.append({"role": "assistant", "content": answer})

        summary = state.get("stm_summary") or ""
        prior, summary, tags = self._trim_stm_on_overflow(
            messages=prior,
            summary=summary,
            tags=tags,
            session_id=str(state.get("session_id") or ""),
        )

        # travel 已口语化；仍轻量去 MD。图片走 reply_images，不塞进正文
        images = list(state.get("reply_images") or [])
        if not images:
            for br in (state.get("branch_results") or {}).values():
                if isinstance(br, dict) and br.get("images"):
                    images = list(br.get("images") or [])
                    break

        return {
            "final_answer": answer,
            "reply_images": images,
            "validation_pass": True,
            "abnormal_tags": list(dict.fromkeys(tags)),
            "stm_messages": prior,
            "stm_slots": dict(state.get("stm_slots") or {}),
            "stm_summary": summary,
            "llm_calls": llm_calls,
            "llm_call_breakdown": breakdown,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
        }

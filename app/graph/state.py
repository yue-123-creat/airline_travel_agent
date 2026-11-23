from __future__ import annotations

from typing import Any, TypedDict


class GraphState(TypedDict, total=False):
    request_id: str
    user_id: str
    session_id: str
    message: str
    est_tokens: int
    reserve_id: str | None
    quota_left: int
    ltm_context: list[dict[str, Any]]
    # STM persisted by LangGraph PostgresSaver (thread_id = session_id)
    stm_messages: list[dict[str, str]]
    stm_slots: dict[str, Any]
    stm_summary: str
    normalized_query: str
    slots_patch: dict[str, Any]
    route: str
    route_confidence: float
    branch_results: dict[str, Any]
    final_answer: str
    reply_images: list[dict[str, Any]]
    validation_pass: bool
    llm_calls: int
    llm_call_breakdown: list[str]
    tokens_in: int
    tokens_out: int
    abnormal_tags: list[str]
    quota_op: str
    error_code: str | None
    error_message: str | None
    clarify_only: bool

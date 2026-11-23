from __future__ import annotations

import uuid

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.api.chat_runtime import iter_chat_sse, run_chat_turn
from app.auth import parse_user_token
from app.deps import get_container
from app.memory.ltm_mock import LtmVersionConflict
from app.models import (
    AirportServiceQueryRequest,
    AirportServiceQueryResponse,
    ChatRequest,
    ChatResponse,
    MemoryItem,
    SessionCreateRequest,
    TravelQueryRequest,
    TravelQueryResponse,
)
from app.tools.airport_service import search_airport_service
from app.travel.knowledge import knowledge_stats, search_attractions
from app.travel.service import answer_travel, collect_travel_images

router = APIRouter()


@router.get("/health")
def health():
    c = get_container()
    try:
        c.redis.ping()
        redis_ok = True
    except Exception:
        redis_ok = False
    return {
        "ok": True,
        "redis": redis_ok,
        "qwen_configured": c.qwen.configured,
        "postgres_saver": c.checkpointer is not None,
        "contracts": "v0.1",
    }


@router.post("/v1/sessions")
def create_session(body: SessionCreateRequest):
    c = get_container()
    uid = parse_user_token(body.user_token, c.settings.auth_mode)
    session_id = f"s_{uuid.uuid4().hex[:16]}"
    c.db.upsert_session(session_id, uid)
    c.stm.ensure(session_id, uid)
    return {"session_id": session_id, "user_id": uid}


@router.get("/v1/sessions")
def list_sessions(user_token: str = Query(...)):
    c = get_container()
    uid = parse_user_token(user_token, c.settings.auth_mode)
    return {"sessions": c.db.list_sessions(uid)}


@router.get("/v1/sessions/{session_id}")
def get_session(session_id: str, user_token: str = Query(...)):
    c = get_container()
    uid = parse_user_token(user_token, c.settings.auth_mode)
    sess = c.db.get_session(session_id)
    if not sess or sess["user_id"] != uid:
        raise HTTPException(status_code=404, detail={"code": "session_not_found", "message": "not found"})
    return {"session": sess, "messages": c.db.list_messages(session_id)}


@router.delete("/v1/sessions/{session_id}")
def delete_session(session_id: str, user_token: str = Query(...)):
    c = get_container()
    uid = parse_user_token(user_token, c.settings.auth_mode)
    sess = c.db.get_session(session_id)
    if not sess or sess["user_id"] != uid:
        raise HTTPException(status_code=404, detail={"code": "session_not_found", "message": "not found"})
    c.db.delete_session(session_id)
    return {"ok": True}


@router.get("/v1/admin/logs")
def admin_logs(x_admin_token: str = Header(default=""), limit: int = 50):
    c = get_container()
    if x_admin_token != c.settings.admin_token:
        raise HTTPException(status_code=401, detail={"code": "unauthorized", "message": "bad admin token"})
    return {"events": c.telemetry.tail(limit)}


@router.get("/v1/admin/stm/{session_id}")
async def admin_stm(session_id: str, x_admin_token: str = Header(default="")):
    """Inspect LangGraph PostgresSaver STM for acceptance tests."""
    c = get_container()
    if x_admin_token != c.settings.admin_token:
        raise HTTPException(status_code=401, detail={"code": "unauthorized", "message": "bad admin token"})
    if c.checkpointer is None:
        raise HTTPException(status_code=503, detail={"code": "stm_unavailable", "message": "checkpointer not ready"})
    config = {"configurable": {"thread_id": session_id}}
    snap = await c.orchestrator.graph.aget_state(config)
    values = dict(snap.values or {}) if snap else {}
    return {
        "session_id": session_id,
        "stm_messages": values.get("stm_messages") or [],
        "stm_slots": values.get("stm_slots") or {},
        "stm_summary": values.get("stm_summary") or "",
    }


@router.post("/v1/admin/ltm/{user_id}/seed")
def admin_ltm_seed(user_id: str, x_admin_token: str = Header(default="")):
    """Seed Mock LTM for a user (P0 acceptance)."""
    c = get_container()
    if x_admin_token != c.settings.admin_token:
        raise HTTPException(status_code=401, detail={"code": "unauthorized", "message": "bad admin token"})

    patch = [
        MemoryItem(
            type="frequent_city",
            key="home_city",
            value="杭州",
            confidence=0.95,
        )
    ]
    for _ in range(3):
        current = c.ltm.get(user_id)
        version = int(current.get("version") or 0)
        try:
            return c.ltm.patch(user_id, version, patch)
        except LtmVersionConflict:
            continue
    raise HTTPException(status_code=409, detail={"code": "ltm_version_conflict", "message": "retry exhausted"})


@router.post("/v1/admin/quota/sweep")
def admin_quota_sweep(x_admin_token: str = Header(default="")):
    c = get_container()
    if x_admin_token != c.settings.admin_token:
        raise HTTPException(status_code=401, detail={"code": "unauthorized", "message": "bad admin token"})
    return {"released": c.quota.sweep_expired()}


@router.get("/v1/travel/sources")
def travel_sources():
    """查看旧/新知识库路径与条目数，以及 chat 默认用哪套。"""
    c = get_container()
    return {
        "chat_default": c.settings.travel_knowledge_source,
        "legacy": knowledge_stats("legacy"),
        "enriched": knowledge_stats("enriched"),
    }


@router.post("/v1/travel/legacy", response_model=TravelQueryResponse)
async def travel_legacy(body: TravelQueryRequest):
    """研发对比 · 旧库 travel_knowledge.json；正式路径不读。"""
    query = (body.query or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail={"code": "bad_request", "message": "query required"})
    result = await answer_travel(query, source="legacy")
    hits = search_attractions(query, limit=body.limit, source="legacy")
    return TravelQueryResponse(
        knowledge_source="legacy",
        answer=result.answer,
        hits=hits,
        images=collect_travel_images(hits, query),
        confidence=result.confidence,
        domain_flags=result.domain_flags,
    )


@router.post("/v1/travel/enriched", response_model=TravelQueryResponse)
async def travel_enriched(body: TravelQueryRequest):
    """正式库 · Postgres attraction_kb（标签过滤/排序）；行程题可调高德距离工具。"""
    c = get_container()
    query = (body.query or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail={"code": "bad_request", "message": "query required"})
    result = await answer_travel(query, source="enriched", qwen=c.qwen)
    hits = search_attractions(query, limit=min(body.limit, 5), source="enriched")
    return TravelQueryResponse(
        knowledge_source="enriched",
        answer=result.answer,
        hits=hits,
        images=collect_travel_images(hits, query),
        confidence=result.confidence,
        domain_flags=result.domain_flags,
    )


@router.post("/v1/aviation/airport-service", response_model=AirportServiceQueryResponse)
def aviation_airport_service(body: AirportServiceQueryRequest):
    """机场服务：库表优先；details 不足则按 guide_chunk_ids 锚定 aviation_rag，拼 Markdown。"""
    result = search_airport_service(
        body.airport_code,
        body.category,
        query=body.query or "",
    )
    return AirportServiceQueryResponse(
        ok=bool(result.get("ok")),
        airport_code=result.get("airport_code"),
        category=result.get("category"),
        service_count=int(result.get("service_count") or 0),
        markdown=str(result.get("markdown") or ""),
        guide_chunk_ids=list(result.get("guide_chunk_ids") or []),
        rag_mode=result.get("rag_mode"),
        rag_hit_count=int(result.get("rag_hit_count") or 0),
        error=result.get("error"),
    )


@router.post("/v1/chat", response_model=ChatResponse)
async def chat(body: ChatRequest):
    """同步聊天：三支线汇合后 Markdown→纯文本。/docs 可看完整纯文本。"""
    c = get_container()
    try:
        resp, _event = await run_chat_turn(c, body)
        return resp
    except HTTPException:
        raise
    except Exception as e:
        if "DASHSCOPE" in str(e) or "Qwen" in str(e):
            raise HTTPException(status_code=503, detail={"code": "llm_unavailable", "message": str(e)[:200]})
        raise HTTPException(status_code=500, detail={"code": "internal_error", "message": str(e)[:200]})


@router.post("/v1/chat/stream")
async def chat_stream(body: ChatRequest):
    """SSE 流式：status → meta → token* → done。会话页/小程序用；Swagger 看不清逐字效果。"""
    c = get_container()
    return StreamingResponse(
        iter_chat_sse(c, body),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

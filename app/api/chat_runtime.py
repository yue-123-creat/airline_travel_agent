"""Shared chat turn execution for sync + SSE stream (Docker / 小程序同一套）。"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import HTTPException

from app.auth import parse_user_token
from app.deps import AppContainer
from app.graph.state import GraphState
from app.models import ChatRequest, ChatResponse, UsageOut
from app.quota.service import QuotaError
from app.render.markdown_text import iter_plain_chunks, markdown_to_plain


def _ensure_session(c: AppContainer, uid: str, session_id: str | None) -> str:
    if not session_id:
        sid = f"s_{uuid.uuid4().hex[:16]}"
        c.db.upsert_session(sid, uid)
        c.stm.ensure(sid, uid)
        return sid
    sess = c.db.get_session(session_id)
    if not sess:
        c.db.upsert_session(session_id, uid)
        c.stm.ensure(session_id, uid)
        return session_id
    if sess["user_id"] != uid:
        raise HTTPException(status_code=404, detail={"code": "session_not_found", "message": "not found"})
    c.stm.ensure(session_id, uid)
    return session_id


def _build_state(
    *,
    request_id: str,
    uid: str,
    session_id: str,
    message: str,
    est: int,
    reserve_id: str | None,
    quota_left: int,
    ltm_items: list[dict[str, Any]],
) -> GraphState:
    return {
        "request_id": request_id,
        "user_id": uid,
        "session_id": session_id,
        "message": message,
        "est_tokens": est,
        "reserve_id": reserve_id,
        "quota_left": quota_left,
        "ltm_context": ltm_items,
        "llm_calls": 0,
        "llm_call_breakdown": [],
        "tokens_in": 0,
        "tokens_out": 0,
        "abnormal_tags": [],
        "validation_pass": True,
        "reply_images": [],
    }


def _persist_and_respond(
    c: AppContainer,
    *,
    body_message: str,
    session_id: str,
    request_id: str,
    reserve_id: str | None,
    est: int,
    quota_left: int,
    result: dict[str, Any],
    t0: float,
) -> tuple[ChatResponse, dict[str, Any]]:
    answer = markdown_to_plain(result.get("final_answer") or "") or "暂无回复。"
    images = list(result.get("reply_images") or [])
    actual = int(result.get("tokens_in") or 0) + int(result.get("tokens_out") or 0)
    if actual <= 0:
        actual = min(est, 200)
    # Redis 配额必须同步 commit；PG usage_ledger 异步，不挡回包
    c.quota.commit(reserve_id, actual)

    c.db.add_message(session_id, "user", body_message, {"request_id": request_id})
    c.db.add_message(
        session_id,
        "assistant",
        answer,
        {"request_id": request_id, "route": result.get("route"), "images": images},
    )

    event = {
        "request_id": request_id,
        "user_id": result.get("user_id"),
        "session_id": session_id,
        "route": result.get("route"),
        "route_confidence": result.get("route_confidence"),
        "llm_calls": result.get("llm_calls") or 0,
        "llm_call_breakdown": result.get("llm_call_breakdown") or [],
        "tokens_in": result.get("tokens_in") or 0,
        "tokens_out": result.get("tokens_out") or 0,
        "quota_left": quota_left,
        "reserve_id": reserve_id,
        "est_tokens": est,
        "actual_tokens": actual,
        "quota_op": "commit",
        "validate_pass": bool(result.get("validation_pass", True)),
        "abnormal_tags": result.get("abnormal_tags") or [],
        "latency_ms": int((time.time() - t0) * 1000),
        "image_count": len(images),
    }
    c.telemetry.emit(event)
    c.db.write_usage_async(event)

    resp = ChatResponse(
        session_id=session_id,
        answer=answer,
        route=result.get("route"),
        usage=UsageOut(
            prompt_tokens=int(result.get("tokens_in") or 0),
            completion_tokens=int(result.get("tokens_out") or 0),
            est_tokens=est,
            actual_tokens=actual,
        ),
        validation_pass=bool(result.get("validation_pass", True)),
        llm_calls=int(result.get("llm_calls") or 0),
        abnormal_tags=list(result.get("abnormal_tags") or []),
        request_id=request_id,
        reserve_id=reserve_id,
        images=images,
    )
    return resp, event


async def run_chat_turn(c: AppContainer, body: ChatRequest) -> tuple[ChatResponse, dict[str, Any]]:
    t0 = time.time()
    request_id = f"req_{uuid.uuid4().hex[:16]}"
    uid = parse_user_token(body.user_token, c.settings.auth_mode)
    message = (body.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail={"code": "bad_request", "message": "message required"})

    session_id = _ensure_session(c, uid, body.session_id)

    est = c.settings.quota_est_default
    reserve_id = None
    quota_left = 0
    try:
        reserve_id, quota_left = c.quota.check_and_reserve(uid, session_id, est)
    except QuotaError as e:
        raise HTTPException(status_code=429, detail={"code": e.code, "message": e.message})

    ltm = c.ltm.get(uid)
    state = _build_state(
        request_id=request_id,
        uid=uid,
        session_id=session_id,
        message=message,
        est=est,
        reserve_id=reserve_id,
        quota_left=quota_left,
        ltm_items=ltm.get("items") or [],
    )

    try:
        result = await c.orchestrator.run(state)
        return _persist_and_respond(
            c,
            body_message=message,
            session_id=session_id,
            request_id=request_id,
            reserve_id=reserve_id,
            est=est,
            quota_left=quota_left,
            result=result,
            t0=t0,
        )
    except Exception:
        try:
            if reserve_id:
                c.quota.release(reserve_id)
        except Exception:
            pass
        raise


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def iter_chat_sse(c: AppContainer, body: ChatRequest) -> AsyncIterator[str]:
    """SSE：配额就绪后立刻 meta；travel 真流式 token；其它支线仍块推。"""
    t0 = time.time()
    request_id = f"req_{uuid.uuid4().hex[:16]}"
    try:
        uid = parse_user_token(body.user_token, c.settings.auth_mode)
        message = (body.message or "").strip()
        if not message:
            raise HTTPException(status_code=400, detail={"code": "bad_request", "message": "message required"})
        session_id = _ensure_session(c, uid, body.session_id)
        est = c.settings.quota_est_default
        reserve_id, quota_left = c.quota.check_and_reserve(uid, session_id, est)
    except HTTPException as e:
        detail = e.detail if isinstance(e.detail, dict) else {"message": str(e.detail)}
        yield _sse("error", {"status": e.status_code, **detail})
        return
    except QuotaError as e:
        yield _sse("error", {"status": 429, "code": e.code, "message": e.message})
        return
    except Exception as e:
        yield _sse("error", {"message": str(e)})
        return

    # 早发 meta：前端可立刻绑 session_id，不必等首 token / 整轮结束
    yield _sse(
        "meta",
        {
            "session_id": session_id,
            "request_id": request_id,
            "quota_left": quota_left,
            "streaming": True,
            "early": True,
        },
    )
    yield _sse("status", {"phase": "running", "message": "正在检索与生成…"})

    ltm = c.ltm.get(uid)
    state = _build_state(
        request_id=request_id,
        uid=uid,
        session_id=session_id,
        message=message,
        est=est,
        reserve_id=reserve_id,
        quota_left=quota_left,
        ltm_items=ltm.get("items") or [],
    )

    token_q: asyncio.Queue[str | None] = asyncio.Queue()
    streamed_any = False

    async def on_token(delta: str) -> None:
        nonlocal streamed_any
        if delta:
            streamed_any = True
            await token_q.put(delta)

    async def _run() -> dict[str, Any]:
        try:
            return await c.orchestrator.run(state, on_token=on_token)
        finally:
            await token_q.put(None)

    task = asyncio.create_task(_run())
    try:
        while True:
            if task.done() and token_q.empty():
                break
            try:
                item = await asyncio.wait_for(token_q.get(), timeout=0.05)
            except TimeoutError:
                if task.done():
                    while not token_q.empty():
                        item = token_q.get_nowait()
                        if item is None:
                            break
                        yield _sse("token", {"text": item})
                    break
                continue
            if item is None:
                break
            yield _sse("token", {"text": item})

        result = await task
    except Exception as e:
        try:
            c.quota.release(reserve_id)
        except Exception:
            pass
        yield _sse("error", {"message": str(e)})
        return

    try:
        resp, event = _persist_and_respond(
            c,
            body_message=message,
            session_id=session_id,
            request_id=request_id,
            reserve_id=reserve_id,
            est=est,
            quota_left=quota_left,
            result=result,
            t0=t0,
        )
    except Exception as e:
        yield _sse("error", {"message": str(e)})
        return

    # 补发带 route 的 meta（early meta 时尚不知路由）
    yield _sse(
        "meta",
        {
            "session_id": resp.session_id,
            "route": resp.route,
            "request_id": resp.request_id,
            "llm_calls": resp.llm_calls,
            "abnormal_tags": resp.abnormal_tags,
            "early": False,
        },
    )

    # 非 travel 真流式：补推块；travel 已推过则不再重复全文
    if not streamed_any:
        for chunk in iter_plain_chunks(resp.answer, size=28):
            yield _sse("token", {"text": chunk})
            await asyncio.sleep(0.012)

    if resp.images:
        yield _sse("images", {"images": resp.images})

    yield _sse(
        "done",
        {
            "session_id": resp.session_id,
            "route": resp.route,
            "request_id": resp.request_id,
            "answer": resp.answer,
            "images": resp.images,
            "usage": resp.usage.model_dump(),
            "latency_ms": event.get("latency_ms"),
        },
    )

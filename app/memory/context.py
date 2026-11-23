"""STM 上下文拼装：滑动窗口 + 滚动摘要注入。"""

from __future__ import annotations

from typing import Any


SUMMARY_SYSTEM = """你是会话记忆压缩器。把旧对话压成一段简短中文摘要，供后续轮次使用。
只保留已确定的客观事实：目的地/景点、出行偏好与约束、关键已确认信息。
不要保留未决问题、待澄清事项或「是否还需要…」类开放问句；未回答的历史问题直接遗弃，禁止写入摘要，以免后续轮次强行补答。
不要复述闲聊；不要编造；不要列表堆砌。
只输出摘要正文，不要标题或 JSON。"""

# 注入千问时：历史与本轮硬分隔
LATEST_USER_SEP = "=== 用户最新提问 ==="
LATEST_FOCUS_HINT = "你的首要任务是回答最新提问，禁止重提历史已答问题。"


def window_message_limit(turns: int) -> int:
    """N 轮 → 最多 2N 条 message（user+assistant）。"""
    return max(1, int(turns)) * 2


def stm_recent_messages(
    messages: list[dict[str, str]] | None,
    *,
    turns: int,
) -> list[dict[str, str]]:
    msgs = list(messages or [])
    limit = window_message_limit(turns)
    return msgs[-limit:]


def _clip(text: str, max_chars: int) -> str:
    t = (text or "").strip()
    if max_chars > 0 and len(t) > max_chars:
        return t[: max_chars - 1].rstrip() + "…"
    return t


def format_ltm_items(
    ltm: list[dict[str, Any]] | None,
    *,
    max_items: int = 5,
    max_chars: int = 400,
) -> str:
    items = list(ltm or [])[:max_items]
    if not items:
        return ""
    lines: list[str] = []
    used = 0
    for it in items:
        if isinstance(it, dict):
            bit = str(it.get("content") or it.get("text") or it.get("value") or "").strip()
            typ = str(it.get("type") or it.get("key") or "").strip()
            line = f"- {typ}: {bit}" if typ and bit else (bit or str(it))
        else:
            line = str(it)
        line = line.strip()
        if not line:
            continue
        if used + len(line) > max_chars:
            remain = max_chars - used
            if remain > 8:
                lines.append(_clip(line, remain))
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


def format_recent_dialogue(
    messages: list[dict[str, str]] | None,
    *,
    per_msg_chars: int = 240,
    user_only: bool = False,
) -> str:
    lines: list[str] = []
    for m in list(messages or []):
        role = str(m.get("role") or "").strip().lower()
        if user_only and role != "user":
            continue
        content = str(m.get("content") or "").strip()
        if not content:
            continue
        label = "用户" if role == "user" else ("助手" if role == "assistant" else role or "未知")
        lines.append(f"{label}：{_clip(content, per_msg_chars)}")
    return "\n".join(lines)


def build_llm_context_payload(
    *,
    message: str,
    summary: str = "",
    recent: list[dict[str, str]] | None = None,
    ltm: list[dict[str, Any]] | None = None,
    slots: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """供 Ingest / Chat 等 JSON user 载荷：LTM + summary + recent + message。"""
    payload: dict[str, Any] = {
        "ltm": list(ltm or []),
        "summary": (summary or "").strip(),
        "recent": list(recent or []),
        "message": message,
    }
    if slots is not None:
        payload["slots"] = slots
    return payload


def build_llm_context_text(
    *,
    message: str,
    summary: str = "",
    recent: list[dict[str, str]] | None = None,
    ltm: list[dict[str, Any]] | None = None,
    recent_user_only: bool = False,
) -> str:
    """供 Travel / Aviation 等纯文本 user 前缀。"""
    parts: list[str] = []
    ltm_block = format_ltm_items(ltm)
    if ltm_block:
        parts.append(f"[LTM 偏好]\n{ltm_block}")
    summ = (summary or "").strip()
    if summ:
        parts.append(f"[会话摘要]\n{summ}")
    recent_block = format_recent_dialogue(recent, user_only=recent_user_only)
    if recent_block:
        parts.append(f"[最近对话]\n{recent_block}")
    # 硬分隔：历史仅供指代；作答必须以分隔符后的最新提问为准
    parts.append(f"{LATEST_USER_SEP}\n{(message or '').strip()}")
    parts.append(LATEST_FOCUS_HINT)
    return "\n\n".join(parts)


def format_messages_for_summary(messages: list[dict[str, str]], *, per_msg_chars: int = 200) -> str:
    return format_recent_dialogue(messages, per_msg_chars=per_msg_chars)


def clamp_summary(text: str, max_chars: int) -> str:
    return _clip((text or "").strip(), max_chars if max_chars > 0 else 0)

"""非咨询闲聊支线：与用户轻松对话；不承担路由/槽位提取。"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from app.config import get_settings
from app.llm.qwen import QwenClient, QwenError
from app.memory.context import build_llm_context_payload
from app.models import SubgraphResult

TokenCallback = Callable[[str], Awaitable[None] | None]

CHAT_SYSTEM = """你是航途小程序出行助手「途途」的陪聊一面，负责轻松闲聊：寒暄、心情、旅途心情等非硬咨询话题。
你的首要任务是回答最新提问（message 字段），禁止重提历史已答问题；结合用户偏好（LTM）、会话摘要与最近对话仅理解指代。
回答简短自然、口语化。不要复述内部字段名（如 LTM、slots、summary）。

【自我介绍】
当用户打招呼、问助手身份或能力时，只用亲切口吻说明职责，可参考（勿死记）：
「你好呀，我是途途～平时陪你聊聊天、解解闷；真正要问景点行程或机场出行指引时，也随时跟我说，我帮你对接旅游和机场方面的咨询。」
本轮只介绍能力边界；不要借机推荐具体景点、美食或城市攻略。日常追问不必反复自我介绍。

边界：
- 不提供医疗/法律/投资建议；不扮演航司或机场官方。
- 若用户转谈具体行程规划、景点攻略或机场航司政策，亲切引导：「你直接说想去哪玩、或哪个机场要办什么，我按旅游/机场咨询来帮你。」不要硬编攻略或政策细节。
- 禁止虚构工具调用或检索结果。

安全（必须遵守）：
- 不讨论政治、意识形态、敏感时政、领导人评价、国家/民族对立等话题。
- 不涉及色情、暴力教唆、违法犯罪、自残方式、仇恨歧视等有害内容。
- 用户若触碰上述话题：简短婉拒并自然转回旅行、生活或轻松闲聊，不展开、不站队、不输出相关细节。"""


async def answer_chat(
    query: str,
    *,
    qwen: QwenClient,
    on_token: TokenCallback | None = None,
    history: list[dict[str, str]] | None = None,
    stm_summary: str = "",
    ltm_context: list[dict[str, Any]] | None = None,
) -> SubgraphResult:
    """闲聊收尾 LLM。失败时由调用方降级话术。"""
    user = json.dumps(
        build_llm_context_payload(
            message=query,
            summary=stm_summary or "",
            recent=list(history or []),
            ltm=list(ltm_context or []),
        ),
        ensure_ascii=False,
    )
    flags = ["chat_reply_llm"]
    max_tokens = max(64, int(get_settings().qwen_chat_max_tokens))
    try:
        if on_token is not None:
            content, _pin, _pout = await qwen.chat_stream(
                system=CHAT_SYSTEM,
                user=user,
                temperature=0.7,
                max_tokens=max_tokens,
                on_token=on_token,
            )
        else:
            content, _pin, _pout = await qwen.chat(
                system=CHAT_SYSTEM,
                user=user,
                temperature=0.7,
                max_tokens=max_tokens,
                json_mode=False,
            )
        answer = (content or "").strip() or "嗯，我在呢，想聊点什么都行。"
        return SubgraphResult(answer=answer, confidence=0.7, domain_flags=flags)
    except QwenError:
        return SubgraphResult(
            answer="对话服务暂时不可用，请稍后再试。",
            confidence=0.0,
            domain_flags=flags + ["llm_unavailable", "out_of_scope"],
        )

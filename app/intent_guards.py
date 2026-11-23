"""硬规则意图门控：同会话非追问（身份/能力）不进旅游检索。"""

from __future__ import annotations

import re

# 结构：短句 + 助手主语 + 身份/能力谓语；或纯招呼
_ASSISTANT_META_RE = re.compile(
    r"(?:你|您|途途)"
    r"(?:"
    r"是谁|叫什么|的名字|"
    r"(?:是)?(?:干嘛|做什么|干什么|干啥)的|"
    r"(?:都)?(?:能|可以|会)(?:做|干|帮)(?:什么|啥)|"
    r"自己|是(?:什么)?助手|是机器人吗"
    r")"
    r"|"
    r"(?:介绍一下)?(?:你|您)自己|自我介绍|"
    r"有什么功能|你会什么",
    re.IGNORECASE,
)

_GREETING_ONLY_RE = re.compile(
    r"^(?:你好|您好|嗨|哈喽|hello|hi|hey)(?:啊|呀|呢|么|嘛|！|!|～|~|\s)*$",
    re.IGNORECASE,
)


def is_greeting_only(text: str) -> bool:
    """纯招呼（你好/hi…），可短路跳过 Ingest LLM。"""
    q = (text or "").strip()
    if not q or len(q) > 48:
        return False
    return bool(_GREETING_ONLY_RE.match(q))


def is_identity_or_capability_query(text: str) -> bool:
    """身份 / 能力 / 纯招呼 → 强制 chat，禁止 travel enrich/检索。"""
    q = (text or "").strip()
    if not q or len(q) > 48:
        return False
    if is_greeting_only(q):
        return True
    return bool(_ASSISTANT_META_RE.search(q))

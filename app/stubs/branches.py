from __future__ import annotations

from app.models import SubgraphResult


def aviation_stub(query: str) -> SubgraphResult:
    realtime_hints = ("航班", "延误", "登机口", "起飞", "降落", "准点")
    flags: list[str] = []
    if any(h in query for h in realtime_hints):
        flags.append("realtime_claim")
        answer = (
            "【航司支线 P0】当前不支持实时航班/登机口/延误查询。"
            "一期仅规划静态机场航司政策与设施问答（P2 RAG）。请改问行李额、值机、航站楼交通等。"
        )
    else:
        answer = (
            "【航司支线 P0 stub】机场航司静态 RAG 尚未接入。"
            f"已收到问题：「{query[:80]}」。P2 将上线检索与无 code 三态策略。"
        )
        flags.append("low_rag")
    return SubgraphResult(
        answer=answer,
        entities=[],
        constraints=[],
        sources=[],
        confidence=0.4,
        domain_flags=flags,
    )

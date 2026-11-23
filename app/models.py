from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


RouteName = Literal["travel", "aviation", "chat", "mixed"]


class Entity(BaseModel):
    type: str
    code: str | None = None
    name: str


class Constraint(BaseModel):
    kind: Literal["time", "location", "policy"]
    key: str
    value: str


class Source(BaseModel):
    chunk_id: str | None = None
    source_url: str | None = None
    effective_at: str | None = None


class SubgraphResult(BaseModel):
    answer: str = ""
    entities: list[Entity] = Field(default_factory=list)
    constraints: list[Constraint] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    confidence: float = 0.5
    domain_flags: list[str] = Field(default_factory=list)
    # 旅游支线等：结构化图片，前端/小程序直接渲染，勿当链接正文
    images: list[dict[str, Any]] = Field(default_factory=list)
    # 写回 STM slots（如 photos_shown）；编排 finalize 合并
    slots_patch: dict[str, Any] = Field(default_factory=dict)


class MemoryItem(BaseModel):
    type: str
    key: str
    value: str
    confidence: float = 0.0


class ChatRequest(BaseModel):
    session_id: str | None = None
    user_token: str
    message: str


class UsageOut(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    est_tokens: int = 0
    actual_tokens: int = 0


class ChatResponse(BaseModel):
    session_id: str
    answer: str
    route: str | None = None
    usage: UsageOut
    validation_pass: bool = True
    llm_calls: int = 0
    abnormal_tags: list[str] = Field(default_factory=list)
    request_id: str
    reserve_id: str | None = None
    images: list[dict[str, Any]] = Field(default_factory=list)


class SessionCreateRequest(BaseModel):
    user_token: str


class TravelQueryRequest(BaseModel):
    """旅游支线直连接口：不走完整 chat 编排，便于对比旧/新知识库。"""

    query: str = Field(..., min_length=1, description="景点/城市相关问题")
    limit: int = Field(default=6, ge=1, le=20)


class TravelQueryResponse(BaseModel):
    knowledge_source: Literal["legacy", "enriched"]
    answer: str
    hits: list[dict[str, Any]] = Field(default_factory=list)
    images: list[dict[str, str]] = Field(
        default_factory=list,
        description="命中景点已有照片（cover/gallery URL），便于前端直接展示",
    )
    confidence: float = 0.0
    domain_flags: list[str] = Field(default_factory=list)


class AirportServiceQueryRequest(BaseModel):
    """机场服务直连接口：按 airport_code + category 查表；可选 query 用于向量锚定排序。"""

    airport_code: str = Field(..., min_length=3, max_length=4, description="IATA，如 LJG")
    category: str = Field(
        ...,
        min_length=1,
        description="transport|navigation|checkin|facility|lounge|emergency|policy",
    )
    query: str = Field(
        default="",
        description="用户原问或关键词；details 不足时用于 guide_chunk_ids 锚定检索",
    )


class AirportServiceQueryResponse(BaseModel):
    ok: bool
    airport_code: str | None = None
    category: str | None = None
    service_count: int = 0
    markdown: str = ""
    guide_chunk_ids: list[str] = Field(default_factory=list)
    rag_mode: str | None = None
    rag_hit_count: int = 0
    error: str | None = None


class ApiError(BaseModel):
    code: str
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)

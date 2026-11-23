from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # utf-8-sig：兼容 Windows 编辑器写入的 BOM。若用纯 utf-8，BOM 会被并入
    # 第一个键名（\ufeffDASHSCOPE_API_KEY），导致 API KEY 静默读不到、上线后 LLM 全线 401。
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8-sig", extra="ignore")

    app_host: str = "0.0.0.0"
    app_port: int = 8080
    log_level: str = "INFO"

    dashscope_api_key: str = ""
    qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    qwen_chat_model: str = "qwen-flash"
    # Ingest / STM 摘要用更轻量模型（可与主答分离）
    qwen_ingest_model: str = "qwen-turbo"
    # 出口 token 上限（严格）
    qwen_ingest_max_tokens: int = 256
    qwen_chat_max_tokens: int = 220
    qwen_travel_reply_max_tokens: int = 560
    qwen_travel_guide_max_tokens: int = 900
    qwen_aviation_max_tokens: int = 480
    qwen_summary_max_tokens: int = 256

    redis_url: str = "redis://localhost:6379/0"
    database_url: str = "postgresql://qa:qa@localhost:5432/travel_qa"

    quota_daily_tokens: int = 200_000
    quota_monthly_tokens: int = 2_000_000
    quota_session_turns: int = 50
    quota_reserve_ttl_sec: int = 60
    quota_est_default: int = 1200

    route_confidence_threshold: float = 0.55
    auth_mode: str = "mock"
    admin_token: str = "dev-admin"

    # STM：滑动窗口 + 溢出摘要（轮 = user+assistant 一对；注入 LLM 默认最近 6 轮）
    stm_window_turns: int = 6
    stm_summary_max_chars: int = 300
    stm_persist_max_turns: int = 16
    llm_budget_per_turn: int = 3

    data_dir: str = "data"
    # 旅游支线默认知识源：legacy=文件旧库；enriched=Postgres attraction_kb（正式路径）
    travel_knowledge_source: str = "legacy"

    # 高德 Web 服务（路径 / 距离 / POI）
    amap_api_key: str = ""
    amap_base_url: str = "https://restapi.amap.com"
    amap_distance_path: str = "/v3/distance"
    amap_direction_driving_path: str = "/v3/direction/driving"
    amap_direction_walking_path: str = "/v3/direction/walking"
    amap_place_text_path: str = "/v3/place/text"
    amap_place_around_path: str = "/v3/place/around"

    # 和风天气 QWeather（自定义 API Host + API KEY；日报路径 /weather/v1/daily/{lat}/{lon}）
    qweather_api_key: str = ""
    qweather_base_url: str = ""
    qweather_daily_path: str = "/weather/v1/daily"
    qweather_timeout_sec: float = 4.0
    qweather_cache_ttl_sec: int = 1800
    qweather_max_cities: int = 4

    # P2 Qdrant（travel_rag / aviation_rag 分 Collection）
    qdrant_url: str = "http://127.0.0.1:6333"
    qdrant_api_key: str = ""
    qdrant_travel_collection: str = "travel_rag"
    qdrant_aviation_collection: str = "aviation_rag"
    qdrant_vector_size: int = 1024
    # 向量模型（≠ QWEN_CHAT_MODEL）；换模型/维度后需重建 Collection 并全量重灌
    qwen_embed_model: str = "text-embedding-v3"
    rag_top_k_anchored: int = 3
    rag_top_k_degraded: int = 5
    # 切块（改后需重跑 ingest / upsert）
    rag_chunk_size: int = 512
    rag_chunk_overlap: int = 64
    # Batch 4 预留（检索侧尚未接线）
    rag_bm25_top_k: int = 20
    rag_vector_top_k: int = 20
    rag_rrf_k: int = 60
    rag_rerank_top_n: int = 15
    rag_rerank_keep: int = 5
    rag_rerank_enabled: bool = False
    rag_rerank_min_candidates: int = 8
    rag_rerank_model: str = "bge-reranker-large"
    qwen_temperature: float = 0.15


@lru_cache
def get_settings() -> Settings:
    return Settings()

from __future__ import annotations

from typing import Any

import redis

from app.config import Settings, get_settings
from app.db import Database
from app.graph.pipeline import Orchestrator
from app.llm.qwen import QwenClient
from app.memory.ltm_mock import MockLtmAdapter
from app.memory.stm import SessionStore
from app.quota.service import QuotaService
from app.telemetry import Telemetry


class AppContainer:
    def __init__(self) -> None:
        self.settings: Settings = get_settings()
        self.redis = redis.Redis.from_url(self.settings.redis_url, decode_responses=True)
        self.quota = QuotaService(self.redis, self.settings)
        self.db = Database(self.settings)
        # Session registry / message list still on Postgres tables; graph STM is PostgresSaver.
        self.stm = SessionStore(self.db)
        self.ltm = MockLtmAdapter(self.settings.data_dir)
        self.telemetry = Telemetry(self.settings.data_dir)
        self.qwen = QwenClient(self.settings)
        self.checkpointer: Any | None = None
        self.orchestrator = Orchestrator(self.settings, self.qwen, checkpointer=None)

    def bind_checkpointer(self, checkpointer: Any) -> None:
        self.checkpointer = checkpointer
        self.orchestrator = Orchestrator(self.settings, self.qwen, checkpointer=checkpointer)


_container: AppContainer | None = None


def get_container() -> AppContainer:
    global _container
    if _container is None:
        _container = AppContainer()
    return _container

# airline_travel_agent（P0 完成 · P1.1 进行中）

航空/旅行问答编排服务。架构规格见同级 [`travel-qa-agent/docs/architecture.md`](../travel-qa-agent/docs/architecture.md)。

| 项 | 定案 |
|----|------|
| 对话模型 | 通义千问 **`qwen-flash`** |
| STM / 会话库 | **Postgres only**（Docker 内） |
| Travel | **P1.1 知识库检索已接入**（灵犀导出 JSON） |
| Aviation | 仍 stub（P2） |
| LTM | Mock Adapter |

> 原目录名 `travel-qa-service` 已弃用，请使用本目录 `airline_travel_agent`。

## 契约

见 [`contracts/v0.1/`](contracts/v0.1/)。

## 启动（推荐 Docker Compose）

```bash
cd d:\Projects\airline_travel_agent
copy .env.example .env
# 编辑 .env：填入 DASHSCOPE_API_KEY；Compose 内保持
# REDIS_URL=redis://redis:6379/0
# DATABASE_URL=postgresql://qa:qa@postgres:5432/travel_qa
# QWEN_CHAT_MODEL=qwen-flash

docker compose up --build
```

- API: http://127.0.0.1:8080/docs  
- 鉴权 mock：`user_token` = `uid:<user_id>`

若在**容器外**跑 uvicorn，请把 `.env` 改成：

```text
REDIS_URL=redis://127.0.0.1:6379/0
DATABASE_URL=postgresql://qa:qa@127.0.0.1:5432/travel_qa
```

并确保 Compose 里的 `postgres`、`redis` 已启动。

## 验收对照

两 uid × 两 session 不串；配额 429；闲聊 `llm_calls`≈2；模型为 flash；STM 落在 Postgres `sessions.stm_state`。

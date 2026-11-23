# P0 验收对照表（按模块清单）

> 日期：2026-11-09  
> 仓库：`airline_travel_agent`  
> 说明：`[PASS]` 已满足；`[NOTE]` 与清单字面略有差异但可接受或已演进

## 一、基础设施与工程底座

| 项 | 状态 | 说明 |
|----|------|------|
| docker-compose 一键拉起 api/postgres/redis | PASS | 三服务均在容器内；干净机需先拉镜像（DaoCloud tag） |
| 密钥仅 .env 注入 | PASS | 业务代码无硬编码 Key；`config.py` 仅有 localhost **默认值**，Compose 用环境变量覆盖 |
| 外部 HTTP 显式 Timeout | PASS | 千问 `httpx.AsyncClient(timeout=20)`（P1 容错下调）；小程序后端尚未实连 |

## 二、状态存储与会话隔离

| 项 | 状态 | 说明 |
|----|------|------|
| STM 使用 LangGraph **PostgresSaver** | PASS | `AsyncPostgresSaver` + `thread_id=session_id`；启动时 `setup()` |
| api 重启后会话上下文仍在 | PASS | 验收脚本 `scripts/p0_accept_restart.py` |
| 新 session_id 上下文清空；同 uid 可注入 LTM Mock | PASS | 验收脚本 `scripts/p0_accept_sessions.py` |

## 三、配额与并发控制

| 项 | 状态 | 说明 |
|----|------|------|
| Lua check_and_reserve / commit / release | PASS | 另含 `reserves:deadline` + `SWEEP_EXPIRED` |
| 并发不超卖 | PASS | `scripts/p0_accept_quota.py` |
| 异常中断靠 TTL 自动退额 | PASS | 后台 sweeper（5s）+ admin `/v1/admin/quota/sweep` |

## 四、接口契约与 Mock 隔离

| 项 | 状态 | 说明 |
|----|------|------|
| 契约 v0.1 + MockMemoryAdapter | PASS | `contracts/v0.1` + `MockLtmAdapter` |
| Stub/子图返回完整 SubgraphResult | PASS | aviation stub / travel 检索均返回完整结构 |

## 五、模型调用与业务边界

| 项 | 状态 | 说明 |
|----|------|------|
| IngestRoute 合并；MemoryWriter/摘要不接真 LLM | PASS | |
| llm_calls ≤ 3（闲聊典型 2） | PASS | 已冒烟验证 |
| Travel/Aviation 全 stub | NOTE | **P1.1 已将 Travel 换成知识库真检索**（仍无外部天气工具）；Aviation 仍 stub |
| 无 uid / 配额 / 低置信 / 实时航班降级 | PASS | 401/429/澄清话术/realtime 边界 |

## 验收命令

```powershell
python scripts/p0_accept_quota.py
python scripts/p0_accept_sessions.py
python scripts/p0_accept_restart.py
```

## 干净机一键跑通备注

1. 配置 `.env`（千问 Key）  
2. 若 Docker Hub 慢：先 `scripts/pull-images.ps1`（DaoCloud pull + tag）  
3. `docker compose up -d --build`  

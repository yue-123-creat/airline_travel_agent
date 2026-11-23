# 契约 v0.1（P0 冻结）

本目录为跨团队接口形状冻结版。小程序后端可事后对齐；**Mock → 真实现只要不改形状，不算架构返工**。

| 文件 | 内容 |
|------|------|
| [gateway-api.md](gateway-api.md) | 小程序 ↔ Gateway：`/v1/chat`、sessions、错误码 |
| [ltm.md](ltm.md) | LTM 增量写 + version 乐观锁；Adapter 模式 |
| [quota.md](quota.md) | Redis 预扣/commit/release 语义 |
| [subgraph-result.md](subgraph-result.md) | 支线统一输出 `SubgraphResult` |
| [telemetry.md](telemetry.md) | 每轮观测必打字段 |

版本：`0.1.0` · 日期：2026-11-09

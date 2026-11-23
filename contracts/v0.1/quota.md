# 配额契约 v0.1

真相源：**Redis**。Postgres `usage_ledger` 异步最终一致。

## Lua：`check_and_reserve`

入参：`uid, session_id, est_tokens, daily_limit, monthly_limit, session_turn_limit`

同脚本内：

1. 读日/月已用与 session 轮次  
2. 超限则返回错误码，不写预扣  
3. 否则扣减预留、session 轮次 +1，写 `reserve:{id}`

成功返回：`reserve_id`。

失败码：`quota_daily` | `quota_monthly` | `quota_session_turns`

## 预扣记录

```text
reserve:{reserve_id} = { uid, session_id, est, status: pending|committed|released, created_at }
TTL = 60s（可配）
```

超时 pending → 清扫 `release`（幂等）。

## `commit(reserve_id, actual)`

- `actual < est`：差额退回余额  
- status → `committed`  
- 幂等

## `release(reserve_id)`

- 全额退回 `est`  
- status → `released`  
- 幂等

## HTTP 映射

预扣失败 → Gateway **429** + 对应 `code`。

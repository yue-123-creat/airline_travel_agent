# Telemetry v0.1

每轮一条 JSON（jsonl），字段：

```text
ts, request_id, user_id, session_id
route, route_confidence
llm_calls, llm_call_breakdown
tokens_in, tokens_out, quota_left
reserve_id, est_tokens, actual_tokens, quota_op
validate_pass
abnormal_tags[]
latency_ms
```

P0 落盘：`data/logs/YYYY-MM-DD.jsonl`。

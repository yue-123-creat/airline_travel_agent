# SubgraphResult v0.1

所有支线（含 stub）必须产出：

```json
{
  "answer": "string",
  "entities": [
    { "type": "airport|city|poi|airline|terminal|departure|destination", "code": "PVG", "name": "浦东" }
  ],
  "constraints": [
    { "kind": "time|location|policy", "key": "string", "value": "string" }
  ],
  "sources": [
    { "chunk_id": null, "source_url": null, "effective_at": null }
  ],
  "confidence": 0.8,
  "domain_flags": []
}
```

`domain_flags` 枚举：`realtime_claim` | `out_of_scope` | `low_rag`

P0：Travel/Aviation stub 可返回固定说明文案 + 空 entities；Chat 真模型填充 `answer`。

# LTM 契约 v0.1

权威存储：小程序用户库。本服务经 `MemoryAdapter` 访问；P0 使用 **Mock Adapter**（进程内/文件），形状与下文一致。

## MemoryItem（增量 patch 单元）

```json
{
  "type": "preference",
  "key": "seat_pref",
  "value": "靠窗",
  "confidence": 0.9
}
```

- 写入 **只提交增量**，禁止默认全量画像覆盖。
- `confidence < 0.7` 由 Adapter **硬丢弃**。
- 白名单外 `type`/`key` 硬丢弃。

## 读

`GET /ltm/{user_id}`（逻辑接口；Mock 本地实现）

```json
{
  "user_id": "u_demo",
  "version": 3,
  "items": [ { "type": "preference", "key": "seat_pref", "value": "靠窗", "confidence": 0.9 } ]
}
```

## 写（默认模式 A：增量 + 版本）

`PATCH /ltm/{user_id}`

```json
{
  "version": 3,
  "patch": [ { "type": "preference", "key": "seat_pref", "value": "靠窗", "confidence": 0.9 } ]
}
```

- 同 `key`：较高 `confidence` 覆盖。
- `version` 不匹配 → **409** `ltm_version_conflict`；调用方重读合并重试（乐观锁）。
- 成功返回新 `version` 与合并后 `items`。

## 模式

| 模式 | 说明 |
|------|------|
| A | 增量 patch + version（**P0 Mock 默认**） |
| B | 仅全量覆盖：Load → merge → PUT 带 version，冲突重试 |
| C | 条件写（If-Match / etag），冲突同乐观锁 |

P0 Mock 实现模式 A；换真小程序 API 时保持请求/响应字段名稳定。

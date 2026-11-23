# Gateway API v0.1

Base：`/v1`  
鉴权（P0 mock）：`Authorization: Bearer uid:<user_id>` 或 body.`user_token` = `uid:<user_id>`。

## POST `/v1/chat`

### Request

```json
{
  "session_id": "s_xxx",
  "user_token": "uid:u_demo",
  "message": "你好"
}
```

- `session_id`：**产品主路径必填**（方案 A）。前端先 `POST /v1/sessions` 再 chat；续聊 / 切历史均带同一或选定 id。详见 [`docs/miniprog-session-contract.md`](../../docs/miniprog-session-contract.md)。
- 兼容：若省略 `session_id`，服务端仍会创建新会话（旧方案 B）；**小程序正式路径勿依赖**。
- `message`：用户本轮输入，非空。

### Response 200

```json
{
  "session_id": "s_xxx",
  "answer": "...",
  "route": "chat",
  "usage": {
    "prompt_tokens": 100,
    "completion_tokens": 50,
    "est_tokens": 1200,
    "actual_tokens": 150
  },
  "validation_pass": true,
  "llm_calls": 2,
  "abnormal_tags": [],
  "request_id": "req_xxx",
  "reserve_id": "rsv_xxx"
}
```

生产可对小程序隐藏 `route` / `abnormal_tags`；契约形状保留。

### Errors

| HTTP | code | 含义 |
|------|------|------|
| 401 | `unauthorized` | token 无效 / 无 uid |
| 404 | `session_not_found` | session 不存在或不属于该用户 |
| 429 | `quota_daily` / `quota_monthly` / `quota_session_turns` | 配额或轮次超限 |
| 503 | `llm_unavailable` | 千问调用失败 |
| 500 | `internal_error` | 其他 |

## POST `/v1/chat/stream`

与 `/v1/chat` **同一请求体**（方案 A：`session_id` 产品路径必填）。

- `Content-Type: application/json`（请求）
- 响应：`Content-Type: text/event-stream`
- 每帧形如：

```text
event: <name>
data: <json>

```

### 事件表

| event | 何时 | data 主要字段 | 前端建议 |
|-------|------|---------------|----------|
| `status` | 几乎最先 | `phase`, `message` | 状态栏文案；可忽略 |
| `error` | 任意失败点；其后**不再**有 `done` | `message`；常有 `status`、`code` | 结束本轮；按 `status`/`code` 提示（429/404 等）；**勿换 session 除非 404** |
| `meta` | 首个 `token` 前，或非流式收尾前补发 | `session_id`, `route`, `request_id`；真流式时另有 `streaming: true`；非流式补发可含 `llm_calls`, `abnormal_tags` | **写回** `session_id`；`route` 可调试展示 |
| `token` | 正文增量（可多次） | `text` | 追加到当前助手气泡；含 `![]()` 时边收边解析（见小程序图文对齐文档） |
| `images` | 有图时、`done` 前 | `images`: `[{...}]` | 可缓存；**以 `done.images` / `done.answer` 为准**做最终渲染 |
| `done` | 成功收尾（每轮至多一次） | `session_id`, `route`, `request_id`, `answer`, `images`, `usage`, `latency_ms` | 用 `answer` 覆盖/定稿正文；写回 `session_id`；本轮结束 |

### 典型顺序

**真流式（如 travel 出 token）**

```text
status → meta → token* → [images] → done
```

**非真流式（chat/aviation 等：结束后块推 token）**

```text
status → meta → token*（块模拟） → [images] → done
```

**失败（鉴权/配额/异常）**

```text
status → error
```
或运行中：`status → … → error`（无 `done`）。

### 约定

- 成功结束以 **`done`** 为准（不是 `final`）。
- `done.answer` 为完整定稿；流式 `token` 拼接结果应与之对齐，冲突时以 `done` 为准。
- 小程序部署细则与 Checklist：[`docs/miniprog-session-contract.md`](../../docs/miniprog-session-contract.md)。

## GET `/v1/sessions?user_token=uid:...`

返回该用户**非空**会话列表（至少有一条 message 才出现；刚 `POST /v1/sessions` 未发言的不进列表）：

```json
[{
  "session_id": "s_xxx",
  "created_at": "...",
  "updated_at": "...",
  "preview": "用户首句摘要…"
}]
```

- `preview`：该会话**第一条用户消息**截断（约 40 字），供历史列表展示。  
- 排序：`updated_at` 倒序。

## POST `/v1/sessions`

```json
{ "user_token": "uid:u_demo" }
```

创建新会话（新 `session_id`，STM 空）。

**方案 A（产品主路径）**：进入聊天页且本地无当前 id、以及用户点「新对话」时调用；拿到 id 后再发 chat。不要用「省略 id 的 chat」冒充新开。

## GET `/v1/sessions/{session_id}`

返回消息历史（需同用户）。Query：`user_token`。

## DELETE `/v1/sessions/{session_id}`

删除会话及消息。

## GET `/v1/admin/logs`

P0：返回近期 jsonl 尾部（开发用）。Header 需 `X-Admin-Token`（环境变量，默认 `dev-admin`）。

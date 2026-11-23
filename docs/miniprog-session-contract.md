# 小程序部署对齐说明 · 会话契约（方案 A）

> **结论先说**：产品主路径采用**方案 A**——开会话与发消息拆开。  
> - 进入聊天页 / 点「新对话」→ **`POST /v1/sessions`** 拿 `session_id`  
> - 用户每一轮说话 → **`POST /v1/chat` 或 `/v1/chat/stream`**，**必须带**当前 `session_id`  
> Agent 侧用该 `session_id` 作为 LangGraph `thread_id` 绑定 STM；前端不要用微信 `openid` 当会话 id。

相关网关契约：[`contracts/v0.1/gateway-api.md`](../contracts/v0.1/gateway-api.md)。  
图文渲染另见 [`miniprog-travel-reply-align.md`](./miniprog-travel-reply-align.md)。

---

## 1. 概念（部署前先对齐）

| 概念 | 含义 | 前端怎么用 |
|------|------|------------|
| `user_token` / 用户 ID | 标识「人」 | 登录后持有；所有 sessions/chat 请求都带 |
| `session_id` | 标识「这一条对话线」 | 本地持久化为「当前会话」；续聊原样回传 |
| 微信 `openid` | 小程序用户标识 | 只用于登录换你们后端的 uid/token，**禁止**当 `session_id` |

- **同会话多轮**：`session_id` 不变 → STM 连续。  
- **新对话**：必须新 `session_id` → STM 空。  
- **切回历史**：把当前 id 换成旧 id → 可继续聊。

### 1.1 三态已拍板

| 项 | 决定 |
|----|------|
| 重进聊天页 | **恢复上次**：本地有 `current_session_id` 则复用，并建议拉 messages 渲染 |
| 空会话与历史列表 | **不出现**：仅 `POST /v1/sessions`、尚未发言的会话不进 `GET /v1/sessions` |
| 历史项展示 | **首句摘要**：列表项用后端字段 `preview`（该会话第一条用户消息截断） |

---

## 2. 必须实现的前端流程（方案 A）

### 2.1 进入聊天页（恢复上次）

```text
若本地有有效的 current_session_id
  → 复用
  → GET /v1/sessions/{id} 拉历史渲染（有消息则回放气泡；无消息则空白欢迎）
否则
  → POST /v1/sessions
  → 保存返回的 session_id 为 current_session_id
  → UI 为空会话（可显示欢迎语，无历史气泡）
```

- 空会话可以是「当前正在聊」的线程，但**不会**出现在历史列表，直到用户发出至少一条消息。  
- **不要**在尚未拿到 id 时允许发出 chat（或发出时省略 id）。

### 2.2 用户发消息

```text
POST /v1/chat  或  POST /v1/chat/stream
body: { user_token, session_id: current_session_id, message }
```

- `session_id`：**必填**（产品路径）。  
- 响应 / SSE 结束包里的 `session_id`：写回本地（与请求一致则无感；若不一致以响应为准）。  
- 空 `message`：前端拦截，不请求。

### 2.3 点「新对话」

```text
POST /v1/sessions
→ 用新 session_id 覆盖 current_session_id
→ 清空当前气泡列表
→ 之后发消息仍走 chat，带新 id
```

不要用「省略 session_id 的 chat」冒充新开。

### 2.4 从历史会话继续聊

```text
GET /v1/sessions?user_token=...          → 非空会话列表，展示 preview（首句摘要）
→ 用户点选一条
GET /v1/sessions/{session_id}?user_token=... → 渲染完整 messages
→ current_session_id = 选中的 id
→ 之后发消息走 chat，带该 id
```

列表 UI：主文案用 `preview`，辅文案可用 `updated_at`；不要只展示裸 `session_id`（可作次要信息）。

### 2.5 删除会话（若产品有）

```text
DELETE /v1/sessions/{session_id}?user_token=...
若删的是当前会话 → 再走 2.1 / 2.3 拿新 id 或切到列表另一条
```

---

## 3. 本地存储约定（小程序）

建议 key（可按项目前缀调整）：

| Key | 内容 |
|-----|------|
| `ata_user_token` 或登录态 | 当前用户凭证 |
| `ata_session_id:${uid}` | 该用户当前会话 id（**按用户隔离**，避免换号串会话） |

规则：

- 登出 / 换号：清该用户的 `ata_session_id:*`（或整表会话缓存）。  
- 收到 `404 session_not_found`：清掉当前 id，提示失效，再 `POST /v1/sessions` 或回历史列表。  
- 刷新/杀进程重进：有本地 id 则复用；没有再创建。

预览页 `app/static/chat.html` 使用 `ata_session_id`（未按 uid 分 key）；**正式小程序请按 uid 分 key**。

---

## 4. 接口速查（前端只关心这些）

### 4.1 `POST /v1/sessions`

```json
{ "user_token": "uid:u_demo" }
```

返回：`{ "session_id": "s_xxx", "user_id": "..." }`  
用途：进页无会话、点「新对话」。

### 4.2 `POST /v1/chat`（同步）

```json
{
  "user_token": "uid:u_demo",
  "session_id": "s_xxx",
  "message": "你好"
}
```

返回含：`session_id`、`answer`、`images`、`request_id`、`usage` 等。

### 4.3 `POST /v1/chat/stream`（SSE，推荐主路径）

请求 body 与 `/v1/chat` 相同（方案 A：**必带** `session_id`）。  
响应：`text/event-stream`，帧格式 `event:` + `data:`（JSON）。完整契约见 [`contracts/v0.1/gateway-api.md`](../contracts/v0.1/gateway-api.md)「POST `/v1/chat/stream`」。

#### 流式事件表

| event | data | 前端动作 |
|-------|------|----------|
| `status` | `{ phase, message }` | 显示「生成中」类状态 |
| `meta` | `{ session_id, route, request_id, streaming? }` | **写回** `current_session_id`；可选展示 route |
| `token` | `{ text }` | 追加助手气泡；有 MD 图则边收边拆（见图文对齐文档） |
| `images` | `{ images: [...] }` | 缓存即可；最终以 `done` 为准 |
| `done` | `{ session_id, route, request_id, answer, images, usage, latency_ms }` | **定稿**：用 `answer`/`images` 最终渲染；再写回 `session_id`；结束本轮 |
| `error` | `{ message, status?, code? }` | 结束本轮并提示；无 `done`；404 才清/换会话 |

#### 顺序（联调对照）

```text
成功真流式:     status → meta → token* → [images] → done
成功块推:       status → meta → token* → [images] → done
失败:           status → error   （或中途 error，无 done）
```

#### 小程序注意

- 结束事件名是 **`done`**，不是 `final`。  
- 冲突时以 **`done.answer`** 覆盖 token 拼接结果。  
- 若运行时不好接 SSE：可暂用同步 `/v1/chat`，**会话三态契约不变**。  
- 预览页实现：`app/static/chat.html`（可对照事件分支）。

### 4.4 历史

- `GET /v1/sessions?user_token=...` →  
  `{ sessions: [{ session_id, created_at, updated_at, preview }] }`  
  - 仅含**至少一条 message** 的会话  
  - `preview`：首条 **user** 消息截断（约 40 字 + `…`）  
- `GET /v1/sessions/{session_id}?user_token=...` → `{ session, messages }`  

当前正在进行、尚未发言的空会话：不在列表中；仍可通过本地 `current_session_id` 续聊。

---

## 5. 错误与空态（小程序文案建议）

| HTTP / code | 前端行为 |
|-------------|----------|
| 401 `unauthorized` | 重新登录 |
| 404 `session_not_found` | 清当前 id；提示会话失效；新建或选历史 |
| 429 `quota_*` | 按日/月/会话轮次提示，勿自动新开会话刷配额 |
| 503 `llm_unavailable` | 可重试；**保持**同一 `session_id` |
| 流式中断 | 保留已上屏半条或标「生成中断」；重发仍用同一 id |

---

## 6. 部署 Checklist（接到小程序时勾选）

- [ ] 登录后持有稳定 `user_token`（或等价鉴权头）  
- [ ] 进聊天页：**恢复上次**（有本地 id 则复用并拉 messages；无则 `POST /v1/sessions`）  
- [ ] 发消息：chat/stream **始终带** `session_id`  
- [ ] 流式：按事件表处理 `status/meta/token/images/done/error`；以 `done` 定稿；勿期待 `final`  
- [ ] 「新对话」：只走 `POST /v1/sessions`，清 UI，换本地 id  
- [ ] 「历史」：用列表 `preview` 展示首句摘要；点选后拉 messages 并切换 id 续聊  
- [ ] 确认空会话不出现在历史（后端已过滤；前端勿本地拼空项）  
- [ ] `session_id` 按 uid 写入 storage；换号不串会话  
- [ ] 处理 404 / 429 / 503（见上表）  
- [ ] 图文气泡按 [`miniprog-travel-reply-align.md`](./miniprog-travel-reply-align.md) 解析  
- [ ] **禁止**用 `openid` / 随机本地 uuid 冒充服务端 `session_id`（除非先经 `POST /v1/sessions` 登记）  
- [ ] **禁止**产品路径上「省略 `session_id` 的 chat」当作新开（后端兼容不代表前端可用）

---

## 7. 与后端兼容说明（给联调）

服务端 `_ensure_session` 在 chat **省略** `session_id` 时仍会新建会话（旧方案 B）。  
**小程序正式路径不要依赖该行为**；方案 A 下省略 id 视为前端缺陷。

STM 绑定：`thread_id === session_id`（PostgresSaver）。前端只需保证 id 稳定回传即可。

---

## 8. 最小时序（便于画流程图）

```text
[进入聊天页 · 恢复上次]
    │
    ├─ 有 current_session_id ──► GET session/{id} 渲染 ──► 等待用户输入
    │
    └─ 无 ──► POST /v1/sessions ──► 存 id ──► 等待用户输入
                                              │
                                    用户发送 message（此后该会话才进历史列表）
                                              │
                                    POST /v1/chat(/stream) + session_id
                                              │
                                    展示回复；id 不变

[新对话] ──► POST /v1/sessions ──► 换 id、清 UI ──► 同上发消息
         （旧会话若已有消息仍留在历史；新空会话暂不进列表）

[选历史] ──► GET sessions（含 preview）→ 点选 → GET session/{id} ──► 换 id ──► 同上发消息
```

# v0.1 身份与鉴权契约（auth.md）

> 状态：**契约待定稿，实现待补**。本文用于对齐「正式版如何拿到可信的用户 id」，
> 不改动任何 `/v1/*` 已有请求 / 响应形状。小程序前端与后端老师按本文确认后，
> 再决定落地哪一条模式。
>
> 关联：`gateway-api.md`、`docs/miniprog-session-contract.md`、`quota.md`、`ltm.md`

---

## 1. 为什么必须换掉 mock

当前实现（`app/auth.py`）：

```python
def parse_user_token(user_token: str, auth_mode: str = "mock") -> str:
    if token.startswith("uid:"):
        return token[4:].strip()   # 没有任何验签 / 有效期 / 来源校验
```

`.env` 中是 `AUTH_MODE=mock`。这意味着线上任何人只要构造 `uid:<别人的 id>`，就能：

- `GET /v1/sessions?user_token=uid:xxx` 读取他人会话列表与消息；
- `DELETE /v1/sessions/{id}?user_token=uid:xxx` 删除他人会话；
- 用他人身份消耗 / 绕过配额，污染 LTM。

**结论：`AUTH_MODE=mock` 不得进入正式版。** 它只适用于开发版 / 体验版联调，
且联调期也应限制 uid 不可枚举、开启接口限频。

---

## 2. 三条不变原则（无论选哪种模式都成立）

| # | 原则 | 说明 |
|---|---|---|
| 1 | `user_token` 对前端是**不透明字符串** | 前端只负责「存取 + 原样透传」，**禁止自己拼 `uid:`**。这样以后换真实鉴权，前端零改动。 |
| 2 | `uid` 必须**稳定且不可枚举** | 同一个微信用户每次登录必须得到同一个 uid，否则会话、LTM、配额全部串号。禁止用自增数字 / 时间戳 / 明文 openid 当 uid。 |
| 3 | 身份只能由**服务端可信来源**产生 | `jscode2session` 换出的 openid，或上游网关鉴权后注入的头。客户端 body 里自报的身份一律不信任。 |

`uid` 的用途贯穿全链路：`/v1/sessions` 归属、LangGraph STM 的隔离键、
`quota.check_and_reserve(uid, ...)`、`ltm.get(uid)`、前端 `ata_session_id:${uid}` 本地缓存键。

---

## 3. 三种模式与选择

### 模式 A：`wechat`（标准做法，能拿到真正的微信用户 id）

```
小程序 wx.login() → code
  → POST /v1/auth/login { "code": "..." }
  → 服务端用 appid + appsecret 调 api.weixin.qq.com/sns/jscode2session
  → openid（+ unionid）
  → 稳定映射为 uid（hash 或 wx_user 表），签发带有效期、带签名的 user_token
  → 前端存 token，之后所有 /v1/* 原样透传
```

- 优点：身份可信、可防伪造；`user_token` 可过期、可吊销；正式版最稳妥。
- 代价：需要 `WX_APPID` / `WX_APP_SECRET`（向后端老师要），且**小程序需要发一版**
  在 `onLaunch` 接入登录与 401 重登。
- 适合：前端团队有排期、且你确实要在正式版里拿到 openid / 用户 id。

### 模式 B：`trusted`（网关注入，前端零改动）

上游 Nginx / 公司统一登录网关完成鉴权后，把可信 uid 放进请求头
（例如 `X-Trusted-Uid`），并**剥离**客户端可能伪造的同名头；agent 只信任该头。

- 优点：小程序不发版，不存在 code2session 依赖；切换当天即可生效。
- 前提：公司已有统一登录网关，且域名管理员愿意配合加这一条；
  同时 agent 必须只允许来自网关的内网 IP 访问（prod compose 已把 8080 绑到 `127.0.0.1`）。
- 适合：周五前必须进正式版、前端没排期的情况。

### 模式 C：兼容层透传（过渡，仅当 dify 的 `user` 本身可信）

若沿用「dify 兼容路由」方案，前端原本调用 `/v1/chat-messages` 时就会传 `user` 字段。
兼容层可把 `user` 映射成 agent 的 `uid`。

⚠️ 但 `user` 是客户端明文上报的，**本身就等同于 mock 的可伪造风险**。
只有在「请求已经过公司业务网关鉴权、且网关保证 `user` 可信 / 会覆写」时才可用于正式版；
否则必须降级到 A 或 B。

---

## 4. `POST /v1/auth/login` 契约（模式 A 用）

**请求**

```http
POST /v1/auth/login
Content-Type: application/json

{ "code": "<wx.login 返回的 code，5 分钟有效，一次性>" }
```

**200**

```json
{
  "user_token": "<服务端签发的不透明 token>",
  "user_id": "u_9f3a...",
  "expires_in": 86400,
  "auth_mode": "wechat"
}
```

**错误**

| HTTP | code | 含义 | 前端动作 |
|---|---|---|---|
| 400 | `invalid_code` | code 为空 / 格式错 | 重新 `wx.login` |
| 401 | `unauthorized` | code 无效、过期或已被使用 | 重新登录一次 |
| 429 | `rate_limited` | 登录过于频繁 | 退避重试 |
| 503 | `wechat_api_unavailable` | jscode2session 超时 / 不可用 | 提示稍后重试，不自动重放 chat |

服务端四步：
1. 用 `WX_APPID` + `WX_APP_SECRET` 调 `sns/jscode2session`（`api.weixin.qq.com` 不能配成小程序合法域名，AppSecret 只能留服务端）；
2. `openid` → **稳定** `uid`（加盐 hash 或落 `wx_user` 表），保证同一用户跨登录不变；
3. 签发 token（HMAC / JWT，含 `uid` + `exp`），返回给前端；
4. `parse_user_token` 增加 `wechat` 分支做验签 + 过期校验；`mock` 分支保留给联调。

---

## 5. 前端接入约定（交给小程序团队的清单）

1. `onLaunch`：`wx.login()` → `/v1/auth/login` → 持久化 `user_token`（模式 B 则跳过此步）。
2. 所有 `/v1/*` 请求原样透传 `user_token`；**不要**解析、拼接或展示它。
3. 统一 401 拦截 → 重新登录 → **只重放幂等请求**（`GET /v1/sessions`、`GET /health`）；
   `POST /v1/chat` / `/v1/chat/stream` **禁止自动重放**，否则会重复扣配额、重复生成。
4. 本地会话缓存按 uid 隔离：`ata_session_id:${uid}`；登出 / 换号清理对应用户的键。
5. 会话三态严格按 `docs/miniprog-session-contract.md` 方案 A：
   进入聊天页先 `POST /v1/sessions` 拿 `session_id`，之后每轮 chat 必带。
   **禁止用 openid 当 `session_id`。**
6. 一期走**同步** `POST /v1/chat`，不要碰 SSE；超时兜底文案与 404 / 429 / 503 分支按现有契约。

---

## 6. 已知契约 bug（必须二选一修复）

`gateway-api.md` 第 4 行声明：

> 鉴权（P0 mock）：`Authorization: Bearer uid:<user_id>` 或 body.`user_token` = `uid:<user_id>`

但 `app/api/routes.py` 中 14 个路由**只读 body / query 的 `user_token`**，
没有任何 `Authorization` header 解析（`/v1/admin/*` 的 `X-Admin-Token` 除外）。
**按文档用 header 传会全部 401。**

修复方案二选一：

- **推荐**：保持文档，给所有 `/v1/*` 增加统一依赖：header 存在时优先取 header，
  否则回落 `user_token` 字段。这样模式 A / B 都天然兼容，且前端可平滑迁移。
- 或：改文档，明确「只支持 body/query 的 `user_token`」，header 说法删除。

---

## 7. 对现有接口的影响

| 项 | 是否变化 |
|---|---|
| `/v1/*` 请求 / 响应形状 | **不变**（`user_token` 仍是不透明字符串） |
| `session_id` 语义与三态 | 不变 |
| `contracts/v0.1` 其它文档 | 不变 |
| `/v1/admin/*` | 建议同步收紧：只放行内网 + `ADMIN_TOKEN` 强口令，Nginx 屏蔽外部访问 |
| 错误码 | 新增 `invalid_code` / `wechat_api_unavailable`，其余不变 |

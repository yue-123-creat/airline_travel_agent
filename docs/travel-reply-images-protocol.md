# 旅游回复插图协议（Agent ↔ 小程序）

本文约定航途 Agent（`/v1/chat`、`/v1/chat/stream`）返回的 `images` 字段如何与正文配合展示，供小程序后端 / 前端对齐。会话页 `static/chat.html` 已按本协议预览。

**小程序部署必读**：[`miniprog-travel-reply-align.md`](./miniprog-travel-reply-align.md)（须解析 `answer` 内 `![]()`，否则会露原文、图沉底）。

## 1. 设计原则

| 场景 | 展示方式 | 张数 |
|------|----------|------|
| 视觉/探索意图（见下「何时出图」；事务问不出图） | 消息底部或正文穿插 | **1～3**（行程按时段穿插时上午/下午为主） |
| 同会话同一景点 | 最多成功出图 **2** 轮；第 2 轮须用**未用过的 URL**，否则该景点本轮跳过 | — |
| 同话题图库冷却 | 出图后 **N=3** 轮内（`turn_index - last_turn`），同景点追问即使命中白名单也不出图；新实体或超过 N 轮后可再出 | — |
| **其余问句** | **默认不出图**，不调高德要图 | 0 |

**何时出图（`wants_attraction_photos`，探索 vs 事务）**：

1. **事务硬拦（优先）**：门票/价格/怎么去/导航/预订/几点/多远/交通/酒店等 → **不出图**（即使同句有「好玩」「景点」）。
2. **强视觉**：照片/图片/实景/长什么样/发几张… → 可出图。
3. **探索介绍**（无事务词时）：好玩/介绍/打卡/一日游/攻略/推荐景点… → 可出图。
4. 其余默认不出图。

实现见 `app/travel/photo_policy.py`（`_TX_INTENT` / `_VISUAL_INTENT` / `_EXPLORE_INTENT`）。

- **主路径（图文混排）**：高德 `photos` → `![景点名](URL)`，按景点名与检索正文混排进 `answer`；前端按正文原位置渲染，避免图片沉底。
- **兼容路径**：无行内 MD 时，仍可用结构化 `images`（`placement` / `slot`）。
- 行程图应尽量对应正文提到的景点。
- **会话记录**：`stm_slots.photos_shown` 记每景点 `{rounds, last_turn, urls}`；`stm_slots.photo_turn_index` 为会话出图轮次计数。

## 2. HTTP / SSE 载荷位置

### 2.1 非流式 `POST /v1/chat`

响应体 `ChatResponse`：

```json
{
  "answer": "……纯文本……",
  "route": "travel",
  "images": [ /* ImageItem[]，见 §3 */ ]
}
```

### 2.2 流式 `POST /v1/chat/stream`

| event | 用途 |
|-------|------|
| `token` | 增量正文（可先渲染文字） |
| `images` | `{ "images": ImageItem[] }`，可在收齐后插图 |
| `done` | `{ "answer": "全文", "images": ImageItem[], ... }`，以 `done` 为准做最终渲染 |

小程序建议：**以 `done.images` 为最终插图源**；若先收到 `images` 事件可做预渲染，再被 `done` 覆盖。

## 3. ImageItem 字段

```ts
type ImageItem = {
  url: string;           // 必填，可直接 <image> / <img> 加载
  name?: string;         // 景点名，作 caption / alt
  role?: string;         // cover | gallery 等，可选
  slot?: string;         // 穿插锚点，见 §4；底部图可缺省
  placement?: "inline" | "footer";  // 缺省按有无 slot 推断
};
```

示例：

```json
[
  {
    "url": "https://…/xizhou.jpg",
    "name": "严家大院",
    "role": "cover",
    "slot": "第1天·下午",
    "placement": "inline"
  },
  {
    "url": "https://…/erhai.jpg",
    "name": "洱海",
    "placement": "footer"
  }
]
```

## 4. `slot` 约定（穿插锚点）

格式：

```text
第{N}天·{时段}
```

- `{N}`：从 1 起的天数
- `{时段}`：`上午` | `下午` | `晚上`

示例：`第1天·上午`、`第2天·下午`。

正文侧应对齐的结构（Agent 收尾 LLM 已约束）：

```text
📖【大理三天两晚】

第1天 抵达与喜洲
上午：……
下午：……
晚上：……

第2天 洱海慢游
上午：……
下午：……
晚上：……
```

也兼容 `Day1` / `Day 1` 标题；小程序解析时把 `DayN` 规范成 `第N天` 再与 `slot` 对齐。

### 4.1 渲染算法（推荐）

1. 若存在任意 `placement === "inline"` 且带 `slot` → **穿插模式**；否则 → **底部模式**。
2. **底部模式**：渲染完整 `answer`，其下挂 1～3 张图（忽略 slot 或统一 footer）。
3. **穿插模式**：
   - 按「第N天 / DayN」切天，天内再按「上午 / 下午 / 晚上」切段；
   - 每段正文渲染后，插入 `slot === 该段锚点` 的图片（每槽最多展示 2～3 张）；
   - 未匹配上的 inline 图可附在文末并带 slot 小标题，避免丢图；
   - `placement === "footer"` 的图仍放整条消息最底部。

## 5. 与 Agent 行为对齐的要点

1. **默认不上「晚上」图**：即使用户行程正文有「晚上：」段落，只要问题未表达早中晚/夜游等，`images` 里通常没有 `·晚上` 的 slot。
2. **图文对应**：只用该时段正文出现的景点名取库内照片；同名图不够时可用**同日**其它已点名景点补到至少 2 张。时段内完全匹配不到候选景点名时，**该 slot 不配图**（不随机塞无关景点）。
3. **城市锁定**：插图景点应属用户目的地同城候选，避免串城。
4. **去重**：同一 `url` 在整条回复中只出现一次。

## 6. 小程序后端对接建议

- 若小程序有独立 BFF：可原样透传 `answer` + `images`，由端上按 §4 渲染；或 BFF 预拆成 `blocks[]`：

```json
{
  "blocks": [
    { "type": "text", "text": "第1天…\n上午：…" },
    { "type": "images", "slot": "第1天·上午", "items": [ /* ImageItem */ ] },
    { "type": "text", "text": "下午：…" },
    { "type": "images", "slot": "第1天·下午", "items": [ /* … */ ] }
  ]
}
```

预拆可选；**协议最低要求**是透传 `images` 字段语义不变。

- 鉴权 / 防盗链：按现有 CDN 策略；`referrer` 建议 `no-referrer`（与会话页一致）。
- 历史消息：落库时建议同时存 `images`（当前 Agent 已写入 message meta），回放时走同一渲染逻辑。

## 7. 版本

| 版本 | 说明 |
|------|------|
| 2026-11-17 | 初版：footer 1～3；行程 inline 上午/下午 2～3；可选晚上；slot=`第N天·时段` |
| 2026-11-17 | 补充小程序对齐文档链接（正文 MD 出图为必做） |

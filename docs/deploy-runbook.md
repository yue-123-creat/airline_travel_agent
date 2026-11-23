# 部署运行手册（后端 agent 侧）

> 目标：**本周五前进入正式版，且能拿到可信的用户 id**。
> 前提：小程序已备案、已正式上线，域名 / 证书 / request 合法域名 / AI 类目均已就绪；
> 本次是**把线上后端从 dify 工作流替换为自建 agent**，不是从零上线。
> 「AI 生成」标识由小程序前端负责，后端不需要处理。
>
> 职责边界：本手册只覆盖后端 agent。`*.wxml`、前端请求层、域名与 Nginx 所有权不在本侧。

---

## 0. 一句话结论

小程序是前端，装不下 agent。所谓「部署」= **把这台四容器的 Docker 服务跑稳，
让它挂到原有已备案域名背后，再把上游从 dify 切过来**。
域名不变 → 合法域名白名单不用改 → 不需要因为域名变更重新审核。

```
小程序 ──HTTPS(原域名, 已备案)──> Nginx(管理员持有) ──> 127.0.0.1:8080 ──> api 容器
                                        │                        ├─ redis    (配额/预占)
                                        └─(保留 dify upstream)    ├─ postgres (会话/STM)
                                                                  └─ qdrant   (RAG 向量)
```

---

## 1. 先发这一段给后端老师（一次问清，避免来回）

> 我在把线上 AI 问答的后端从 dify 换成自建 agent（Docker Compose 四容器，HTTP :8080）。
> 域名和 Nginx 不在我这边，需要确认 6 件事，麻烦帮我看下：
>
> 1. 现在小程序请求 AI 走的是**哪个域名 / 哪条路径**？是直连 `api.dify.ai`，
>    还是公司私有化 dify / 公司业务网关转发？
> 2. 原接口是 **chatflow `/v1/chat-messages`（入参 `query`）**，
>    还是 **workflow `/v1/workflows/run`（入参 `inputs`）**？有无前端调用代码片段或抓包样例？
> 3. 前端调用时传的 **`user` 字段是什么值**（openid / unionid / 业务 uid / 随机串）？
>    这个请求**前面有没有统一登录网关**在鉴权？网关能不能把可信 uid 注入请求头？
> 4. 能否在 Nginx 上**加一条 upstream 指向我提供的 `IP:8080`**，与 dify 那条并存、
>    用开关或按比例分流？我需要能秒级切回 dify。
> 5. 小程序的 **AppID / AppSecret** 由谁保管？（若走 `wx.login` 换 openid 需要）
> 6. 换成阿里云通义千问后，AI 问答类目挂的**技术主体**是否需要更新为阿里云，
>    以及 `msgSecCheck` 内容安全原来是在哪一层做的？

其中 **1、2、3 决定后端怎么写兼容层 / 怎么拿用户 id**，是最优先的三条。
其余不阻塞你本周的后端工作。

---

## 2. 不依赖任何人、这周你自己能做完的

| 优先级 | 事项 | 产物 / 命令 | 状态 |
|---|---|---|---|
| P0 | 冻结版本基线（当前**不是 git 仓库**，无回滚能力） | `git init && git add -A && git commit -m "baseline: pre-deploy" && git tag v0.1.0-baseline` | 待做 |
| P0 | 修好环境变量模板 | 已重建 `.env.example`（UTF-8 无 BOM，补全被吞掉的 KEY） | ✅ 本次已做 |
| P0 | 生产编排加固 | 已新增 `docker-compose.prod.yml` | ✅ 本次已做 |
| P0 | 镜像构建上下文清理 | 已新增 `.dockerignore`（排除 `.env` / `.venv` / 日志） | ✅ 本次已做 |
| P0 | 定义身份契约 | 已新增 `contracts/v0.1/auth.md` | ✅ 本次已做 |
| P0 | 本地按生产配置起一遍并冒烟 | 见 §3 | 待做 |
| P0 | 关掉 `AUTH_MODE=mock` 进生产（正式版红线） | 模式选 A/B/C，见 `auth.md` §3 | 待后端老师回话后定 |
| P1 | 修 `Authorization` header 契约 bug | `gateway-api.md` 声明支持 header，`routes.py` 无解析 → 按文档传必 401 | 待做 |
| P1 | 补 `/v1/auth/login`（若选模式 A） | `app/auth.py` + `routes.py` + `config.py` 加 `WX_APPID/SECRET` | 待定 |
| P1 | dify 兼容路由（若走方案① 免发版切换） | `app/api/dify_compat.py`，形状取决于 §1 第 2 条 | 待定，需先问清 |
| P1 | 超时预算与降级 | Qwen 20s + Qdrant 30s + 地图 + 天气串行，可能顶穿 `wx.request` 默认 60s | 待做 |
| P2 | 灌入 RAG 向量数据、执行 P1 验收脚本 | `scripts/ingest_*`、`docs/p1-acceptance-checklist.md` | 待做 |
| P2 | 管理端点收紧 | `/docs` `/redoc` `/chat` `/static` `/v1/admin/*` 由 Nginx 屏蔽 | 需管理员 |

---

## 3. 部署操作（照着敲）

### 3.1 本机：先按生产配置跑通（今天就能做）

```bash
cd D:/Projects/airline_travel_agent

# 1) 版本基线
git init
git add -A
git commit -m "baseline: pre-deploy"
git tag v0.1.0-baseline

# 2) 生成环境文件（新模板已是 UTF-8 无 BOM）
cp .env.example .env
#    填写所有 <必填> 项：DASHSCOPE_API_KEY / ADMIN_TOKEN / POSTGRES_PASSWORD / 各地图天气 KEY
#    生成强口令： openssl rand -base64 24

# 3) 用生产编排启动（注意：会重新 build 镜像，不再挂载源码）
docker compose -f docker-compose.prod.yml up -d --build

# 4) 看四个容器是否都 healthy
docker compose -f docker-compose.prod.yml ps

# 5) 健康检查
curl -s http://127.0.0.1:8080/health
#    期望： {"ok":true,"redis":true,"qwen_configured":true,"postgres_saver":true,...}
#    qwen_configured=false → API KEY 没读到（检查 .env 是否带 BOM / 键名是否有多余字符）

# 6) 冒烟：按现有 v0.1 契约打一轮完整会话
curl -s -X POST http://127.0.0.1:8080/v1/sessions \
  -H "Content-Type: application/json" \
  -d '{"user_token":"uid:smoke_test_001"}'
#    拿到 session_id 后：
curl -s -X POST http://127.0.0.1:8080/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"user_token":"uid:smoke_test_001","session_id":"<上一步返回的>","message":"上海有哪些好玩的"}'
#    期望 200 + answer + request_id；检查 data/logs/<今天>.jsonl 有对应 telemetry

# 7) 回归测试
pytest -q
```

**验收点**：`/health` 全 true、chat 返回 200、日志有 telemetry、四个容器 `restart` 生效
（`docker update --restart=no` 可临时验证后再改回）。

### 3.2 云主机：同一套 compose 原样搬

公司给你一台 Linux 云主机后（不需要备案新域名，因为复用原域名）：

```bash
# 云主机上
mkdir -p /opt/airline_travel_agent
# 把整个项目目录传上去：代码 + .env + data/ （data/knowledge 是知识库，必须带）
# scp -r ./airline_travel_agent user@host:/opt/

cd /opt/airline_travel_agent
docker compose -f docker-compose.prod.yml up -d --build
curl -s http://127.0.0.1:8080/health
```

要点：

- prod compose 已把 api 的 8080 **只绑 127.0.0.1**，redis/pg/qdrant 不映射宿主机端口，
  公网只应看到 Nginx 的 443。
- `./data` 目录必须一并带上并定期备份（知识库 + `logs/*.jsonl` + `ltm_mock.json`）。
- 三个命名卷 `airline_travel_agent_pgdata` / `_qdrant` / `_redisdata` 也要纳入备份。
- 首次部署后需重灌 RAG（`scripts/ingest_*`），否则 `travel_rag` / `aviation_rag` 是空集合。

### 3.3 交给域名管理员的唯一变更（你不用自己有域名权限）

把下面这段发给他，**只加一条 upstream，不动原有 dify 配置**：

```nginx
upstream ai_agent {
    server 127.0.0.1:8080;      # agent 与 Nginx 同机；不同机则填内网 IP
    keepalive 32;
}
# 保留原有 dify upstream 不动，用于回滚

# 灰度开关：改这一行的注释即可秒级切换
location /agent/ {
    proxy_pass         http://ai_agent/;
    proxy_http_version 1.1;
    proxy_set_header   Host              $host;
    proxy_set_header   X-Real-IP         $remote_addr;
    proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header   Connection        "";
    proxy_read_timeout 70s;              # 略高于 wx.request 默认 60s，给后端超时兜底留余量
    proxy_buffering    off;              # 二期若上 SSE 必须关
    # 管理端点不要暴露：/docs /redoc /chat /static /v1/admin/*
}
```

**如果原域名下已有业务 API 路径**，把 agent 挂到该路径的子路径（如 `/agent/v1/*`）
即可，域名不变，白名单不用动。

### 3.4 灰度与回滚

1. 先按 uid 白名单 / 1% 流量切到 agent，观察 30 分钟：`/health`、`data/logs` 错误率、Qwen 耗时。
2. 异常 → 注释掉 `location /agent/` 的 `proxy_pass` 或切回 dify upstream，**秒级回滚**；
   dify 服务不要提前下线。
3. 稳定后再逐步放大到 100%，最后才考虑下线 dify。

---

## 4. 「能拿到用户 id」怎么实现

这是正式版的核心验收项，且与 `AUTH_MODE=mock` 直接冲突。三条路径见
`contracts/v0.1/auth.md` §3，按你的时间线建议：

| 路径 | 是否需前端发版 | 是否需要 AppSecret | 能否周五前生效 | 建议 |
|---|---|---|---|---|
| A `wechat`（`wx.login` → `code2session` → openid → uid） | 需要（提审 1–3 天） | 需要 | 取决于前端排期与审核 | 终态，**但要尽早排期** |
| B `trusted`（网关鉴权后注入可信 uid 头） | **不需要** | 不需要 | ✅ 可 | 周五硬期限下的首选 |
| C 兼容层透传 dify 的 `user` | 不需要 | 不需要 | ✅ 可 | 仅当 `user` 已被网关保证可信 |

无论哪条，都必须满足：
`user_token` 对前端不透明、uid 稳定不可枚举、身份来自服务端可信来源。
**正式版切换前，`AUTH_MODE` 必须不再是 `mock`**，否则任何人构造 `uid:别人的id`
就能读 / 删他人会话并刷配额。

---

## 5. 上线验收清单（逐条打勾）

- [ ] 四个容器 `Up (healthy)`，`restart: unless-stopped` 已生效
- [ ] `/health` 返回 `redis / qwen_configured / postgres_saver` 全 true
- [ ] `POST /v1/sessions` → `POST /v1/chat` 全链路 200，且 `session_id` 三态正确
- [ ] `AUTH_MODE != mock`；用他人 uid 伪造请求应被 401 拒绝
- [ ] 所有外部依赖（Qwen / Qdrant / 地图 / 天气）有超时与降级，单轮不顶穿 60s
- [ ] 域名 / 证书 / 合法域名沿用原有配置，**未新增域名**
- [ ] Nginx 已屏蔽 `/docs` `/redoc` `/chat` `/static` `/v1/admin/*`
- [ ] 数据库 / Redis / Qdrant 端口未暴露公网；`ADMIN_TOKEN`、`POSTGRES_PASSWORD` 已换强口令
- [ ] `data/` 与三个命名卷有备份；回滚开关验证过（能切回 dify）
- [ ] 「AI 生成」标识仍在前端展示（本侧不处理，但需确认未被改动）
- [ ] 内容安全 `msgSecCheck` 未因替换后端而丢失（向后端老师确认原实现位置）
- [ ] 老用户 dify `conversation_id` 处理方式已定（迁移 or 作废重开）
- [ ] `git tag` 基线存在，出事可一键回滚到切_before 的镜像 / 提交

---

## 6. 倒排（今天 11/22 周日 → 下周五 11/27）

| 时间 | 动作 |
|---|---|
| 今晚 / 周一上午 | 发 §1 那段给后端老师；`git init` 打基线；本机跑通 §3.1 |
| 周一 | 拿到 dify 接入形状 → 定兼容层 or 前端改契约；定身份模式 A/B/C |
| 周二 | 落地身份改造（关 mock）+ 修 header 解析 bug；云主机申请 / 开通 |
| 周三 | 云主机部署 + RAG 灌库 + 全链路冒烟；把 Nginx 变更发给管理员 |
| 周四 | 体验版 / 灰度 1% 验证，压一轮并发与长请求；补超时兜底 |
| 周五 | 灰度放大 → 正式切换；dify 保留待观察；验收清单逐项打勾 |

**唯一可能卡住周五的是**：前端发版提审（若选模式 A）、以及域名管理员排期。
这两条今天就要并行启动，不要等到代码写完。

---

## 7. 常见故障速查

| 现象 | 排查 |
|---|---|
| `qwen_configured=false` | `.env` 是否带 BOM / `DASHSCOPE_API_KEY` 键名是否被注释吞掉；新模板已修 |
| compose 启动报 `POSTGRES_PASSWORD` 必填 | 在 `.env` 生成强口令；compose 会自动派生 `DATABASE_URL`，不要再手改 |
| 首启 api 报 Postgres 连接失败 | postgres healthcheck 未通过；`docker compose ... logs postgres`；卷里旧密码残留时需清理卷或对齐密码 |
| 配额 / 预占重启后丢失 | 必须用 prod compose（Redis 已开 AOF + 独立卷），别用开发版 `docker-compose.yml` |
| RAG 检索返回空 | 首次部署未灌库，跑 `scripts/ingest_*`；确认 `QDRANT_URL=http://qdrant:6333` |
| 前端按 `gateway-api.md` 用 `Authorization` 头传 → 全部 401 | 已知契约 bug，见 `auth.md` §6；临时改用 body/query 的 `user_token` |
| chat 超过 60s 被 `wx.request` 断开 | 一期走同步 chat + 超时兜底；二期再上 SSE / 异步任务 + 轮询 |
| 改了代码但容器里没生效 | prod compose 已去掉源码热挂载，必须 `up -d --build` 重新构建镜像 |

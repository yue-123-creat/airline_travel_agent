# P1 业务验收清单

> 在 P0 对照表基础上增补；状态随实现更新。

## 容错

| 项 | 目标 | 状态 |
|----|------|------|
| 千问 HTTP Timeout | `20s`（由 60s 下调） | DONE |
| IngestRoute 解析失败 | Fallback `route=chat`，不 500 | DONE（JSONDecodeError/ValueError/TypeError/KeyError → chat） |

## 知识库 Postgres

| 项 | 目标 | 状态 |
|----|------|------|
| `attraction_kb` 表 + 索引 | 按 `docs/p1-attraction-kb-postgres.md` | DONE（`init_attraction_kb.sql`） |
| enriched JSON 导入 | upsert；`adult_price` 可 NULL；`guide_chunk_ids` 保 `[]` | DONE（531 行） |
| 标签 AND 过滤 | `tags @> ARRAY[...]` | DONE（`app/travel/pg_search.py`） |
| 排序 Top 5 | 名称命中 → 标签重叠 → 有图 → 有简介 → chunk_count → 票价随意图（ASC/DESC + NULLS LAST） | DONE |
| legacy 隔离 | 正式 chat/enriched 只读 `attraction_kb`；`/v1/travel/legacy` 仅研发 | DONE |

## 地图工具

| 项 | 目标 | 状态 |
|----|------|------|
| 高德路径+实况+邻近 | `amap_route` / `amap_poi_snapshot` / `amap_nearby_pois`；无数据不编造 | DONE |
| 攻略固定结构 | 【上午】【下午】【交通】【注意】；同城门槛；对外不写坐标，只写大约时间 | DONE |
| 照片 | 已有 `media` 写入回答；全量拉图非本阶段必须 | PARTIAL |

## RAG 预留

| 项 | 目标 | 状态 |
|----|------|------|
| `rag_links.guide_chunk_ids` | payload 内保留，现为空数组 | DONE |
| Qdrant 二次检索 | P2（见 `docs/p2-rag-qdrant.md`） | TODO（Batch1 `airport_kb` / `airline_kb` 已建） |

## 数据补全

| 项 | 目标 | 状态 |
|----|------|------|
| 阶段 A 灵犀→库 | 文案补空/追加；`province`/`best_time`；nearby 空才补；去重更新 | DONE（`scripts/phase_a_lingxi_to_kb.py`） |
| 阶段 B 高德→库 | 开放时间 + 邻近（**本轮不做票价**）；脚本 `phase_b_amap_fill_kb.py --only open,nearby` | PARTIAL（邻近已补一批；开放时间受高德日配额 `USER_ABROAD_DAILY_QUERY_OVER_LIMIT` 卡住，配额恢复后重跑 `--only open --apply`） |

## 浏览

DBeaver 请打开视图 **`v_attraction_kb_browse`**（不要直接翻 `attraction_kb` 主键序），执行：

```sql
SELECT * FROM v_attraction_kb_browse
ORDER BY "城市", "景点名";
```

建视图：`scripts/create_attraction_kb_browse_view.sql`

## 冒烟

```powershell
python scripts/p1_smoke_kb_tools.py
python scripts/phase_a_lingxi_to_kb.py          # dry-run
python scripts/phase_a_lingxi_to_kb.py --apply  # 写库
```

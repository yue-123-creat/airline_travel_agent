# LJG 向量切块 + 库表筛选

## Postgres 筛选（先表后向量）

```sql
-- 按大类筛
SELECT service_name, location, rag_links
FROM airport_services
WHERE airport_code = 'LJG' AND service_category = 'lounge';

-- 净空
SELECT * FROM airport_services
WHERE airport_code = 'LJG' AND service_category = 'policy';
```

| category | 含义 | 例 |
|----------|------|-----|
| `transport` | 交通接驳 | 快线/出租/停车 |
| `navigation` | 乘机指引 | 出发到达路线/问讯 |
| `checkin` | 值机与安检 | 值机岛/安检 |
| `facility` | 便民设施 | 母婴室/寄存/医疗 |
| `lounge` | 贵宾与商业 | 休息室/**兴滇惠才卡** |
| `emergency` | 异常与保障 | 延误/行李/**96566** |
| `policy` | 机场规定/净空 | **净空管理** |

`details` 现阶段均为 `{}`，高频问点再补。

## 向量粗切（7 块，已入库）

| chunk_id | category |
|----------|----------|
| `chunk_ljg_01_transport` | transport |
| `chunk_ljg_02_navigation` | navigation |
| `chunk_ljg_03_checkin` | checkin |
| `chunk_ljg_04_facility` | facility |
| `chunk_ljg_05_lounge` | lounge（含兴滇惠才） |
| `chunk_ljg_06_emergency` | emergency（含96566） |
| `chunk_ljg_07_policy` | policy（净空） |

Collection：`aviation_rag`。同大类服务行共用同一 `rag_links.guide_chunk_ids`。

灌库脚本：`python scripts/ingest_ljg_airport_rag.py`

## 读路径（已闭环）

1. 查 `airport_kb` + `airport_services`（能给的字段原样输出）
2. `details` 不足时用行内 `guide_chunk_ids` 调 `search_aviation_rag` 锚定补指引
3. 拼 Markdown；对话支线由千问基于资料组织对用户话术

## /docs 联调

`POST /v1/aviation/airport-service`

```json
{ "airport_code": "LJG", "category": "lounge", "query": "兴滇惠才卡怎么用" }
```

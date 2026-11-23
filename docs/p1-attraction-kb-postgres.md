# P1.2 景点知识库迁 PostgreSQL（JSONB）方案草案

> 对齐已敲定产品规则（正式只走 enriched；票价 null 严格排除；成都含都江堰一带；多标签 AND；票价排序随意图；chunk 按数量 DESC；Top 5 名片；`rag_links.guide_chunk_ids` 先留空数组）。  
> DDL：`scripts/init_db.sql` / `scripts/init_attraction_kb.sql`；导入：`scripts/import_attraction_kb.py`。

---

## 1. 设计原则

1. **热过滤字段外提**（city_norm / tags / adult_price 等）→ 走 B-tree / GIN。  
2. **完整景点对象进 `payload JSONB`** → 回答与后续新字段不改表。  
3. **`rag_links.guide_chunk_ids` 结构现在就保留**（可为空数组），P2 只填 ID。  
4. **legacy 独立备份表**，不进正式检索路径。  
5. 入库时去掉 `_meta`（或塞进 `payload_meta` 可选），避免污染对外名片。

---

## 2. 表结构

### 2.1 主表 `attraction_kb`（正式）

```sql
-- 正式景点知识库（enriched）
CREATE TABLE IF NOT EXISTS attraction_kb (
    id              TEXT PRIMARY KEY,              -- spot_sh_wt_005
    name            TEXT NOT NULL,
    name_norm       TEXT NOT NULL,                 -- 去空白/标点后的归一名，便于命中
    aliases         TEXT[] NOT NULL DEFAULT '{}',
    city_raw        TEXT NOT NULL DEFAULT '',      -- JSON 原文 city
    city_norm       TEXT NOT NULL DEFAULT '',      -- 成都/上海/...（「成都」含都江堰一带）
    region          TEXT NOT NULL DEFAULT '',
    category        TEXT,
    tags            TEXT[] NOT NULL DEFAULT '{}',

    -- 票价（严格过滤用）
    -- adult_price：允许 NULL；禁止 DEFAULT 0；没查到价格必须存 null
    -- 按价格 ASC 排序时必须 NULLS LAST，避免无价格行霸榜
    is_free         BOOLEAN,
    adult_price     NUMERIC(10, 2),                 -- 可 NULL，无 DEFAULT
    currency        TEXT NOT NULL DEFAULT 'CNY',

    -- 检索辅助
    has_cover       BOOLEAN NOT NULL DEFAULT FALSE,
    has_intro       BOOLEAN NOT NULL DEFAULT FALSE,
    chunk_count     INT NOT NULL DEFAULT 0,        -- = len(guide_chunk_ids)，排序用
    lat             DOUBLE PRECISION,
    lng             DOUBLE PRECISION,
    geo_code        TEXT,

    -- 完整对象（含 media/content/visit_info/relations/rag_links...）
    payload         JSONB NOT NULL,

    source          TEXT NOT NULL DEFAULT 'enriched',
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### 2.2 研发比对表 `attraction_kb_legacy`（非正式）

```sql
-- 仅研发比对 / 排障；正式检索禁止读此表
CREATE TABLE IF NOT EXISTS attraction_kb_legacy (
    id              TEXT PRIMARY KEY,              -- 可用 legacy:{name}:{city}
    name            TEXT NOT NULL,
    city_raw        TEXT,
    region          TEXT,
    payload         JSONB NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

上线前可保留表但删应用配置；或直接 drop。

---

## 3. 索引

```sql
-- 城市 / 省
CREATE INDEX IF NOT EXISTS idx_akb_city_norm ON attraction_kb (city_norm);
CREATE INDEX IF NOT EXISTS idx_akb_region ON attraction_kb (region);

-- 标签 AND：tags @> ARRAY['亲子游','自然风光']
CREATE INDEX IF NOT EXISTS idx_akb_tags_gin ON attraction_kb USING GIN (tags);

-- 票价严格过滤
CREATE INDEX IF NOT EXISTS idx_akb_adult_price
  ON attraction_kb (adult_price)
  WHERE adult_price IS NOT NULL;

-- 名称命中
CREATE INDEX IF NOT EXISTS idx_akb_name_norm ON attraction_kb (name_norm);
CREATE INDEX IF NOT EXISTS idx_akb_aliases_gin ON attraction_kb USING GIN (aliases);

-- 排序辅助
CREATE INDEX IF NOT EXISTS idx_akb_chunk_count ON attraction_kb (chunk_count DESC);
CREATE INDEX IF NOT EXISTS idx_akb_has_cover ON attraction_kb (has_cover);

-- JSONB 保底（按 path 查 rag_links 等；主路径优先用外提列）
CREATE INDEX IF NOT EXISTS idx_akb_payload_gin
  ON attraction_kb USING GIN (payload jsonb_path_ops);
```

---

## 4. 导入字段映射（`attractions_enriched.json` → `attraction_kb`）

| 列 | 来源 JSON 路径 | 规则 |
|----|----------------|------|
| `id` | `$.id` | 必填；缺失则生成 `legacy-import:{name}:{city}`（不应发生） |
| `name` | `$.name` | 原样 |
| `name_norm` | `$.name` | 去空白与 `·•－—-_/｜|` 等 |
| `aliases` | `$.aliases` | 数组；非数组 → `{}` |
| `city_raw` | `$.city` | 原样 |
| `city_norm` | `$.city` | 见 §5 归一规则 |
| `region` | `$.region` | 原样 |
| `category` | `$.category` | 可空 |
| `tags` | `$.tags` | 数组 |
| `is_free` | `$.visit_info.ticket.is_free` | 可空 |
| `adult_price` | `$.visit_info.ticket.adult_price` | **可 NULL；禁止 DEFAULT 0；禁止把未知写成 0**；仅 JSON 明确有数字时写入 |
| `currency` | `$.visit_info.ticket.currency` | 默认 CNY |
| `has_cover` | `$.media.cover` | 非空字符串 → true |
| `has_intro` | `$.content.intro` 或 `$.intro` | 非空 → true |
| `chunk_count` | `$.rag_links.guide_chunk_ids` | `len(array)`；缺省或 null → **写 `[]` 且 count=0** |
| `lat` / `lng` | `$.location.lat/lng` | 可空 |
| `geo_code` | `$.location.geo_code` | 可空 |
| `payload` | 整对象 | **删除 `_meta` 后**写入；并保证 `rag_links.guide_chunk_ids` 至少为 `[]` |
| `source` | 常量 | `'enriched'` |
| `updated_at` | `$.rag_links.last_updated` 或 NOW() | 解析失败则 NOW() |

### legacy 导入（`travel_knowledge.json` → `attraction_kb_legacy`）

| 列 | 来源 |
|----|------|
| `id` | `legacy:{name}:{city}` |
| `name` / `city_raw` / `region` | 对应字段 |
| `payload` | 原 attraction 对象 |

---

## 5. `city_norm` 规则（与产品一致）

入库时对 `city_raw` 做最长前缀/包含匹配，例如：

- 含「成都」（含成都都江堰）→ `成都`  
- 含「上海」→ `上海`  
- 其它：维护一张底座词表（与补全脚本 `_CITY_BASES` 对齐）；匹配不到则 `city_norm = city_raw`（或空）

用户说「成都」→ `WHERE city_norm = '成都'`（自然包含都江堰、青城山一带）。

---

## 6. 查询伪 SQL（对齐产品排序）

参数示例：`city_norm=成都`，`tags=['亲子游']`，`max_price=50`，`price_sort=asc|desc|none`，`name_query=可选`。

```sql
SELECT
  id,
  name,
  city_norm,
  region,
  category,
  tags,
  adult_price,
  is_free,
  payload->'media'->>'cover'          AS cover,
  COALESCE(payload->'content'->>'intro', '') AS intro,
  COALESCE(payload->'content'->>'tips', '')  AS tips,
  COALESCE(payload->'rag_links'->'guide_chunk_ids', '[]'::jsonb) AS guide_chunk_ids,
  -- 排序键
  CASE
    WHEN name_norm = :q_norm THEN 3
    WHEN name ILIKE '%' || :q || '%' THEN 2
    WHEN :q = ANY (aliases) THEN 2
    ELSE 0
  END AS name_hit,
  (SELECT COUNT(*)::int FROM unnest(tags) t WHERE t = ANY(:user_tags::text[])) AS tag_overlap
FROM attraction_kb
WHERE
  (:city_norm = '' OR city_norm = :city_norm)
  AND (:user_tags = '{}'::text[] OR tags @> :user_tags)   -- AND
  AND (
    :max_price IS NULL
    OR (adult_price IS NOT NULL AND adult_price < :max_price)  -- 严格：排除 null
  )
ORDER BY
  name_hit DESC,
  tag_overlap DESC,
  has_cover DESC,
  has_intro DESC,
  chunk_count DESC,
  CASE
    WHEN :price_sort = 'asc'  THEN adult_price
  END ASC NULLS LAST,
  CASE
    WHEN :price_sort = 'desc' THEN adult_price
  END DESC NULLS LAST
LIMIT 5;
```

说明：
- `:price_sort` 由意图解析给出：`asc`（便宜优先）/ `desc`（昂贵优先）/ `none`（不按票价排）。  
- Top 5 行直接作为大模型「名片」；`guide_chunk_ids` 供 P2 Qdrant 二次检索。

---

## 7. Top 5 名片 JSON 形状（接口/LLM）

```json
{
  "id": "spot_sh_wt_005",
  "name": "外滩",
  "city_norm": "上海",
  "region": "上海",
  "category": "城市夜景",
  "tags": ["夜景", "拍照", "城市地标"],
  "adult_price": null,
  "is_free": null,
  "cover": "http://store.is.autonavi.com/showpic/...",
  "intro": "黄浦江畔万国建筑群和陆家嘴天际线相对而立。",
  "tips": "傍晚到夜间最适合。",
  "guide_chunk_ids": []
}
```

---

## 8. 落地步骤（实现时）

1. 将 §2–§3 DDL 并入 `scripts/init_db.sql`（或独立 `scripts/init_attraction_kb.sql`）。  
2. 写导入脚本：`attractions_enriched.json` → `attraction_kb`（幂等 upsert by `id`）。  
3. 可选：`travel_knowledge.json` attractions → `attraction_kb_legacy`。  
4. `app/travel/knowledge.py` enriched 路径改为 SQL；文件检索仅 fallback。  
5. 保留 `/v1/travel/legacy` 读 legacy 表或文件，供研发 diff。  
6. 验收用例：成都+亲子+票价&lt;50；无价格意图时排序不含票价主序；chunk_count 全 0 时排序仍稳定。

---

## 9. 已确认约束（DDL / 导入前锁定）

### 9.1 `adult_price`（本轮确认）

| 规则 | 说明 |
|------|------|
| 允许 NULL | 列定义**无** `NOT NULL`、**无** `DEFAULT 0` |
| 未知价格 | 高德/腾讯未返回明确标价 → 存 **null** |
| 免费 | 仅当明确免费时用 `is_free=true` 和/或 `adult_price=0`；**不得**用 0 表示「未知」 |
| ASC 排序 | `ORDER BY adult_price ASC NULLS LAST`（无价格不得霸榜） |
| DESC 排序 | `ORDER BY adult_price DESC NULLS LAST` |
| 过滤「&lt; N」 | `adult_price IS NOT NULL AND adult_price < N`（null 一律排除） |

### 9.2 `rag_links` 锚点（P2 预留）

- `payload.rag_links.guide_chunk_ids` 导入时若缺失 → 写成 **`[]`**
- `chunk_count = jsonb_array_length(...)`，现多为 0；P2 只填 ID，检索代码不改

---

## 10. P1 工程/验收联动（PI 草案对齐）

| 项 | 状态/动作 |
|----|-----------|
| 千问 Timeout 60s → 20s | DONE（`app/llm/qwen.py` → 20s） |
| IngestRoute JSON 失败 Fallback → chat | DONE（解析/类型异常 → chat） |
| 结构化入 Postgres + 标签过滤/排序 | DONE（运行时 `pg_search`） |
| 高德工具：行程两地距离 | DONE（`amap_distance` + LLM tools） |
| RAG 锚点 `guide_chunk_ids` | 方案与导入规则已保留 |

---

## 11. 待实现时注意

- 当前大量 `adult_price = null`，「票价&lt;50」结果会很少——符合严格规则，需在回答中提示。  
- `chunk_count` 现阶段全 0，不阻塞；P2 回填 `guide_chunk_ids` 后自动参与排序。  
- 导入脚本与运行时共用同一套 `city_norm` / `name_norm` 函数，避免两边不一致。

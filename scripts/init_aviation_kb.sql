-- P2: airport_kb / airport_services / airline_kb（机场与航司分表；取代 aviation_kb）
-- airport_kb：机场基础信息（无 rag_links）；
-- airport_services：服务详情 + rag_links（FK → airport_code）；
-- airline_kb：仍为 code stub。与 attraction_kb 物理隔离。可重复执行。

-- 旧 stub（仅 airport_code / updated_at）无法满足 name/city NOT NULL → 重建
DO $$
BEGIN
  IF to_regclass('public.airport_kb') IS NOT NULL
     AND NOT EXISTS (
       SELECT 1
       FROM information_schema.columns
       WHERE table_schema = 'public'
         AND table_name = 'airport_kb'
         AND column_name = 'name'
     ) THEN
    DROP TABLE airport_kb CASCADE;
  END IF;
END $$;

-- 旧混表丢弃（仅有 code，无法回填 name/city）
DROP TABLE IF EXISTS aviation_kb;

CREATE TABLE IF NOT EXISTS airport_kb (
    airport_code     VARCHAR(4) PRIMARY KEY,  -- IATA 三字码 (如 LJG)
    aliases_airport  TEXT[] NOT NULL DEFAULT '{}',  -- 机场别名（与 attraction_kb.aliases 同为 text[]）
    name             VARCHAR(100) NOT NULL,   -- 机场全称
    city             VARCHAR(50) NOT NULL,    -- 所在城市
    open_rule        TEXT,                    -- 航站楼运营时间规则（如：首班起飞前120分钟）
    create_time      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 已建库：去掉 icao_code / rag_links；aliases_airport 统一为 text[]
DROP INDEX IF EXISTS idx_airport_kb_rag_links;
ALTER TABLE airport_kb DROP COLUMN IF EXISTS rag_links;
ALTER TABLE airport_kb DROP COLUMN IF EXISTS icao_code;
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'airport_kb'
      AND column_name = 'aliases_airport'
  ) THEN
    ALTER TABLE airport_kb
      ADD COLUMN aliases_airport TEXT[] NOT NULL DEFAULT '{}';
  ELSIF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'airport_kb'
      AND column_name = 'aliases_airport' AND udt_name = 'text'
  ) THEN
    -- 旧逗号串 → text[]
    ALTER TABLE airport_kb
      ALTER COLUMN aliases_airport DROP DEFAULT,
      ALTER COLUMN aliases_airport TYPE TEXT[]
        USING CASE
          WHEN aliases_airport IS NULL OR btrim(aliases_airport) = '' THEN '{}'::text[]
          ELSE string_to_array(aliases_airport, ',')
        END,
      ALTER COLUMN aliases_airport SET DEFAULT '{}',
      ALTER COLUMN aliases_airport SET NOT NULL;
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_airport_kb_city
  ON airport_kb (city);

CREATE INDEX IF NOT EXISTS idx_airport_kb_aliases_gin
  ON airport_kb USING GIN (aliases_airport);

-- 机场服务详情（外键关联 airport_kb.airport_code；RAG 锚点在此表）
CREATE TABLE IF NOT EXISTS airport_services (
    id                SERIAL PRIMARY KEY,
    airport_code      VARCHAR(4) NOT NULL,      -- 关联机场 (LJG)
    service_category  VARCHAR(50) NOT NULL,     -- 大类 (transport/facility/checkin/lounge)
    service_name      VARCHAR(100) NOT NULL,    -- 小类/名称 (如 交通接驳, 母婴室)
    details           JSONB NOT NULL,           -- 详细参数 (价格、时间等)
    location          TEXT,                     -- 位置指引
    rag_links         JSONB DEFAULT '{"guide_chunk_ids": []}'::jsonb,  -- 预留 RAG 锚点
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_airport_services_airport
      FOREIGN KEY (airport_code) REFERENCES airport_kb (airport_code)
);

ALTER TABLE airport_services
  ADD COLUMN IF NOT EXISTS rag_links JSONB DEFAULT '{"guide_chunk_ids": []}'::jsonb;

CREATE INDEX IF NOT EXISTS idx_airport_services_code
  ON airport_services (airport_code);

CREATE INDEX IF NOT EXISTS idx_airport_services_code_cat
  ON airport_services (airport_code, service_category);

CREATE INDEX IF NOT EXISTS idx_airport_services_rag_links
  ON airport_services USING GIN (rag_links jsonb_path_ops);

CREATE TABLE IF NOT EXISTS airline_kb (
    airline_code   TEXT PRIMARY KEY,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE airline_kb
  ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS idx_airline_kb_updated
  ON airline_kb (updated_at DESC);

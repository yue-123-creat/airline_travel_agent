-- P0: sessions + STM (Postgres) + async usage ledger (quota truth remains Redis)

CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    stm_state    JSONB NOT NULL DEFAULT '{"messages":[],"summary":"","slots":{}}'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS messages (
    id           BIGSERIAL PRIMARY KEY,
    session_id   TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    role         TEXT NOT NULL,
    content      TEXT NOT NULL,
    meta         JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);

CREATE TABLE IF NOT EXISTS usage_ledger (
    id           BIGSERIAL PRIMARY KEY,
    request_id   TEXT NOT NULL,
    reserve_id   TEXT,
    user_id      TEXT NOT NULL,
    session_id   TEXT,
    est_tokens   INT NOT NULL DEFAULT 0,
    actual_tokens INT NOT NULL DEFAULT 0,
    quota_op     TEXT NOT NULL,
    route        TEXT,
    llm_calls    INT NOT NULL DEFAULT 0,
    abnormal_tags JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_usage_user ON usage_ledger(user_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- P1.2 景点知识库（enriched 正式表 + legacy 研发表）
-- adult_price：允许 NULL，无 DEFAULT 0；未知价格必须存 null
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS attraction_kb (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    name_norm       TEXT NOT NULL,
    aliases         TEXT[] NOT NULL DEFAULT '{}',
    city_raw        TEXT NOT NULL DEFAULT '',
    city_norm       TEXT NOT NULL DEFAULT '',
    region          TEXT NOT NULL DEFAULT '',
    category        TEXT,
    tags            TEXT[] NOT NULL DEFAULT '{}',
    is_free         BOOLEAN,
    adult_price     NUMERIC(10, 2),
    currency        TEXT NOT NULL DEFAULT 'CNY',
    has_cover       BOOLEAN NOT NULL DEFAULT FALSE,
    has_intro       BOOLEAN NOT NULL DEFAULT FALSE,
    chunk_count     INT NOT NULL DEFAULT 0,
    lat             DOUBLE PRECISION,
    lng             DOUBLE PRECISION,
    geo_code        TEXT,
    payload         JSONB NOT NULL,
    source          TEXT NOT NULL DEFAULT 'enriched',
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS attraction_kb_legacy (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    city_raw        TEXT,
    region          TEXT,
    payload         JSONB NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_akb_city_norm ON attraction_kb (city_norm);
CREATE INDEX IF NOT EXISTS idx_akb_region ON attraction_kb (region);
CREATE INDEX IF NOT EXISTS idx_akb_tags_gin ON attraction_kb USING GIN (tags);
CREATE INDEX IF NOT EXISTS idx_akb_adult_price
  ON attraction_kb (adult_price)
  WHERE adult_price IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_akb_name_norm ON attraction_kb (name_norm);
CREATE INDEX IF NOT EXISTS idx_akb_aliases_gin ON attraction_kb USING GIN (aliases);
CREATE INDEX IF NOT EXISTS idx_akb_chunk_count ON attraction_kb (chunk_count DESC);
CREATE INDEX IF NOT EXISTS idx_akb_has_cover ON attraction_kb (has_cover);
CREATE INDEX IF NOT EXISTS idx_akb_payload_gin
  ON attraction_kb USING GIN (payload jsonb_path_ops);

-- ---------------------------------------------------------------------------
-- P2 机场 / 航司名片分表（与 attraction_kb 隔离）
-- airport_kb 完整字段；airline_kb 仍 stub；迁移见 scripts/init_aviation_kb.sql
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS airport_kb (
    airport_code     VARCHAR(4) PRIMARY KEY,  -- IATA 三字码 (如 LJG)
    aliases_airport  TEXT[] NOT NULL DEFAULT '{}',  -- 与 attraction_kb.aliases 一致
    name             VARCHAR(100) NOT NULL,   -- 机场全称
    city             VARCHAR(50) NOT NULL,    -- 所在城市
    open_rule        TEXT,                    -- 航站楼运营时间规则
    create_time      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS airport_services (
    id                SERIAL PRIMARY KEY,
    airport_code      VARCHAR(4) NOT NULL,
    service_category  VARCHAR(50) NOT NULL,
    service_name      VARCHAR(100) NOT NULL,
    details           JSONB NOT NULL,
    location          TEXT,
    rag_links         JSONB DEFAULT '{"guide_chunk_ids": []}'::jsonb,
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_airport_services_airport
      FOREIGN KEY (airport_code) REFERENCES airport_kb (airport_code)
);

CREATE TABLE IF NOT EXISTS airline_kb (
    airline_code   TEXT PRIMARY KEY,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_airport_kb_city
  ON airport_kb (city);

CREATE INDEX IF NOT EXISTS idx_airport_kb_aliases_gin
  ON airport_kb USING GIN (aliases_airport);

CREATE INDEX IF NOT EXISTS idx_airport_services_code
  ON airport_services (airport_code);

CREATE INDEX IF NOT EXISTS idx_airport_services_code_cat
  ON airport_services (airport_code, service_category);

CREATE INDEX IF NOT EXISTS idx_airport_services_rag_links
  ON airport_services USING GIN (rag_links jsonb_path_ops);

CREATE INDEX IF NOT EXISTS idx_airline_kb_updated
  ON airline_kb (updated_at DESC);

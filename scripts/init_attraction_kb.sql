-- Standalone apply for existing Postgres volumes (init_db.sql only runs on first boot).
-- Same DDL as scripts/init_db.sql P1.2 section.

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

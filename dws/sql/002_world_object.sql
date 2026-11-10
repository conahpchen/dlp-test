-- 002_world_object.sql —— 契约 v1.3（12 号 §13）：world/world_version/dws_object
--                          + analysis_label/identity_edge/profile/strategy_effect
--                          + event 表追加 world_id、correlation_id 列
-- （PostgreSQL 语法，与 001 一致）

-- ------------------------------------------------------------- 13.1 世界根对象
CREATE TABLE world (
    world_id        TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',
    current_version INT NOT NULL DEFAULT 1
);

CREATE TABLE world_version (
    world_id    TEXT NOT NULL,
    version     INT  NOT NULL,
    change_id   TEXT,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (world_id, version)
);

-- ------------------------------------------------------------- 13.2 通用对象
CREATE TABLE dws_object (
    fact_id       UUID PRIMARY KEY,
    entity_id     UUID NOT NULL,
    version       INT  NOT NULL CHECK (version >= 1),
    hlc           TEXT NOT NULL,
    node_id       TEXT NOT NULL,
    attacker_id   TEXT,
    world_version INT  NOT NULL CHECK (world_version >= 1),
    superseded_by UUID,
    type          TEXT NOT NULL,                 -- ticket / announcement / asset_item…
    object_key    TEXT NOT NULL,
    data          JSONB NOT NULL DEFAULT '{}',
    UNIQUE (entity_id, version)
);
CREATE VIEW object_current AS
    SELECT * FROM (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY entity_id ORDER BY version DESC) rn
        FROM dws_object) t WHERE rn = 1;
CREATE INDEX dws_object_type_idx ON dws_object (type);

-- ------------------------------------------------------------- event 表追加列
ALTER TABLE event ADD COLUMN world_id       TEXT;
ALTER TABLE event ADD COLUMN correlation_id TEXT;

-- ------------------------------------------------------------- 分析/画像/归并/效果
CREATE TABLE analysis_label (
    event_id   TEXT NOT NULL,
    rule_id    TEXT NOT NULL,
    class      TEXT NOT NULL,
    confidence TEXT,
    by         TEXT NOT NULL DEFAULT 'rules-v0',
    labeled_at TEXT NOT NULL DEFAULT now(),
    PRIMARY KEY (event_id, rule_id)
);

CREATE TABLE identity_edge (
    edge_id    BIGSERIAL PRIMARY KEY,
    view_a     TEXT NOT NULL,
    view_b     TEXT NOT NULL,
    signal     TEXT NOT NULL,                  -- credential / ip / knowledge_chain / fingerprint
    confidence TEXT,
    created_at TEXT NOT NULL DEFAULT now()
);

CREATE TABLE profile (
    attacker_id TEXT PRIMARY KEY,
    techniques  JSONB NOT NULL DEFAULT '[]',   -- ATT&CK technique 累计
    first_seen  TEXT,
    last_seen   TEXT,
    summary     TEXT
);

CREATE TABLE strategy_effect (
    strategy_id       TEXT NOT NULL,
    trigger_event     TEXT,
    action            TEXT,
    target_resource   TEXT,
    attacker_session  TEXT,
    engagement_result TEXT,
    dwell_time        TEXT,
    interaction_count INT,
    canary_triggered  TEXT,
    evaluation_time   TEXT
);

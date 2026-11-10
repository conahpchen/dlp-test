-- 001_init.sql —— DWS 全部表（契约 12 §2，v1.1）
-- 纪律（11 §09）：不写 ORM，先写 SQL。追加式版本化：修改事实 = 插入新行（version+1），旧行永不变更。
-- 本文件由 deploy/docker-compose.yml 挂载到 Postgres /docker-entrypoint-initdb.d 自动执行。

-- ------------------------------------------------------------- 2.9 世界元数据
CREATE TABLE dws_meta (
    key   TEXT PRIMARY KEY,
    value JSONB NOT NULL
);
-- 约定键: world_version(INT) / subnets(JSONB) / seed(TEXT) / now_hlc(TEXT)

-- ------------------------------------------------------------- 2.0 公共列
-- 每张实体表都有: fact_id, entity_id, version, hlc, node_id, attacker_id,
--                  world_version, superseded_by（v1.1）+ 类内 payload 列。
-- 追加式: (entity_id, version) 唯一，version 从 1 严格 +1 连续（不变量 I4）。

CREATE TABLE host (
    fact_id        UUID PRIMARY KEY,
    entity_id      UUID NOT NULL,
    version        INT  NOT NULL CHECK (version >= 1),
    hlc            TEXT NOT NULL,
    node_id        TEXT NOT NULL,
    attacker_id    TEXT,
    world_version  INT  NOT NULL CHECK (world_version >= 1),
    superseded_by  UUID,
    hostname       TEXT NOT NULL,
    ip             INET NOT NULL,
    os             TEXT,
    services       JSONB NOT NULL DEFAULT '[]',   -- [{port,proto,ver,banner}]
    banner         TEXT,
    UNIQUE (entity_id, version)
);

CREATE TABLE identity (
    fact_id        UUID PRIMARY KEY,
    entity_id      UUID NOT NULL,
    version        INT  NOT NULL CHECK (version >= 1),
    hlc            TEXT NOT NULL,
    node_id        TEXT NOT NULL,
    attacker_id    TEXT,
    world_version  INT  NOT NULL CHECK (world_version >= 1),
    superseded_by  UUID,
    username       TEXT NOT NULL,
    role           TEXT NOT NULL,
    role_bindings  JSONB NOT NULL DEFAULT '[]',   -- [{entry,role}]
    mfa            BOOL NOT NULL DEFAULT FALSE,
    last_login_hlc TEXT,
    UNIQUE (entity_id, version)
);

CREATE TABLE credential (
    fact_id         UUID PRIMARY KEY,
    entity_id       UUID NOT NULL,
    version         INT  NOT NULL CHECK (version >= 1),
    hlc             TEXT NOT NULL,
    node_id         TEXT NOT NULL,
    attacker_id     TEXT,
    world_version   INT  NOT NULL CHECK (world_version >= 1),
    superseded_by   UUID,
    secret_hash     TEXT NOT NULL,               -- 加盐哈希，无真实明文
    owner_entity_id UUID NOT NULL,               -- -> identity 实体
    valid_on        JSONB NOT NULL DEFAULT '[]', -- [{host,service,scope}]
    revoked_at_hlc  TEXT,
    UNIQUE (entity_id, version)
);

CREATE TABLE data (
    fact_id         UUID PRIMARY KEY,
    entity_id       UUID NOT NULL,
    version         INT  NOT NULL CHECK (version >= 1),
    hlc             TEXT NOT NULL,
    node_id         TEXT NOT NULL,
    attacker_id     TEXT,
    world_version   INT  NOT NULL CHECK (world_version >= 1),
    superseded_by   UUID,
    path            TEXT NOT NULL,
    owner_entity_id UUID,
    ctime_hlc       TEXT NOT NULL,
    mtime_hlc       TEXT NOT NULL,               -- 不变量 I1: ctime <= mtime <= now
    kind            TEXT NOT NULL DEFAULT 'file' CHECK (kind IN ('file','dir')),  -- v1.1
    content         TEXT,                        -- v1.1: 只存攻击者创建的文件；系统路径由模板生成
    canary          BOOL NOT NULL DEFAULT FALSE,
    deleted         BOOL NOT NULL DEFAULT FALSE, -- v1.1
    UNIQUE (entity_id, version)
);

CREATE TABLE relation (
    fact_id        UUID PRIMARY KEY,
    entity_id      UUID NOT NULL,
    version        INT  NOT NULL CHECK (version >= 1),
    hlc            TEXT NOT NULL,
    node_id        TEXT NOT NULL,
    attacker_id    TEXT,
    world_version  INT  NOT NULL CHECK (world_version >= 1),
    superseded_by  UUID,
    from_entity_id UUID NOT NULL,
    to_entity_id   UUID NOT NULL,
    kind           TEXT NOT NULL,                -- l3-reachable / trusts / mounts
    ports          JSONB NOT NULL DEFAULT '[]',  -- 不变量 I3: ports ⊆ services(to)
    UNIQUE (entity_id, version)
);

CREATE TABLE timeline (
    fact_id        UUID PRIMARY KEY,
    entity_id      UUID NOT NULL,
    version        INT  NOT NULL CHECK (version >= 1),
    hlc            TEXT NOT NULL,
    node_id        TEXT NOT NULL,
    attacker_id    TEXT,
    world_version  INT  NOT NULL CHECK (world_version >= 1),
    superseded_by  UUID,
    event_id_ref   TEXT NOT NULL,                -- -> event.event_id
    object_ref     TEXT,                         -- host:<uuid> / credential:<uuid> / ...
    kind           TEXT NOT NULL,                -- login / login_fail / cmd / revocation / ...
    UNIQUE (entity_id, version)
);

-- ------------------------------------------------------------- 2.7 会话（钉版本）
CREATE TABLE dws_session (
    session_id     TEXT PRIMARY KEY,
    attacker_id    TEXT NOT NULL,
    user_name      TEXT,
    pinned_version INT  NOT NULL,                -- 会话开始时固定；生成只读 <= 该版本（§5.1）
    started_hlc    TEXT NOT NULL,
    ended_hlc      TEXT,
    host_entity_id UUID
);

-- ------------------------------------------------------------- 2.8 世界预算
CREATE TABLE dws_budget (
    session_id            TEXT PRIMARY KEY,
    new_entities_used     INT NOT NULL DEFAULT 0,  -- 上限 12（M4 前固定）
    tokens_used           INT NOT NULL DEFAULT 0,  -- 上限 40000
    -- 世界总节点上限 200 为全局约束，存 dws_meta，由提交事务强制
    last_interaction_hlc  TEXT
);

-- ------------------------------------------------------------- 2.10 事件库
CREATE TABLE event (
    land_seq     BIGSERIAL PRIMARY KEY,          -- 与 events.jsonl 行序一一对应
    event_id     TEXT NOT NULL UNIQUE,
    ts_hlc       TEXT NOT NULL,
    entry        TEXT NOT NULL,
    node         TEXT NOT NULL,
    session      TEXT NOT NULL,
    attacker_id  TEXT,
    payload      JSONB NOT NULL                  -- 统一事件全文
);
CREATE INDEX event_session_idx ON event (session);
CREATE INDEX event_hlc_idx     ON event (ts_hlc);

-- ------------------------------------------------------------- 当前有效行视图
-- 各实体类一张（列不同不能 UNION ALL）；钉定版本的快照读在读取侧按
-- world_version <= :pinned 谓词 + 自写叠加求值（§5.1 v1.1）。
CREATE VIEW host_current AS
    SELECT * FROM (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY entity_id ORDER BY version DESC) rn
        FROM host) t WHERE rn = 1;
CREATE VIEW identity_current AS
    SELECT * FROM (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY entity_id ORDER BY version DESC) rn
        FROM identity) t WHERE rn = 1;
CREATE VIEW credential_current AS
    SELECT * FROM (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY entity_id ORDER BY version DESC) rn
        FROM credential) t WHERE rn = 1;
CREATE VIEW data_current AS
    SELECT * FROM (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY entity_id ORDER BY version DESC) rn
        FROM data) t WHERE rn = 1;
CREATE VIEW relation_current AS
    SELECT * FROM (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY entity_id ORDER BY version DESC) rn
        FROM relation) t WHERE rn = 1;
CREATE VIEW timeline_current AS
    SELECT * FROM (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY entity_id ORDER BY version DESC) rn
        FROM timeline) t WHERE rn = 1;

-- ------------------------------------------------------------- 常用索引
CREATE INDEX host_entity_ver_idx     ON host (entity_id, version DESC);
CREATE INDEX data_entity_ver_idx     ON data (entity_id, version DESC);
CREATE INDEX data_path_idx           ON data (path);
CREATE INDEX credential_secret_idx   ON credential (secret_hash);
CREATE INDEX timeline_attacker_idx   ON timeline (attacker_id);
CREATE INDEX relation_to_idx         ON relation (to_entity_id);

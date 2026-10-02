-- =============================================================
--rag-kb 数据层Schema
-- 版本 v1.1（与 docs/数据规范.md v1.1 严格对应）
--
-- 设计要点：
--   1. 能力值采用「数值 + 类别 + 说明 + 原始字面量」四联存储，
--      归一化是 lossy 操作，保留 *_raw 才能在规范演进时重跑历史数据
--   2. 类别枚举强制区分 缺失/未知/不适用/无限 —— 五种不同语义
--   3. 结构化数值不进入向量索引，文本块单独建表
--
-- 对应校验规则 V1-V11（见 docs/数据规范.md §5）
-- =============================================================

BEGIN;

-- -------------------------------------------------------------
-- 枚举类型
-- -------------------------------------------------------------
CREATE TYPE stat_category AS ENUM (
    'RANKED',                -- 正常等级，有数值（1–5）
    'NONE',                  -- 明确无此能力，数值为 0
    'EMPTY_SLOT',            -- 空位（∅ / undefined / N/A），数值 NULL
    'UNKNOWN',               -- 未知（?），数值 NULL
    'NOT_APPLICABLE',        -- 不适用，数值 NULL
    'INFINITE',              -- 无限（∞），数值 NULL，raw 保留原值
    'CONDITIONAL',           -- 条件值有基础等级，数值有效，note 存说明
    'CONDITIONAL_NO_BASE',   -- 条件值无基础等级（首位 ※），数值 NULL
    'UNPARSED'               -- 无法解析，需人工介入（目标：0 条）
);

CREATE TYPE form_type AS ENUM (
    'base', 'evolved', 'requiem', 'alternate'
);

CREATE TYPE conflict_type AS ENUM (
    'VALUE_MISMATCH',        -- 数值实质不同
    'CATEGORY_DIFF',         -- 类别不同但语义可能等价
    'MISSING_IN_ONE',        -- 一源缺失一源有
    'DUPLICATE_IN_SOURCE',   -- 源内重复登记
    'COLUMN_MISMATCH'        -- 列名/列序不一致
);

-- -------------------------------------------------------------
-- 角色表
-- -------------------------------------------------------------
CREATE TABLE characters (
    char_id      TEXT PRIMARY KEY,
    name_en      TEXT NOT NULL,
    name_zh      TEXT,
    aliases      TEXT[],
    gender       TEXT,
    affiliation  TEXT,
    part_debut   INTEGER
);

-- -------------------------------------------------------------
-- 替身主表
-- -------------------------------------------------------------
CREATE TABLE stands (
    stand_id      TEXT PRIMARY KEY,          -- 规范化 slug，如 star_platinum
    name_en       TEXT NOT NULL,
    name_raw      TEXT NOT NULL,             -- 源原始名（含多余引号等脏字符）
    name_zh       TEXT,
    aliases       TEXT[],
    owner_id      TEXT REFERENCES characters(char_id),
    part          INTEGER,                -- 所属部 3–9
    part_name_en  TEXT,                       -- 如 Stardust Crusaders
    stand_type    TEXT,
    ability_desc  TEXT,                      -- 非结构化能力描述长文本
    reference     TEXT,                      -- 命名原型出处
    form_chain    TEXT[],                    -- 形态链，按序
    data_sources  TEXT[] NOT NULL DEFAULT '{}',
    updated_at    TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_stands_part    ON stands(part);
CREATE INDEX idx_stands_owner   ON stands(owner_id);
CREATE INDEX idx_stands_name_en ON stands(name_en);
-- GIN 索引支撑别名/形态链包含查询
CREATE INDEX idx_stands_aliases ON stands USING GIN(aliases);
CREATE INDEX idx_stands_forms   ON stands USING GIN(form_chain);

-- -------------------------------------------------------------
-- 能力数值表（六维宽表）
--
-- 每维四联：value(数值) / cat(类别) / note(条件说明) / raw(原始字面量)
--
-- 约束来自 V10：category 与 stat_value 必须一致
-- -------------------------------------------------------------
CREATE TABLE stand_stats (
    stand_id  TEXT PRIMARY KEY REFERENCES stands(stand_id) ON DELETE CASCADE,

    pwr INTEGER, pwr_cat stat_category, pwr_note TEXT, pwr_raw TEXT NOT NULL,
    spd INTEGER, spd_cat stat_category, spd_note TEXT, spd_raw TEXT NOT NULL,
    rng INTEGER, rng_cat stat_category, rng_note TEXT, rng_raw TEXT NOT NULL,
    sta INTEGER, sta_cat stat_category, sta_note TEXT, sta_raw TEXT NOT NULL,
    prc INTEGER, prc_cat stat_category, prc_note TEXT, prc_raw TEXT NOT NULL,
    dev INTEGER, dev_cat stat_category, dev_note TEXT, dev_raw TEXT NOT NULL,

    -- 综合分：仅当 6 维全部非 NULL 时计算，否则 NULL（禁止取平均，见规范§1.7）
    composite      INTEGER,
    -- 缺失维度数，便于「数据完整度」类查询
    missing_count  INTEGER NOT NULL DEFAULT 0,
    has_partial    INTEGER NOT NULL DEFAULT 0,

    -- V2：数值必须落在 0–5
    CONSTRAINT ck_pwr_range CHECK (pwr IS NULL OR pwr BETWEEN 0 AND 5),
    CONSTRAINT ck_spd_range CHECK (spd IS NULL OR spd BETWEEN 0 AND 5),
    CONSTRAINT ck_rng_range CHECK (rng IS NULL OR rng BETWEEN 0 AND 5),
    CONSTRAINT ck_sta_range CHECK (sta IS NULL OR sta BETWEEN 0 AND 5),
    CONSTRAINT ck_prc_range CHECK (prc IS NULL OR prc BETWEEN 0 AND 5),
    CONSTRAINT ck_dev_range CHECK (dev IS NULL OR dev BETWEEN 0 AND 5),

    -- V10：RANKED / CONDITIONAL 必须有数值
    CONSTRAINT ck_pwr_ranked  CHECK (pwr_cat NOT IN ('RANKED','CONDITIONAL') OR pwr IS NOT NULL),
    CONSTRAINT ck_spd_ranked  CHECK (spd_cat NOT IN ('RANKED','CONDITIONAL') OR spd IS NOT NULL),
    CONSTRAINT ck_rng_ranked  CHECK (rng_cat NOT IN ('RANKED','CONDITIONAL') OR rng IS NOT NULL),
    CONSTRAINT ck_sta_ranked  CHECK (sta_cat NOT IN ('RANKED','CONDITIONAL') OR sta IS NOT NULL),
    CONSTRAINT ck_prc_ranked  CHECK (prc_cat NOT IN ('RANKED','CONDITIONAL') OR prc IS NOT NULL),
    CONSTRAINT ck_dev_ranked  CHECK (dev_cat NOT IN ('RANKED','CONDITIONAL') OR dev IS NOT NULL),

    -- V10：NONE 必须为 0
    CONSTRAINT ck_pwr_none    CHECK (pwr_cat <> 'NONE' OR pwr = 0),
    CONSTRAINT ck_spd_none    CHECK (spd_cat <> 'NONE' OR spd = 0),
    CONSTRAINT ck_rng_none    CHECK (rng_cat <> 'NONE' OR rng = 0),
    CONSTRAINT ck_sta_none    CHECK (sta_cat <> 'NONE' OR sta = 0),
    CONSTRAINT ck_prc_none    CHECK (prc_cat <> 'NONE' OR prc = 0),
    CONSTRAINT ck_dev_none    CHECK (dev_cat <> 'NONE' OR dev = 0),

    -- V10：INVALID 类别必须无数值
    CONSTRAINT ck_pwr_null    CHECK (pwr_cat NOT IN ('EMPTY_SLOT','UNKNOWN','NOT_APPLICABLE','INFINITE','CONDITIONAL_NO_BASE','UNPARSED') OR pwr IS NULL),
    CONSTRAINT ck_spd_null    CHECK (spd_cat NOT IN ('EMPTY_SLOT','UNKNOWN','NOT_APPLICABLE','INFINITE','CONDITIONAL_NO_BASE','UNPARSED') OR spd IS NULL),
    CONSTRAINT ck_rng_null    CHECK (rng_cat NOT IN ('EMPTY_SLOT','UNKNOWN','NOT_APPLICABLE','INFINITE','CONDITIONAL_NO_BASE','UNPARSED') OR rng IS NULL),
    CONSTRAINT ck_sta_null    CHECK (sta_cat NOT IN ('EMPTY_SLOT','UNKNOWN','NOT_APPLICABLE','INFINITE','CONDITIONAL_NO_BASE','UNPARSED') OR sta IS NULL),
    CONSTRAINT ck_prc_null    CHECK (prc_cat NOT IN ('EMPTY_SLOT','UNKNOWN','NOT_APPLICABLE','INFINITE','CONDITIONAL_NO_BASE','UNPARSED') OR prc IS NULL),
    CONSTRAINT ck_dev_null    CHECK (dev_cat NOT IN ('EMPTY_SLOT','UNKNOWN','NOT_APPLICABLE','INFINITE','CONDITIONAL_NO_BASE','UNPARSED') OR dev IS NULL),

    -- V10：INFINITE 必须保留原值（不可丢）
    CONSTRAINT ck_pwr_inf     CHECK (pwr_cat <> 'INFINITE' OR pwr_raw IS NOT NULL),
    CONSTRAINT ck_spd_inf     CHECK (spd_cat <> 'INFINITE' OR spd_raw IS NOT NULL),
    CONSTRAINT ck_rng_inf     CHECK (rng_cat <> 'INFINITE' OR rng_raw IS NOT NULL),
    CONSTRAINT ck_sta_inf     CHECK (sta_cat <> 'INFINITE' OR sta_raw IS NOT NULL),
    CONSTRAINT ck_prc_inf     CHECK (prc_cat <> 'INFINITE' OR prc_raw IS NOT NULL),
    CONSTRAINT ck_dev_inf     CHECK (dev_cat <> 'INFINITE' OR dev_raw IS NOT NULL),

    -- V10：CONDITIONAL 必须有说明
    CONSTRAINT ck_pwr_cond    CHECK (pwr_cat <> 'CONDITIONAL' OR pwr_note IS NOT NULL),
    CONSTRAINT ck_spd_cond    CHECK (spd_cat <> 'CONDITIONAL' OR spd_note IS NOT NULL),
    CONSTRAINT ck_rng_cond    CHECK (rng_cat <> 'CONDITIONAL' OR rng_note IS NOT NULL),
    CONSTRAINT ck_sta_cond    CHECK (sta_cat <> 'CONDITIONAL' OR sta_note IS NOT NULL),
    CONSTRAINT ck_prc_cond    CHECK (prc_cat <> 'CONDITIONAL' OR prc_note IS NOT NULL),
    CONSTRAINT ck_dev_cond    CHECK (dev_cat <> 'CONDITIONAL' OR dev_note IS NOT NULL),

    -- V3：composite 非空时 6 维必须齐全
    CONSTRAINT ck_composite   CHECK (composite IS NULL OR missing_count = 0)
);

-- 支撑「按维度数值查询」的索引（结构化检索的主力路径）
CREATE INDEX idx_stats_pwr ON stand_stats(pwr);
CREATE INDEX idx_stats_spd ON stand_stats(spd);
CREATE INDEX idx_stats_rng ON stand_stats(rng);
CREATE INDEX idx_stats_sta ON stand_stats(sta);
CREATE INDEX idx_stats_prc ON stand_stats(prc);
CREATE INDEX idx_stats_dev ON stand_stats(dev);
CREATE INDEX idx_stats_composite ON stand_stats(composite);
-- 支撑「按异常类型筛选」，D8 实验要用
CREATE INDEX idx_stats_pwr_cat ON stand_stats(pwr_cat);
CREATE INDEX idx_stats_sta_cat ON stand_stats(sta_cat);

-- -------------------------------------------------------------
-- 形态表
-- -------------------------------------------------------------
CREATE TABLE stand_forms (
    form_id     TEXT PRIMARY KEY,
    stand_id    TEXT REFERENCES stands(stand_id) ON DELETE CASCADE,
    form_name   TEXT NOT NULL,
    form_type   form_type DEFAULT 'base',
    parent_id   TEXT REFERENCES stand_forms(form_id),
    awakens_to  TEXT,
    condition   TEXT,                -- 觉醒条件描述
    is_default  BOOLEAN DEFAULT false-- 默认形态（数值取该形态）
);

CREATE INDEX idx_forms_stand ON stand_forms(stand_id);
CREATE INDEX idx_forms_parent ON stand_forms(parent_id);

-- -------------------------------------------------------------
-- 条件值跨状态展开表（规范 §1.6，特殊场景）
-- -------------------------------------------------------------
CREATE TABLE stand_stat_conditional (
    stand_id       TEXT REFERENCES stands(stand_id) ON DELETE CASCADE,
    stat_dim       TEXT NOT NULL,           -- PWR/SPD/RNG/STA/PRC/DEV
    state_desc     TEXT NOT NULL,           -- 初始 / 觉醒后 / 成熟期
    stat_value     INTEGER,
    condition_note TEXT,
    PRIMARY KEY (stand_id, stat_dim, state_desc),
    CONSTRAINT ck_dim CHECK (stat_dim IN ('PWR','SPD','RNG','STA','PRC','DEV')),
    CONSTRAINT ck_val CHECK (stat_value IS NULL OR stat_value BETWEEN 0 AND 5)
);

-- -------------------------------------------------------------
-- 冲突记录表（规范 §2.2）
-- 冲突不做静默覆盖，全部落表
-- -------------------------------------------------------------
CREATE TABLE stat_conflicts (
    id             SERIAL PRIMARY KEY,
    stand_id       TEXT,
    stat_dim       TEXT,
    value_a        TEXT,
    source_a       TEXT NOT NULL,       -- jojowiki / csv_bogdan / csv_topology
    value_b        TEXT,
    source_b       TEXT,
    conflict_type  conflict_type,
    resolution     TEXT,                -- prefer_primary / normalize_only / manual
    resolved_value TEXT,
    resolved_cat   stat_category,
    note           TEXT,
    created_at     TIMESTAMPTZ DEFAULT now(),
    CONSTRAINT ck_conf_dim CHECK (stat_dim IS NULL OR stat_dim IN ('PWR','SPD','RNG','STA','PRC','DEV'))
);

CREATE INDEX idx_conf_stand ON stat_conflicts(stand_id);
CREATE INDEX idx_conf_type  ON stat_conflicts(conflict_type);

-- -------------------------------------------------------------
-- 文本块表（进入向量索引的单元，规范 §4）
--
-- 关键设计：stat_row 类型不进此表，结构化数据走 SQL
-- -------------------------------------------------------------
CREATE TABLE text_chunks (
    chunk_id       BIGSERIAL PRIMARY KEY,
    stand_id       TEXT REFERENCES stands(stand_id) ON DELETE CASCADE,
    form_id        TEXT REFERENCES stand_forms(form_id),
    chunk_type     TEXT NOT NULL,   -- ability_overview / move / battle_record / lore
    content        TEXT NOT NULL,
    content_len    INTEGER,
    part           INTEGER,
    source_url     TEXT,
    data_source    TEXT,
    -- 用于检索结果溯源与冲突消解
    source_version TEXT,
    created_at     TIMESTAMPTZ DEFAULT now(),
    CONSTRAINT ck_type CHECK (chunk_type IN ('ability_overview','move','battle_record','lore'))
);

CREATE INDEX idx_chunks_stand ON text_chunks(stand_id);
CREATE INDEX idx_chunks_type  ON text_chunks(chunk_type);
CREATE INDEX idx_chunks_form  ON text_chunks(form_id);

-- -------------------------------------------------------------
-- 视图：完整可比较的替身（6 维齐全才进此视图）
-- 用于「数据完整」类查询，避免应用层处理 NULL
-- -------------------------------------------------------------
CREATE VIEW v_stands_complete AS
SELECT s.stand_id, s.name_en, s.name_zh, s.part, s.part_name_en,
       s.owner_id, c.name_en AS owner_name,
       t.pwr, t.spd, t.rng, t.sta, t.prc, t.dev,
       t.composite
FROM stands s
JOIN stand_stats t ON t.stand_id = s.stand_id
LEFT JOIN characters c ON c.char_id = s.owner_id
WHERE t.missing_count = 0;

-- -------------------------------------------------------------
-- 视图：存在异常的替身（D8 脏数据实验的入口）
-- -------------------------------------------------------------
CREATE VIEW v_stands_anomalous AS
SELECT s.stand_id, s.name_en, s.part,
       t.pwr_cat, t.pwr_raw, t.pwr_note,
       t.spd_cat, t.spd_raw, t.spd_note,
       t.rng_cat, t.rng_raw, t.rng_note,
       t.sta_cat, t.sta_raw, t.sta_note,
       t.prc_cat, t.prc_raw, t.prc_note,
       t.dev_cat, t.dev_raw, t.dev_note,
       t.missing_count
FROM stands s
JOIN stand_stats t ON t.stand_id = s.stand_id
WHERE t.missing_count > 0;

-- -------------------------------------------------------------
-- 视图：冲突汇总
-- -------------------------------------------------------------
CREATE VIEW v_conflict_summary AS
SELECT conflict_type, source_a, source_b, COUNT(*) AS n
FROM stat_conflicts
GROUP BY conflict_type, source_a, source_b;

COMMIT;

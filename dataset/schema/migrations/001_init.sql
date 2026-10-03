-- ==========================================================================
-- rag-kb 初始化迁移 001
--
-- 设计原则：**完全幂等** —— 可反复执行不报错
--   - CREATE TABLE / TYPE / INDEX 全部用 IF NOT EXISTS
--   - CHECK 约束内联在表定义里（Postgres 的 CHECK 不支持 IF NOT EXISTS，
--     但因表本身幂等，重复执行不会重复添加）
--   - 无 DROP TABLE —— 避免误删数据
--
-- 执行方式：
--   psql -U ragkb -d ragkb -f 001_init.sql
--   或 python database/load_db.py --init
-- ==========================================================================

-- --------------------------------------------------------------------------
-- 0. 扩展
-- --------------------------------------------------------------------------
-- pg_trgm：模糊匹配（替身名拼写变体、别名检索）
-- vector：pgvector 向量索引（M3 补，M4 用）
-- ★两者都是可选的，用 IF NOT EXISTS 避免权限不足时报错中断
--   若无权限，删掉对应行即可（不影响核心功能）
-- --------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- bge-m3 输出 1024 维float32，向量列定义见 3.1
-- 注意：pgvector 的 HNSW 索引在 <1000 行时收益不明显，
--   本项目 2407 块用暴力精确检索即可（实测 <1ms）
--   故不预建HNSW 索引，M3 决策记录见 docs/M3实验报告.md §1

-- --------------------------------------------------------------------------
-- 1. 枚举类型
-- --------------------------------------------------------------------------
-- 1.1 能力值类别（数据规范 §1.5）
-- 关键设计：EMPTY_SLOT（明确空位）vs UNKNOWN（不确定）vs
--           NOT_APPLICABLE（该源未提供）是三种不同语义，不可合并
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'stat_category') THEN
        -- ★ 顺序与 encode.py 的 Category 枚举严格一致
        --   check_schema_consistency.py 会校验两者集合与顺序
        CREATE TYPE stat_category AS ENUM (
            'RANKED',                -- 正常等级，有数值（A–E）
            'NONE',                  -- 明确无此能力，数值 0（字面量 'None'）
            'EMPTY_SLOT',            -- 空位，数值 NULL（字面量 '∅'）
            'UNKNOWN',               -- 未知，数值 NULL（字面量 '?'，与 EMPTY_SLOT 语义不同）
            'NOT_APPLICABLE',        -- 该源未提供（CSV 空格子）
            'INFINITE',              -- 无限，数值 NULL（字面量 '∞'）
            'CONDITIONAL',           -- 条件值有基础等级（如 'B※20-30 meters'）
            'CONDITIONAL_NO_BASE',   -- 条件值无基础等级（如 '※Complete'）
            'UNPARSED'               -- 判定表未覆盖（实测应为 0）
        );
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'form_type') THEN
        CREATE TYPE form_type AS ENUM (
            'base',      -- 基础形态
            'evolved',   -- 进化形态
            'alt',       -- 替身替身（不同所有者持有）
            'unknown'
        );
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'conflict_type') THEN
        CREATE TYPE conflict_type AS ENUM (
            'VALUE_MISMATCH',        -- 数值不同
            'CATEGORY_DIFF',         -- 类别不同
            'MISSING_IN_ONE',    -- 一方缺失
            'DUPLICATE_IN_SOURCE',   -- 源内重复登记
            'NAME_CONFLICT'          -- 名称冲突
        );
    END IF;
END$$;

-- --------------------------------------------------------------------------
-- 2. 表
-- --------------------------------------------------------------------------

-- 2.1 characters —— 角色（替身的使用者）
CREATE TABLE IF NOT EXISTS characters (
    character_id   TEXT PRIMARY KEY,
    name_en      TEXT NOT NULL,
    name_ja     TEXT,
    part       SMALLINT,
    stand_ids     TEXT[],        -- 便于反查该角色的所有替身

    CONSTRAINT chk_char_part CHECK (
        part IS NULL OR part BETWEEN 1 AND 9
    )
);

CREATE INDEX IF NOT EXISTS idx_char_name ON characters(name_en);
CREATE INDEX IF NOT EXISTS idx_char_part ON characters(part);
CREATE INDEX IF NOT EXISTS idx_char_stands ON characters USING GIN(stand_ids);

COMMENT ON TABLE characters IS
    '替身使用者。part 为首次登场部（数据源限制，可能为 NULL）';
COMMENT ON COLUMN characters.character_id IS
    'name_en 归一化后生成的 ID';

-- 2.2 stands —— 替身主表
CREATE TABLE IF NOT EXISTS stands (
    stand_id       TEXT PRIMARY KEY,
    name_en     TEXT NOT NULL,
    name_ja        TEXT,
    owner_id        TEXT REFERENCES characters(character_id),
    part           SMALLINT,
    part_name_en   TEXT,
    stand_type     TEXT,          -- Close-Range / Natural Humanoid ...
    reference      TEXT,          -- 命名出处
    manga_debut    TEXT,
    anime_debut    TEXT,
    form_count     SMALLINT NOT NULL DEFAULT 1,
    main_table_registrations SMALLINT NOT NULL DEFAULT 1,
    detail_url     TEXT,
    owner_name_raw TEXT,          -- 原始字符串（未归一，含括号注释）
    aliases        TEXT[],
    form_chain     TEXT[],        -- 形态 ID 链

    CONSTRAINT chk_stands_part CHECK (part IS NULL OR part BETWEEN 1 AND 9),
    CONSTRAINT chk_stands_form_count CHECK (form_count >= 0)
);

CREATE INDEX IF NOT EXISTS idx_stands_part  ON stands(part);
CREATE INDEX IF NOT EXISTS idx_stands_owner ON stands(owner_id);
CREATE INDEX IF NOT EXISTS idx_stands_name  ON stands(name_en);
CREATE INDEX IF NOT EXISTS idx_stands_trgm  ON stands USING GIN(name_en gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_stands_aliases ON stands USING GIN(aliases);
CREATE INDEX IF NOT EXISTS idx_stands_forms   ON stands USING GIN(form_chain);

COMMENT ON TABLE stands IS
    '替身主表。main_table_registrations > 1 表示主源有多次登记（形态差异，非脏数据）';
COMMENT ON COLUMN stands.main_table_registrations IS
    '主表里的同名登记次数。实测 3 组：Star Platinum / Killer Queen / Echoes';
COMMENT ON COLUMN stands.form_count IS
    '详情页解析出的形态数';

-- 2.3 stand_stats —— 六维能力值
-- ★ 核心设计：raw 与 cat并存
--   value  归一化后的数值（0–5），无值时 NULL
--   raw    原始字面量（如 'C※Range: 2 m (6.6 ft)'），永久保留
--   cat    类别（stat_category 枚举）
--   note   条件值说明
-- 这样规范演进时可从 raw 重新推导，不必重新抓数据
CREATE TABLE IF NOT EXISTS stand_stats (
    stand_id   TEXT PRIMARY KEY REFERENCES stands(stand_id) ON DELETE CASCADE,

    -- 归一化数值（0–5；NONE 类为 0；无有效值为 NULL）
    pwr SMALLINT, spd SMALLINT, rng SMALLINT,
    sta SMALLINT, prc SMALLINT, dev SMALLINT,

    -- 原始字面量（★ 永久保留，归一化是 lossy 操作）
    pwr_raw TEXT, spd_raw TEXT, rng_raw TEXT,
    sta_raw TEXT, prc_raw TEXT, dev_raw TEXT,

    -- 类别
    pwr_cat stat_category, spd_cat stat_category, rng_cat stat_category,
    sta_cat stat_category, prc_cat stat_category, dev_cat stat_category,

    -- 条件值说明（仅 CONDITIONAL / CONDITIONAL_NO_BASE 类别有值）
    pwr_note TEXT, spd_note TEXT, rng_note TEXT,
    sta_note TEXT, prc_note TEXT, dev_note TEXT,

    -- 综合分：六维求和
    -- ★ 约束：任一维缺失即置 NULL，禁止取平均（数据规范 §3.2）
    composite      SMALLINT,
    missing_count  SMALLINT NOT NULL DEFAULT 0,

    CONSTRAINT chk_stat_range CHECK (
        (pwr IS NULL OR pwr BETWEEN 0 AND 5) AND
        (spd IS NULL OR spd BETWEEN 0 AND 5) AND
        (rng IS NULL OR rng BETWEEN 0 AND 5) AND
        (sta IS NULL OR sta BETWEEN 0 AND 5) AND
        (prc IS NULL OR prc BETWEEN 0 AND 5) AND
        (dev IS NULL OR dev BETWEEN 0 AND 5)
    ),
    -- ★ composite 禁止取平均：有任一维无值，composite 必须为 NULL
    CONSTRAINT chk_composite_no_avg CHECK (
        composite IS NULL OR missing_count = 0
    ),
    CONSTRAINT chk_composite_range CHECK (
        composite IS NULL OR composite BETWEEN 0 AND 30
    ),
    CONSTRAINT chk_missing_count CHECK (missing_count BETWEEN 0 AND 6)
);

CREATE INDEX IF NOT EXISTS idx_stats_pwr  ON stand_stats(pwr);
CREATE INDEX IF NOT EXISTS idx_stats_spd  ON stand_stats(spd);
CREATE INDEX IF NOT EXISTS idx_stats_rng  ON stand_stats(rng);
CREATE INDEX IF NOT EXISTS idx_stats_sta  ON stand_stats(sta);
CREATE INDEX IF NOT EXISTS idx_stats_prc  ON stand_stats(prc);
CREATE INDEX IF NOT EXISTS idx_stats_dev  ON stand_stats(dev);
CREATE INDEX IF NOT EXISTS idx_stats_comp ON stand_stats(composite);
CREATE INDEX IF NOT EXISTS idx_stats_pwr_cat ON stand_stats(pwr_cat);
CREATE INDEX IF NOT EXISTS idx_stats_sta_cat ON stand_stats(sta_cat);
-- 多维筛选用复合索引（T2 极值推理题会用到）
CREATE INDEX IF NOT EXISTS idx_stats_pwr_spd ON stand_stats(pwr, spd);

COMMENT ON TABLE stand_stats IS
    '六维能力值。value 归一化 / raw 原始字面量 / cat 类别 三者并存';
COMMENT ON COLUMN stand_stats.composite IS
    '六维求和。任一维无值则 NULL —— 禁止取平均（实测 123/154 可计算）';
COMMENT ON COLUMN stand_stats.missing_count IS
    '无有效值的维度数（0–6）';

-- 2.4 stand_forms —— 替身形态
-- ★ 这是本项目区别于普通 RAG 的关键表：
--   主源把同一替身的多形态压成多行登记（main_table_registrations），
--   本表显式建模形态链
CREATE TABLE IF NOT EXISTS stand_forms (
    form_id    TEXT PRIMARY KEY,
    stand_id   TEXT NOT NULL REFERENCES stands(stand_id) ON DELETE CASCADE,
    form_name  TEXT,
    form_type  form_type NOT NULL DEFAULT 'base',
    parent_id  TEXT REFERENCES stand_forms(form_id),

    -- 归一化数值（0–5），复用 encode.py 判定表编码
    pwr SMALLINT, spd SMALLINT, rng SMALLINT,
    sta SMALLINT, prc SMALLINT, dev SMALLINT,

    -- ★ 原始字面量与类别（与 stand_stats 一致的审计能力）
    --形态表的 values 来自渲染层，天然是字面量（'A'/'B'/'?'/'∞'），
    --   入库时编码为数值，但原始值必须保留以便追溯
    pwr_raw TEXT, spd_raw TEXT, rng_raw TEXT,
    sta_raw TEXT, prc_raw TEXT, dev_raw TEXT,
    pwr_cat stat_category, spd_cat stat_category, rng_cat stat_category,
    sta_cat stat_category, prc_cat stat_category, dev_cat stat_category,

    raw_order  SMALLINT,

    CONSTRAINT chk_form_range CHECK (
        (pwr IS NULL OR pwr BETWEEN 0 AND 5) AND
        (spd IS NULL OR spd BETWEEN 0 AND 5) AND
        (rng IS NULL OR rng BETWEEN 0 AND 5) AND
        (sta IS NULL OR sta BETWEEN 0 AND 5) AND
        (prc IS NULL OR prc BETWEEN 0 AND 5) AND
        (dev IS NULL OR dev BETWEEN 0 AND 5)
    )
);

CREATE INDEX IF NOT EXISTS idx_forms_stand  ON stand_forms(stand_id);
CREATE INDEX IF NOT EXISTS idx_forms_parent ON stand_forms(parent_id);
CREATE INDEX IF NOT EXISTS idx_forms_type   ON stand_forms(form_type);

COMMENT ON TABLE stand_forms IS
    '替身形态。实测 146 个形态，11 个替身有多形态';
COMMENT ON COLUMN stand_forms.pwr_raw IS
    '原始字面量（如 ''A''/''?''/''∞''）。★ 形态表的值来自渲染层，'
    '入库时用 encode.py 判定表编码为 0–5，raw 保留以便追溯';

-- 2.5 stand_stat_conditional —— 条件值明细
CREATE TABLE IF NOT EXISTS stand_stat_conditional (
    stand_id      TEXT NOT NULL REFERENCES stands(stand_id) ON DELETE CASCADE,
    stat_dim      TEXT NOT NULL,
    base_value    SMALLINT,
    raw_value     TEXT NOT NULL,
    condition_note TEXT NOT NULL,

    PRIMARY KEY (stand_id, stat_dim),
    CONSTRAINT chk_cond_dim CHECK (stat_dim IN ('PWR','SPD','RNG','STA','PRC','DEV')),
    CONSTRAINT chk_cond_base CHECK (base_value IS NULL OR base_value BETWEEN 0 AND 5)
);

COMMENT ON TABLE stand_stat_conditional IS
    '条件值明细。实测 23 条（如 Made in Heaven 速度 = ∞）';

-- 2.6 stat_conflicts —— 源间冲突
-- ★ 本表是「冲突不静默覆盖」原则的落地：
--   冲突全部落表并保留原始值，便于后续策略调整后重新消解
CREATE TABLE IF NOT EXISTS stat_conflicts (
    conflict_id  BIGSERIAL PRIMARY KEY,
    stand_id     TEXT NOT NULL REFERENCES stands(stand_id) ON DELETE CASCADE,
    stat_dim     TEXT NOT NULL,
    value_a      TEXT,
    source_a     TEXT NOT NULL,
    value_b      TEXT,
    source_b     TEXT NOT NULL,
    conflict_type conflict_type NOT NULL,
    resolution   TEXT,       -- prefer_primary / split_by_form / manual
    resolved_value SMALLINT,
    resolved_cat stat_category,
    resolved_by  TEXT,       -- 人工消解时记录
    resolved_at  TIMESTAMPTZ,
    note         TEXT,

    CONSTRAINT chk_conf_dim CHECK (stat_dim IN ('PWR','SPD','RNG','STA','PRC','DEV')),
    CONSTRAINT chk_conf_resolved CHECK (
        resolved_value IS NULL OR resolved_value BETWEEN 0 AND 5
    ),
    -- ★ 防止「同一对来源对同一维度重复记冲突」
    CONSTRAINT uniq_conflict UNIQUE (stand_id, stat_dim, value_a, source_a,
                                     value_b, source_b)
);

CREATE INDEX IF NOT EXISTS idx_conf_stand  ON stat_conflicts(stand_id);
CREATE INDEX IF NOT EXISTS idx_conf_type   ON stat_conflicts(conflict_type);
CREATE INDEX IF NOT EXISTS idx_conf_unres  ON stat_conflicts(resolved_value)
    WHERE resolved_value IS NULL;

COMMENT ON TABLE stat_conflicts IS
    '源间冲突记录。★ 人工消解请写 resolved_by / resolved_at，'
    'load_db.py 用 ON CONFLICT DO NOTHING，不会覆盖人工决策';

-- 2.7 text_chunks —— 文本块
-- ★ 与 stand_stats 分离的理由（数据规范 §3.1）：
--   结构化数据不进向量索引。数值走 SQL，文本才走向量。
CREATE TABLE IF NOT EXISTS text_chunks (
    chunk_id    INTEGER PRIMARY KEY,
    stand_id    TEXT NOT NULL REFERENCES stands(stand_id) ON DELETE CASCADE,
    stand_name  TEXT,
    part        SMALLINT,
    chunk_type  TEXT NOT NULL,
    content     TEXT NOT NULL,
    content_len INTEGER NOT NULL,
    section     TEXT,
    entity      TEXT,          -- 招式名
    phonetic    TEXT,          -- 招式读音
    alias       TEXT,
    debut       TEXT,          -- 招式首发章节
    source_url  TEXT,
    data_source TEXT NOT NULL DEFAULT 'jojowiki_rendered',

    -- 向量列（M3 已算好，1024 维 bge-m3）
    -- ★ 注意：CREATE EXTENSION vector 若失败，此列会创建失败。
    --   故用独立 migration 002 处理，或先确保 pgvector 已装。
    -- embedding vector(1024),

    CONSTRAINT chk_chunk_len CHECK (content_len > 0),
    CONSTRAINT chk_chunk_type CHECK (
        chunk_type IN ('ability_overview','move','battle_record',
                       'lore','section')
    )
);

CREATE INDEX IF NOT EXISTS idx_chunks_stand ON text_chunks(stand_id);
CREATE INDEX IF NOT EXISTS idx_chunks_type  ON text_chunks(chunk_type);
CREATE INDEX IF NOT EXISTS idx_chunks_part  ON text_chunks(part);
CREATE INDEX IF NOT EXISTS idx_chunks_len   ON text_chunks(content_len);
-- 全文检索（M4 混合检索用）
CREATE INDEX IF NOT EXISTS idx_chunks_fts ON text_chunks
    USING GIN(to_tsvector('english', coalesce(content, '')));

COMMENT ON TABLE text_chunks IS
    '文本块。★ 结构化能力值在 stand_stats，不进本表';
COMMENT ON COLUMN text_chunks.chunk_type IS
    'ability_overview / move / battle_record / lore / section';

-- --------------------------------------------------------------------------
-- 3. 视图
-- --------------------------------------------------------------------------

-- 3.1 替身全景（M4 冲突消解与 T2 极值推理的主入口）
CREATE OR REPLACE VIEW v_stand_overview AS
SELECT
    s.stand_id, s.name_en, s.name_ja, s.owner_id, s.owner_name_raw,
    s.part, s.part_name_en, s.stand_type, s.reference,
    s.form_count, s.main_table_registrations,
    st.pwr, st.spd, st.rng, st.sta, st.prc, st.dev,
    st.pwr_cat, st.spd_cat, st.rng_cat, st.sta_cat, st.prc_cat, st.dev_cat,
    st.composite, st.missing_count
FROM stands s
LEFT JOIN stand_stats st ON st.stand_id = s.stand_id;

COMMENT ON VIEW v_stand_overview IS
    '替身全景。★ 极端值题（T2）直接查本视图';

-- 3.2 分部统计（T2 分部极值题用）
CREATE OR REPLACE VIEW v_part_stats AS
SELECT
    part,
    max(part_name_en)  AS part_name_en,
    count(*)           AS n_stands,
    count(composite)   AS n_with_composite,
    round(avg(pwr), 2)  AS avg_pwr,
    round(avg(spd), 2)  AS avg_spd,
    round(avg(rng), 2)  AS avg_rng,
    round(avg(sta), 2)  AS avg_sta,
    round(avg(prc), 2)  AS avg_prc,
    round(avg(dev), 2)  AS avg_dev,
    round(avg(composite), 2) AS avg_composite
FROM v_stand_overview
GROUP BY part;

COMMENT ON VIEW v_part_stats IS
    '分部统计。avg 忽略 NULL（不会把空位当 0 算进平均）';

-- 3.3 待消解冲突（M4 的工作队列）
CREATE OR REPLACE VIEW v_conflicts_pending AS
SELECT
    c.conflict_id, c.stand_id, s.name_en, s.part,
    c.stat_dim, c.value_a, c.source_a, c.value_b, c.source_b,
    c.conflict_type, c.note,
    s.part_name_en
FROM stat_conflicts c
JOIN stands s ON s.stand_id = c.stand_id
WHERE c.resolved_value IS NULL
ORDER BY c.conflict_type, s.part NULLS LAST, c.stand_id;

COMMENT ON VIEW v_conflicts_pending IS
    '待消解冲突队列。★ M4 实验直接读本视图';

-- 3.4 替身能力雷达（生成侧用；M8 接入生成后启用）
CREATE OR REPLACE VIEW v_stand_vector AS
SELECT
    stand_id, name_en, part, part_name_en,
    pwr, spd, rng, sta, prc, dev, composite
FROM v_stand_overview
WHERE composite IS NOT NULL;

COMMENT ON VIEW v_stand_vector IS
    '六维完整可比的替身（123 条）。★ 生成数值答案时应加此过滤';

-- --------------------------------------------------------------------------
-- 4. 统计视图
-- --------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_data_quality AS
SELECT
    'stands'          AS table_name, count(*) AS n_rows, NULL::TEXT AS detail
FROM stands
UNION ALL SELECT 'stand_stats',  count(*),
    'composite可算: ' || count(composite) || '/' || count(*)
FROM stand_stats
UNION ALL SELECT 'stand_forms',  count(*), NULL FROM stand_forms
UNION ALL SELECT 'text_chunks',  count(*),
    '平均长度: ' || round(avg(content_len)) FROM text_chunks
UNION ALL SELECT 'stat_conflicts', count(*),
    '待消解: ' || count(*) FILTER (WHERE resolved_value IS NULL)
FROM stat_conflicts
UNION ALL SELECT 'characters',   count(*), NULL FROM characters;

COMMENT ON VIEW v_data_quality IS
    '数据质量总览。★ 每次入库后跑一次 SELECT * FROM v_data_quality;';

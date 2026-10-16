-- =====================================================================
-- M72：篇章（Story Arc）维度 + text_chunks 支持「篇章块」
--
-- 由来：用户反馈②「勒索之路的替身是什么」。
--   源站里 Shakedown Road **既是地点也是篇章**，两个独立页面靠
--   {{Hatnote}} 互相指路；我们只建了地点 → 篇章诉求被答成地点关联。
--   M70 建了 story_arcs.json（JSON only），M71 建了篇章↔替身证据链，
--   M72 补角色侧 + 把篇章落库 + 篇章块入 text_chunks。
-- =====================================================================

-- ------------------------------------------------------------------
-- 1) 篇章主表（M70 的 story_arcs.json 落库）
--    ★ part 判据与 locations 同源（源站 infobox 的 part 字段），
--      chapters 是源站的部内相对章号（如 "JJL 18-22"），
--      chapters_abs 是换算后的绝对章号（M71 的口径归一化）。
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS story_arcs (
    arc_id       TEXT PRIMARY KEY,           -- 剥掉引号与 (Story Arc) 后缀
    arc_title    TEXT NOT NULL,              -- 源站页面原标题
    name_en      TEXT,                       -- 显示名（Infobox 的 title）
    part         SMALLINT,                   -- 正传部号 1–9
    chapters     TEXT[] NOT NULL DEFAULT '{}',  -- 源站原样（部内相对）
    chapters_abs INTEGER[],                  -- 换算后的绝对章号区间 [lo, hi]
    location_id  TEXT,                       -- 同名地点的 id（消歧用）
    source_url   TEXT,
    data_source  TEXT
);

-- ------------------------------------------------------------------
-- 2) text_chunks 支持「篇章块」—— 第三类归属
--
-- 背景：
--   M70 抓了 189 个篇章页（`"Shakedown Road" (Story Arc)` 等），
--   但当时没重跑 build_article_chunks → 篇章块一直没生成。
--   M72 重跑后生成 1062 个篇章块，入库时撞上 chk_chunk_owner：
--
--     psycopg2.errors.CheckViolation: new row for relation "text_chunks"
--       violates check constraint "chk_chunk_owner"
--
--   因为篇章块**既不是替身页的、也不是角色页的** ——
--   stand_id 与 character_id 皆为 NULL，正是约束要防的"孤儿块"。
--
-- ★ 为什么不是"跳过篇章块不入库"：
--   检索层（retrieval/merging.py 的 _load_base_chunks）是运行时
--   text_chunks.json + article_chunks.json 合并的，篇章块**已经在
--   检索库里**。若 DB 不入，就会出现「检索能命中、但按 SQL 查不到、
--   证据回查失败」的分裂状态 —— 与 M65「表里有数据但 loader 没写」
--   是同族问题。
--
-- ★ 约束不是放松，是**补上第三类合法归属**：
--   原来的「至少有一个归属」这个设计意图是对的，只是当时只有两类实体。
--   现在有第三类（篇章），约束跟着扩，不降低严格度。
-- =====================================================================

-- 1) 新列：篇章 id
ALTER TABLE text_chunks
    ADD COLUMN IF NOT EXISTS arc_id TEXT;

-- 2) 归属约束：三类至少一类非空
--
--    ★★ 为什么要 NOT VALID ★★
--    迁移在**数据入库之前**跑，而此时 `arc_id` 列刚加上、全是 NULL ——
--    库里已有的篇章块（M72 之前那次部分入库留下的）三个归属全空，
--    直接 ADD CONSTRAINT 会失败：
--        check constraint "chk_chunk_owner" is violated by some row
--    → 标准做法：先加 `NOT VALID`（只约束**新插入**的行，不扫旧数据），
--      等 load_db 把 arc_id 填好后再 `VALIDATE CONSTRAINT`（那时才扫全表）。
--      load_db 在数据加载完成后会执行这一步（见 load_data 末尾）。
--
--    ★ 必须先 DROP 再 ADD（PG 不支持 ALTER CONSTRAINT 改表达式）
ALTER TABLE text_chunks DROP CONSTRAINT IF EXISTS chk_chunk_owner;
-- ★★ 四类，不是三类 —— location_id 也在里面 ★★
--   踩坑实录：我第一版只写了 stand / character / arc 三类，
--   结果 1179 个**地点块**（M65 加的，只有 location_id）全部违反约束：
--       psycopg2.errors.CheckViolation: ... Failing row contains
--         (6633, null, Air Supplena Island, 2, ..., air_supplena_island, null)
--   003_locations.sql 早就把这个约束从两类扩到了三类（加 location_id），
--   我在 005 里重写时**漏掉了它** —— 又一处「改写别人定过型的东西时
--   没有先看它现在的全貌」。
--   → 四类归属：替身 / 角色 / 地点 / 篇章。
ALTER TABLE text_chunks ADD CONSTRAINT chk_chunk_owner CHECK (
    stand_id IS NOT NULL
    OR character_id IS NOT NULL
    OR location_id IS NOT NULL
    OR arc_id IS NOT NULL
) NOT VALID;

-- 3) 外键：arc_id 指向 story_arcs（该表由 build_locations 产出）
--    ★ 用 DO 块包一层：若 story_arcs 表不存在（老库未建）则只警告不中断，
--      避免「为了加一列把整个 migration 搞失败」。
DO $$
BEGIN
    -- story_arcs 由本 migration 的第 1 段创建，正常必然存在。
    -- 保留这层判断是为了「迁移被部分执行过」时不至于整体失败。
    IF EXISTS (SELECT 1 FROM information_schema.tables
               WHERE table_name = 'story_arcs') THEN
        ALTER TABLE text_chunks DROP CONSTRAINT IF EXISTS text_chunks_arc_id_fkey;
        ALTER TABLE text_chunks ADD CONSTRAINT text_chunks_arc_id_fkey
            FOREIGN KEY (arc_id) REFERENCES story_arcs(arc_id);
    ELSE
        RAISE NOTICE 'story_arcs 表不存在，跳过外键';
    END IF;
END $$;

-- 4) 索引
CREATE INDEX IF NOT EXISTS idx_chunks_arc ON text_chunks(arc_id);

COMMENT ON COLUMN text_chunks.arc_id IS
    'M72：篇章页块的 arc id（替身/角色页块为 NULL）；三者至少一个非空';

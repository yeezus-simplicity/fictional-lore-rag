-- ==========================================================================
-- 002_article_chunks.sql —— B 方案（扩语料）的 schema 升级
-- ==========================================================================
-- ★★ 为什么必须有这个文件，而不是只改 001 ★★
--   001 用的是 `CREATE TABLE IF NOT EXISTS`。
--   **对已经存在的库，改 001 的建表语句完全不会生效** ——
--   表已存在，IF NOT EXISTS 直接跳过。
--   本地库（已 load 过数据）不会重建，于是：
--     - 新增的 character_id 列不存在 → 入库报 column does not exist
--     - chunk_type 的 CHECK 仍拒绝 'article_section'
--     - stand_id 仍 NOT NULL → 角色页块（无 stand_id）被拒
--   → 需要一个**幂等的 ALTER**，让新库（001 已正确）与旧库都能收敛到同一状态。
--
-- ★ 幂等性要求（可重复执行）：
--   - 用 ADD COLUMN IF NOT EXISTS（PG 9.6+）
--   - DROP CONSTRAINT IF EXISTS 再 ADD
--   - ALTER COLUMN ... DROP NOT NULL 本身幂等
--   这样 load_db 每次启动都执行也不会报错。
-- ==========================================================================

-- 1) stand_id 改可空：角色页块没有 stand_id
--    ★ 外键约束保留 —— SQL 标准里 NULL 不参与外键检查，
--      所以已有的替身页块仍然受 ON DELETE CASCADE 保护。
ALTER TABLE text_chunks ALTER COLUMN stand_id DROP NOT NULL;

-- 2) 新增 character_id（角色页块的归属）
ALTER TABLE text_chunks
    ADD COLUMN IF NOT EXISTS character_id TEXT;

-- 3) character_id 外键（若缺失才加）
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'text_chunks_character_id_fkey'
    ) THEN
        ALTER TABLE text_chunks
            ADD CONSTRAINT text_chunks_character_id_fkey
            FOREIGN KEY (character_id) REFERENCES characters(character_id)
            ON DELETE CASCADE;
    END IF;
END $$;

-- 4) chunk_type 白名单加入 article_section
ALTER TABLE text_chunks DROP CONSTRAINT IF EXISTS chk_chunk_type;
ALTER TABLE text_chunks ADD CONSTRAINT chk_chunk_type CHECK (
    chunk_type IN ('ability_overview','move','battle_record',
                   'lore','section','article_section')
);

-- 5) 归属约束：不允许「既无 stand_id 又无 character_id」的孤儿块
--   ★ M72：用 DO 块包一层，**先看有没有行会违反**再加约束。
--     踩坑实录：`load_db --all` 在已跑过 005（加了 arc_id、约束放宽成三类）
--     的库上重跑，002 走到这里会失败：
--        check constraint "chk_chunk_owner" of relation "text_chunks"
--        is violated by some row
--     因为库里已有篇章块（stand_id / character_id 皆 NULL）——
--     它们是 005 认可的新合法归属，但 002 的旧约束不认。
--     → 迁移必须是**幂等且顺序无关**的：002 跑在 005 之后就应当跳过。
--   ★ 005 会把约束改成三类归属（stand / character / arc），那才是最终形态。
ALTER TABLE text_chunks DROP CONSTRAINT IF EXISTS chk_chunk_owner;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM text_chunks
               WHERE stand_id IS NULL AND character_id IS NULL) THEN
        RAISE NOTICE '存在无 stand/character 归属的块（多为 005 的篇章块）→ 跳过加约束，由 005 放宽为三类';
    ELSE
        ALTER TABLE text_chunks ADD CONSTRAINT chk_chunk_owner CHECK (
            stand_id IS NOT NULL OR character_id IS NOT NULL
        );
    END IF;
END $$;

-- 6) 索引
CREATE INDEX IF NOT EXISTS idx_chunks_character ON text_chunks(character_id);

COMMENT ON COLUMN text_chunks.character_id IS
    '角色页块的角色 id（替身页块为 NULL）；与 stand_id 至少一个非空';
COMMENT ON COLUMN text_chunks.chunk_type IS
    'ability_overview / move / battle_record / lore / section / article_section';

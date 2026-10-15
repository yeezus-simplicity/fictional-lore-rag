-- 003_locations.sql
-- M65：地点维度（正传 9 部 / 113 个地点）+ **带语料证据**的地点关联表
--
-- == 为什么要有这一层 ==
--   替身天然只覆盖第 3–9 部（`stands.part` 实测第 1、2 部各 0 个 —— 波纹时代
--   没有替身）。第 1、2 部的内容只能靠**地点与人物**来承载
--   （Ogre Street / Joestar Mansion / Air Supplena Island …）。
--
-- == ★★ 为什么关联表必须带 chunk_id ★★
--   用户明确要求：「不准瞎掰 必须要有语料证据」——
--   「这个地点和哪个替身有关」不能靠推测（比如"第 4 部的替身都可能出现在杜王町"），
--   必须是**语料里真的写了**，并且能指出是**哪一段**。
--   所以 `location_mentions` 每行都记：chunk_id + 命中的原文片段 + 匹配依据，
--   前端可以点开看到证据 —— 与项目「答案附证据」的一贯做法一致。

-- ------------------------------------------------------------------
-- 1) 地点主表
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS locations (
    location_id      TEXT PRIMARY KEY,
    name_en          TEXT NOT NULL,
    name_ja          TEXT,                    -- ja_kanji，如「杜王町」「アンジェロ岩」
    name_romaji      TEXT,
    primary_type     TEXT,                    -- 主类型（界面单行展示）
    types            TEXT[] NOT NULL DEFAULT '{}',
    part             SMALLINT,                -- 正传部号 1–9（非正传不入库）
    part_basis       TEXT,                    -- ★ 判据原文（如 'Chapter 272'），可复核
    mangadebut       TEXT,                    -- 最早一次漫画出场
    animedebut       TEXT,
    debut_chapter    INTEGER,                 -- 从 mangadebut 解析出的章节号
    source_url       TEXT,
    n_infobox_fields SMALLINT,
    data_source      TEXT
);

-- ------------------------------------------------------------------
-- 2) 归属层级（一个地点可有多条路径 —— 源站用 `<br />` 并列）
--    实测 Devil's Palm 同时写了「美国」（SBR 主线）与「杜王町」（JJL 提及），
--    拼成一条会得到「United States → Morioh → Japan」这种**不存在的路径**。
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS location_places (
    id          SERIAL PRIMARY KEY,
    location_id TEXT NOT NULL REFERENCES locations(location_id) ON DELETE CASCADE,
    path_index  SMALLINT NOT NULL,   -- 第几条路径
    depth       SMALLINT NOT NULL,   -- 0 = 最具体 → 越大越宽泛
    place_name  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_location_place
    ON location_places(location_id, path_index, depth);
CREATE INDEX IF NOT EXISTS idx_location_places_name
    ON location_places(place_name);

-- ------------------------------------------------------------------
-- 3) ★ 地点 ↔ 替身/角色 关联（**每条都带语料证据**）
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS location_mentions (
    id           BIGSERIAL PRIMARY KEY,
    location_id  TEXT NOT NULL REFERENCES locations(location_id) ON DELETE CASCADE,
    chunk_id     BIGINT,             -- ★ 证据：语料块 id（text_chunks.chunk_id）
    entity_kind  TEXT NOT NULL,      -- 'stand' | 'character'
    entity_id    TEXT NOT NULL,
    -- ★★ 证据强度：两者都算"有语料证据"，但含义不同 ★★
    --   'location_page' = **地点页正文里写了这个替身/角色** → 强证据
    --                     （如 Angelo Rock 页写明命名来源是 Anjuro Katagiri）
    --   'entity_page'   = 替身/角色页里提到了该地名 → 弱证据
    --                     （「born in Japan」这类会让泛地名关联爆炸）
    direction    TEXT NOT NULL DEFAULT 'entity_page',
    match_kind   TEXT NOT NULL,      -- 凭什么匹配上：name_en / name_ja / alias
    matched_term TEXT NOT NULL,      -- 实际命中的那个词
    snippet      TEXT                -- 命中处上下文（截断，供人复核）
);
CREATE INDEX IF NOT EXISTS idx_loc_mentions_loc
    ON location_mentions(location_id);
CREATE INDEX IF NOT EXISTS idx_loc_mentions_ent
    ON location_mentions(entity_kind, entity_id);
-- 同一块里同一实体同一词只记一次
CREATE UNIQUE INDEX IF NOT EXISTS uq_loc_mention
    ON location_mentions(location_id, chunk_id, entity_kind, entity_id, matched_term);

-- ★ 幂等补列：表若在本迁移之前就已建（缺 direction），这里补上
ALTER TABLE location_mentions
    ADD COLUMN IF NOT EXISTS direction TEXT NOT NULL DEFAULT 'entity_page';
CREATE INDEX IF NOT EXISTS idx_loc_mentions_direction
    ON location_mentions(direction);

-- ------------------------------------------------------------------
-- 4) text_chunks 支持地点页块
-- ------------------------------------------------------------------
ALTER TABLE text_chunks ADD COLUMN IF NOT EXISTS location_id TEXT;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'text_chunks_location_id_fkey') THEN
        ALTER TABLE text_chunks
            ADD CONSTRAINT text_chunks_location_id_fkey
            FOREIGN KEY (location_id) REFERENCES locations(location_id)
            ON DELETE CASCADE;
    END IF;
END $$;

-- ★ 归属约束从「替身或角色」放宽为「替身 / 角色 / 地点 至少一个非空」
--   （地点页块只有 location_id）
ALTER TABLE text_chunks DROP CONSTRAINT IF EXISTS chk_chunk_owner;
ALTER TABLE text_chunks ADD CONSTRAINT chk_chunk_owner CHECK (
    stand_id IS NOT NULL OR character_id IS NOT NULL OR location_id IS NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chunks_location ON text_chunks(location_id);

-- ------------------------------------------------------------------
-- 5) 注释（说明口径与已知局限）
-- ------------------------------------------------------------------
COMMENT ON TABLE locations IS
    '地点（M65）。口径：只收 9 部正传漫画（外传/小说/游戏不入库）';
COMMENT ON COLUMN locations.part_basis IS
    '部号判据（mangadebut 原文，如 Chapter 272 / SBR Chapter 13），可复核';
COMMENT ON COLUMN locations.debut_chapter IS
    '★ 已知局限：mangadebut 只记**首次**出场 → 跨部地点只算最早部'
    '（Naples/Rome/Venice 记在第 2 部，故第 5 部偏少）';
COMMENT ON TABLE location_mentions IS
    '地点↔替身/角色的关联，★ 每条都带语料证据（chunk_id + 原文片段 + 匹配依据）';
COMMENT ON COLUMN location_mentions.match_kind IS
    '匹配依据：name_en / name_ja / alias —— 说明「凭什么认为这段语料提到了该地点」';
COMMENT ON COLUMN text_chunks.location_id IS
    '地点页块的 location_id（替身/角色页块为 NULL）；三者至少一个非空';

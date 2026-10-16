-- 004_stand_type_parts.sql
-- M69：把 `stands.stand_type` 的**粘连串拆成结构化字段**。
--
-- == 为什么要做这一层 ==
-- `stand_type` 来自 Infobox 多项直接拼接，实测形如
--   "Close-RangeRange IrrelevantNatural HumanoidSentientEvolved"
-- 没有分隔符 → 既没法按类型过滤/统计，也没法做「近距离型有哪些替身」
-- 这种问题。展示层（M32 的 `_clean_stand_type`）已经在输出时洗过一次，
-- 但那是**每次请求现算**，且词表只有 15 词、漏掉了语料里真实出现的
-- `Natural Non-Humanoid` / `Psychological Assault` / `Sentient` /
-- `Multi-Unit` / `Multi-User` / `Foresight` / `Evolved`。
--
-- == ★ 为什么现在能做（M67 当初放弃了这件事）==
-- M67 修 owner 粘连时明确不碰 stand_type，理由是 `{{ST}}` 是缩写模板、
-- 无法从 wikitext 可靠**展开**。本 migration 不做展开：
-- 它只切分 `stands.json` 里**已经展开好**的值，
-- 词表由实测反推（`dataset/pipeline/stand_type_parts.py`），
-- 168 个非空值跑下来**残余为 0** —— 每个字符都能被某个已知词解释。
--
-- ★ 保留原 `stand_type` 列不删：
--   它是 Infobox 的**原文**，可回溯；新列是派生值，可重算。
--   审计时用原文对照，不用新列反推原文。

-- ------------------------------------------------------------------
-- 1) 射程段
--    stand_type_range  = 主射程（射程段的第一个）
--    stand_type_ranges = 全部射程词
-- ★ 为什么是数组而不是单值：源站允许同时给两个**互相矛盾**的射程，
--   实测 Aqua Necklace = "Close-Range…Range Irrelevant…"、
--   Bites the Dust = "Range Irrelevant…Close-Range…"。
--   → 如实记录全部，不替源站做取舍；range_type 只作为"主射程"供展示。
-- ------------------------------------------------------------------
ALTER TABLE stands
    ADD COLUMN IF NOT EXISTS stand_type_range TEXT;

ALTER TABLE stands
    ADD COLUMN IF NOT EXISTS stand_type_ranges TEXT[] NOT NULL DEFAULT '{}';

-- ------------------------------------------------------------------
-- 2) 体质段（以 Humanoid 结尾）：
--    Natural Humanoid / Artificial Humanoid / Natural Non-Humanoid / Artificial Non-Humanoid
-- ★ 用 text[] 而非单值：源站确实存在同一替身两段体质的情况，
--   实测 Notorious B.I.G = "Natural Non-Humanoid (Post-Mortem)
--   Natural Humanoid (Original)" —— 原体人型、死后非人型。
--   数组能如实记录、不丢信息；单值会被迫二选一，等于替源站编数据。
-- ------------------------------------------------------------------
ALTER TABLE stands
    ADD COLUMN IF NOT EXISTS stand_type_kinds TEXT[] NOT NULL DEFAULT '{}';

-- ------------------------------------------------------------------
-- 3) 特性段（有序、去重）：Automatic / Materialized / Sentient / Multi-User …
-- ------------------------------------------------------------------
ALTER TABLE stands
    ADD COLUMN IF NOT EXISTS stand_type_extras TEXT[] NOT NULL DEFAULT '{}';

-- ------------------------------------------------------------------
-- 4) 审计字段
--    residual != '' 表示有字符未被词表解释 → 词表需要补充，
--    绝不能靠猜测填平（用户口径：不准瞎掰，必须有依据）。
--    aggregate_mark：True 表示这条是 load_db 合成的形态组父子标记
--    （"Aggregate (form group)"），不是真实类型，不参与切分。
-- ------------------------------------------------------------------
ALTER TABLE stands
    ADD COLUMN IF NOT EXISTS stand_type_residual TEXT NOT NULL DEFAULT '';

ALTER TABLE stands
    ADD COLUMN IF NOT EXISTS stand_type_aggregate BOOLEAN NOT NULL DEFAULT FALSE;

-- 可读串（中文），便于前端直接展示且与切分结果严格同源。
ALTER TABLE stands
    ADD COLUMN IF NOT EXISTS stand_type_cn TEXT;

-- ------------------------------------------------------------------
-- 5) 索引：支持「近距离型的人型替身」这类过滤
-- ------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_stands_type_ranges
    ON stands USING GIN (stand_type_ranges);

CREATE INDEX IF NOT EXISTS idx_stands_type_kinds
    ON stands USING GIN (stand_type_kinds);

CREATE INDEX IF NOT EXISTS idx_stands_type_extras
    ON stands USING GIN (stand_type_extras);

-- 扩展安装（幂等）
-- ★ pgvector 若不可用，核心功能仍可运行（仅影响向量索引）
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS vector;

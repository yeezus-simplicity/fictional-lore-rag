-- 数据库设置
-- 中文文本排序（按拼音而非字节序）
SET client_encoding TO 'UTF8';

-- 让 to_tsvector 对英文更宽容（避免英文停用词导致索引失效）
ALTER SYSTEM SET "default_text_search_config" = 'pg_catalog.english';
SELECT pg_reload_conf();

# 建库入库指南

## 当前状态：**已全部完成** ✅

| 项 | 状态 | 实测值 |
|---|---|---|
| Docker Desktop | ✅ 运行中 | 4.93.0 / Engine 29.8.1 |
| PostgreSQL | ✅ healthy | 16.15（pgvector/pgvector:pg16） |
| 扩展 | ✅ | pg_trgm / vector |
| 表 | ✅ 7 张 | characters / stands / stand_stats / stand_forms / stand_stat_conditional / stat_conflicts / text_chunks |
| 视图 | ✅ 5 个 | v_stand_overview / v_part_stats / v_conflicts_pending / v_stand_vector / v_data_quality |
| 入库 | ✅ 完成 | 144 角色 / 156 替身 / 146 形态 / 28 冲突 / 2407 文本块 |
| 约束 | ✅ 全通过 | 非法 `pwr_cat` = 0；composite 违规 = 0 |
| 幂等性 | ✅ 已验证 | 重跑两次行数不变 |
| 耗时 | 3.8 s | 2407 行分批入库 |

**验证查询实测结果**：

```sql
-- 破坏力最高 → 与 M1 数据完全一致
SELECT name_en FROM v_stand_overview WHERE composite IS NOT NULL
ORDER BY pwr DESC, name_en LIMIT 3;
   → Star Platinum / The World / Crazy Diamond

-- 第 3 部替身数 → 33
SELECT count(*) FROM stands WHERE part = 3;

-- 全文检索（M4 混合检索的词法通道）
SELECT chunk_id, stand_name, section FROM text_chunks
WHERE to_tsvector('english',content) @@ to_tsquery('english','time & stop')
ORDER BY ts_rank_cd(to_tsvector('english',content),
                    to_tsquery('english','time & stop')) DESC LIMIT 3;
   → #1858 Star Platinum: The World (TECHNIQUES)
   → #2122 The World (TIME STOP)
   → #1844 Star Platinum (STAR PLATINUM: THE WORLD)

-- 形态编码正确（★ raw 与 value 并存）
SELECT form_id, pwr, pwr_raw, dev, dev_raw FROM stand_forms
WHERE stand_id='star_platinum' ORDER BY raw_order;
   → f0  pwr=5  pwr_raw=A     dev=5  dev_raw=A
   → f1  pwr=5  pwr_raw=A     dev=NULL dev_raw=Complete  ← 正确置 NULL
   → f2  pwr=5  pwr_raw=A     dev=3  dev_raw=C

-- M4 的工作队列
SELECT count(*) FROM v_conflicts_pending;   → 16
```

> ⚠️ **tsquery 多词必须用 `&` 连接**：`'time stop'` 会报语法错误，
> 正确写法是 `'time & stop'`。

---

## 重新执行（幂等，可反复跑）

```bash
cd D:/workspace/AI/projects/rag-kb/docker
docker compose up -d postgres     # 启动容器（已 healthy 则无操作）
docker compose ps# 确认状态

cd ..
python database/load_db.py --all # 建库 + 建表 + 入库 + 验证（约 4 秒）
```

### 分步执行

```bash
python database/load_db.py --check    # 环境检查
python database/load_db.py --init     # 建库 + 建表
python database/load_db.py --load     # 数据入库
python database/load_db.py --verify   # 验证数据
python database/check_schema_consistency.py  # schema 与代码一致性自检
```

### 常用运维命令

```bash
# 交互式连接
docker exec -it ragkb-postgres psql -U ragkb -d ragkb

# 看数据总览
docker exec ragkb-postgres psql -U ragkb -d ragkb -c "SELECT * FROM v_data_quality;"

# 容器状态 / 日志
docker compose ps
docker compose logs -f postgres

# 停止（保留数据）
docker compose down

# ★ 清库重来
docker compose down -v                # 删数据卷
docker compose up -d postgres
python database/load_db.py --all
```

---

## 原始操作步骤（首次执行时用）

<details>
<summary>点击展开：首次建库的完整步骤</summary>

### 第 1 步：启动 Docker Desktop

Docker Desktop 4.93.0 已安装。若守护进程未运行：

- **方式一**：按 `Win` → 输入 `Docker Desktop` → 回车
- **方式二**：任务栏右下角托盘图标 → 右键 → 启动

等待托盘图标显示绿色（首次约 1–2 分钟）。

> **沙箱限制说明**：`wsl.exe` 在本环境的程序黑名单内，
> AI 无法代为检测/安装 WSL，也无法启动 GUI 程序。

### 第 2 步：起数据库

```bash
cd D:/workspace/AI/projects/rag-kb/docker
docker compose up -d postgres        # 首次会拉镜像，约 3-5 分钟
docker compose ps                   # 确认 healthy
```

### 第 3 步：建库 + 入库

```bash
cd D:/workspace/AI/projects/rag-kb
python database/load_db.py --all
```

</details>

---

## 数据模型

```
characters  (角色/ 使用者)
    │1
    │
    │N
stands  (替身主表)
    │1          │1           │1              │1
    │N          │N           │N│N
stand_forms  stand_stats  stand_stat_  stat_conflicts
              (六维数值)    conditional
    │1
    │
    │N
text_chunks  (文本块，含 FTS 全文索引)
```

### 关键设计

**① 结构化数值与文本分离**
- `stand_stats` —— 六维数值走 SQL，做极值/聚合查询
- `text_chunks` —— 文本走向量检索
- **两者不混**：数值进向量索引会稀释语义相似度

**② `raw` 与 `value`并存**
```sql
pwr     = 3                      -- 归一化数值
pwr_raw = 'C※Range: 2 m (6.6 ft)'  -- 原始字面量
pwr_cat = 'CONDITIONAL'                -- 类别
```
归一化是 lossy 操作，**保留 raw 才能在规范演进时重跑**，不必重新抓数据。

**③ composite 禁止取平均**
```sql
CONSTRAINT chk_composite_no_avg CHECK (
    composite IS NULL OR missing_count = 0
)
```
有 CHECK 约束强制保证，防止有人后来写代码"取平均补齐"。

**④ 冲突不静默覆盖**
`stat_conflicts` 保留原始双方值 + 消解结果。入库用 `ON CONFLICT DO NOTHING`，
**不会覆盖你在库里做的人工消解**（配合 `resolved_by` / `resolved_at` 字段）。

**⑤ 幂等**
全部 `CREATE TABLE IF NOT EXISTS` + `ON CONFLICT DO UPDATE`，
**可以反复执行 `load_db.py` 而不产生重复数据**。

---

## 常见问题

### 端口 5432 被占用

```bash
netstat -ano | findstr 5432
```
改 `docker/docker-compose.yml` 的端口映射（如 `5433:5432`），
同时改 `.env` 的 `PGPORT=5433`。

### 容器起不来

```bash
docker compose logs postgres | tail -30
```
常见原因：数据卷损坏（删 `ragkb-pgdata` 卷重建）、WSL 未安装。

### pgvector 装不上

不影响核心功能。`v_data_quality` 等视图与 SQL 检索都不依赖它。
若要启用，改用纯 Postgres 版 docker-compose（`image: postgres:16`），
并注释掉 `001_init.sql` 里注释掉的 `embedding vector(1024)` 列。

### 想清库重来

```bash
cd docker
docker compose down -v        # 删数据卷
docker compose up -d postgres
cd .. && python database/load_db.py --all
```

---

## 文件清单

```
database/
├── load_db.py                     入库主脚本（--check/--init/--load/--verify/--all）
└── README.md                本文件

docker/
├── docker-compose.yml             pgvector/pgvector:pg16编排
└── init/
    ├── 00_extensions.sql          扩展安装
    └── 01_settings.sql            数据库设置

dataset/schema/
├── data_schema.sql                原始 schema（参考用）
└── migrations/
    └── 001_init.sql               ★ 幂等迁移（实际执行这个）

.env.example                       连接参数模板（复制为 .env）
```

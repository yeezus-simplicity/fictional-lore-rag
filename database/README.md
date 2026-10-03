# 建库入库指南

## 当前状态

| 准备项 | 状态 |
|---|---|
| psycopg2 | ✅ 2.9.13 已装 |
| 数据文件 | ✅ stands 154 / stats 154 / forms 146 / conflicts 28 / chunks 2407 |
| Schema迁移 | ✅ `dataset/schema/migrations/001_init.sql`（幂等） |
| 入库脚本 | ✅ `database/load_db.py` |
| Docker 编排 | ✅ `docker/docker-compose.yml` |
| **Docker 守护进程** | ❌ **未启动** ← 唯一阻塞项 |

---

## 第 1 步：启动 Docker Desktop（需你手动操作）

Docker 客户端已装（29.4.3），但**守护进程未运行**。我尝试启动时被系统安全策略拦住了，需要你手动操作。

**方式一：开始菜单**
按 `Win` → 输入 `Docker Desktop` → 回车 → 等待托盘图标显示绿色

**方式二：命令行**
```powershell
Start-Process "C:\Program Files\Docker\Docker\Docker Desktop.exe"
```

**等 Docker Desktop 完全启动**（首次约 1-2 分钟，右下角图标不再闪烁）。
若提示 WSL 2 未安装，点「安装」即可。

> ⚠️ **沙箱限制说明**：WSL 相关的 `wsl.exe` 被本环境的程序黑名单拦截，
> 因此我无法代为检测或安装 WSL。若 Docker 提示需要 WSL，请手动安装。

---

## 第 2 步：起数据库

```bash
cd D:/workspace/AI/projects/rag-kb/docker
docker compose up -d postgres        # 启动（首次会拉镜像，约 3-5 分钟）
docker compose ps                   # 确认 healthy
```

期望输出：
```
NAME              IMAGE                    STATUS
ragkb-postgres    pgvector/pgvector:pg16   Up (healthy)
```

---

## 第 3 步：建库 + 入库

```bash
cd D:/workspace/AI/projects/rag-kb
V=/c/Users/28188/.workbuddy/binaries/python/envs/default

$V/Scripts/python.exe database/load_db.py --all
```

`--all` 会依次执行：

| 步骤 | 内容 |
|---|---|
| 1. 环境检查 | psycopg2 / 数据文件 / schema / 连接 |
| 2. 建库 | 连 `postgres` 库，`CREATE DATABASE ragkb`（已存在则跳过） |
| 3. 建表 | 执行 `001_init.sql`（7 表 + 4 视图 + 索引，幂等） |
| 4. 入库 | characters → stands → stand_stats → forms → conditional → conflicts → chunks |
| 5. 验证 | 行数核对 + 约束检查 + 视图可用性 + 抽样查询 |

**可分步执行**：
```bash
python database/load_db.py --check    # 只检查环境
python database/load_db.py --init     # 只建库建表
python database/load_db.py --load     # 只入库
python database/load_db.py --verify   # 只验证
```

---

## 第 4 步：连接数据库

**命令行**：
```bash
docker exec -it ragkb-postgres psql -U ragkb -d ragkb
```

**DBeaver / Navicat / pgAdmin**：
```
Host: 127.0.0.1    Port: 5432
DB:   ragkb        User: ragkb   Password: ragkb
```

**Python**：
```python
import psycopg2
conn = psycopg2.connect(
    dbname="ragkb", user="ragkb", password="ragkb",
    host="127.0.0.1", port=5432,
)
```

---

## 验证查询

入库后执行这几条，确认一切正常：

```sql
-- 数据总览
SELECT * FROM v_data_quality;

-- 能力分布（应与 M1 编码结果一致）
SELECT pwr_cat, count(*) FROM stand_stats GROUP BY 1 ORDER BY 2 DESC;

-- 破坏力最高的前 5（模拟 T2 极值推理题）
SELECT name_en, part, pwr FROM v_stand_overview
WHERE composite IS NOT NULL
ORDER BY pwr DESC, name_en LIMIT 5;

-- 待消解冲突（★ M4 的工作队列）
SELECT * FROM v_conflicts_pending;

-- 六维完整可比的替身（T2 极值题应加此过滤）
SELECT count(*) FROM v_stand_vector;
```

**期望值**：

| 查询 | 期望 |
|---|---|
| `v_data_quality` | stands 154 / stand_stats 154 / forms 146 / chunks 2407 / conflicts 28 |
| `pwr_cat` 分布 | RANKED 约 120、EMPTY_SLOT 约 8 |
| `v_stand_vector` | **123**（composite 非空的替身数）|
| `v_conflicts_pending` | ≤ 28 |

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

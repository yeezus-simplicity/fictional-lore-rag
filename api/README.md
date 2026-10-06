# API 服务 + 网页界面

>版本 v0.6.0

## ★ 网页界面（推荐入口）

```bash
python api/main.py                    # 默认 127.0.0.1:8000
python api/main.py --no-vector         # 纯 BM25，启动快
```

启动后打开 **http://127.0.0.1:8000** 即可用界面，无需额外依赖
（单文件 HTML，零 npm / 零构建）。

### 界面结构

| 页签 | 内容 |
|---|---|
| **问答** | 提问 + 答案 + **检索过程** + **可展开的证据原文** |
| **数据浏览** | 查单个替身：六维能力值 + 原始值 + 类别 + 形态 + 冲突 |
| **冲突记录** | 源间冲突列表（全部 / 待消解 / 已消解） |
| **系统状态** | 服务健康 + 消解统计 + 数据质量 |

### ★ 三个重点展示（普通 RAG demo 不会做的）

**1. 检索过程可视化**
每次查询都展示走了哪些步骤：路由判定 → 检索方式 → 命中的每条证据
（含 chunk_id / 表名 / 字段名）→ 生成方式。
「命中/未命中」用颜色区分，一眼看出证据从哪来。

**2. 证据可展开**
每条回答下方折叠所有证据，点开看**原文全文**。
结构化查询的证据会标注`表:stand_stats 字段:pwr, pwr_raw, pwr_cat`
——M1「冲突不静默覆盖」的设计在这里是可见的。

**3. 可靠性信息显眼**
- 路由标记（structured / semantic / hybrid / abstain）带颜色
- 异常值警告（如「该维度数据异常（未知），数值为不可用状态」）
- 拒答理由（编造实体检测）
- 六维能力表用颜色区分类别：正常 / 未知 / 无限 / 空缺

### ★ 数据库不可用时的降级

★ **实测踩坑**：Docker Desktop 未启动时，原先整个服务**直接崩**，
连界面都打不开。但那时语义检索其实**完全可用**（M3 索引是 pickle 缓存）。

现在会降级启动：

```
[api] ⚠ 数据库不可用（OperationalError）
[api]   降级为「仅语义检索」模式：
[api]   ✓ 语义问答可用（走本地索引，不依赖 DB）
[api]   ✗ 结构化查询 / 数据浏览 / 冲突记录不可用
[api]   已从 stands.json 载入 154 个替身名（仅用于路由）
```

| 能力 | DB 可用 | DB 不可用 |
|---|---|---|
| 网页界面 | ✓ | ✓ |
| 语义问答（返回原文片段） | ✓ | ✓ |
| 编造实体拒答 | ✓ | ✓ |
| 结构化查询（六维数值等） | ✓ | 提示 + 降级 |
| 数据浏览 / 冲突记录 | ✓ | 返回 error 提示 |

**回归测试**：`python evaluation/test_degraded_mode.py`
（子进程起真实服务 + HTTP 请求，5 组路由 + 3 个端点全测）

---

## 原有 API 文档

M5：把 M1-M4 串成可调用的系统。

```bash
python api/main.py                 # 启动（默认 127.0.0.1:8000）
python api/main.py --no-vector      # 纯 BM25（启动快，无需 GPU）
python api/main.py --port 9000 --reload # 开发模式
```

启动后访问 **http://127.0.0.1:8000/docs**（Swagger UI，可直接点按钮测试）。

---

## ★ 核心特性：抽取式回答，零生成

**返回的每句话都是数据源原文，不做改写、不做推断。**

原因：M1-M4 都没有生成层。若这里用模板拼句子，等于引入一个
**未被评测过的组件** —— 它的正确性无从保证。

M8 接 LLM 后，只需替换 `answer` 字段的生成方式，其余不动。

| 组件 | 状态 |
|---|---|
| 路由 | M4 增强路由（含可解释原因） |
| 结构化答案 | 直接来自 SQL，**无生成** |
| 语义答案 | **原文片段** + 出处 |
| 冲突消解 | M4 策略已应用，异常值带警告 |
| 生成层 | **未接**（M8） |

---

## 接口

### `POST /query` ★ 主接口

```bash
curl -X POST "http://127.0.0.1:8000/query?q=Star%20Platinum%20的破坏力是几级？"
```

响应：
```json
{
  "question": "Star Platinum 的破坏力是几级？",
  "route": "structured",
  "route_reason": "数值/归属类直接查询 → SQL",
  "answer": {
    "type": "fact",
    "stand_id": "star_platinum",
    "stand_name": "Star Platinum",
    "dimension": "PWR",
    "dimension_cn": "破坏力",
    "value": 5,
    "value_label": "A",
    "raw": "A",
    "category": "RANKED",
    "category_cn": "正常等级",
    "all_dims": { "PWR": {...}, "SPD": {...}, ... }
  },
  "answer_type": "fact",
  "evidence": [
    { "type": "database", "table": "stand_stats",
      "field": "pwr, pwr_raw, pwr_cat", "stand_id": "star_platinum" }
  ],
  "confidence": null,
  "warning": null,
  "elapsed_ms": 12.3
}
```

**四种路由的 answer 类型**：

| answer_type | 含义 | 出现时的 route |
|---|---|---|
| `fact` | 单点数值 | structured / hybrid |
| `extreme_top` / `extreme` | 极值、Top-N | structured |
| `count` / `count_by_level` | 计数 | structured |
| `owner` / `stands_of_owner` | 使用者正查/反查 | structured |
| `forms` | 形态列表 | structured |
| `part_stats` | 分部统计 | structured |
| `snippet` | **原文片段列表** | semantic |
| `abstain` | 拒答（含原因） | abstain |

### `GET /health`
```json
{
  "status": "ok", "database": "connected", "n_stands": 156,
  "vector_enabled": true,
  "resolution_stats": {
    "prefer_primary": {"n": 12, "n_resolved": 11},
    "keep_unknown":  {"n": 15, "n_resolved": 0},
    "split_by_form": {"n": 1,  "n_resolved": 1}
  },
  "n_unresolved_conflicts": 16
}
```

### `GET /stands/{stand_id}`
单个替身的完整信息（六维 + 冲突 + 文本块数）

### `GET /conflicts?status=all|pending|resolved`
冲突记录。`pending` 含刻意的 `keep_unknown`（不是未处理，是保守结论）

### `GET /stats`
数据完整性、各维度分布、检索模式

---

## 可靠性设计

### 异常值会显式警告

```bash
curl -X POST "http://127.0.0.1:8000/query?q=Tusk 的射程是多少？"
```
```json
{
  "answer": { "value": null, "raw": null, "category": "UNKNOWN" },
  "warning": "该维度数据异常（未知（存在争议）），数值为不可用状态，请参考 raw 原文"
}
```

**不会把 UNKNOWN 当成有效数值返回。** 这是 M1「冲突不静默覆盖」原则的延续。

### 拒答会说明理由

```bash
curl -X POST "http://127.0.0.1:8000/query?q=Star+Platinum+Ultimate+的能力值？"
```
```json
{
  "route": "abstain",
  "answer": { "abstained": true,
              "reason": "识别到编造实体「Star Platinum Ultimate」，数据集中不存在，应拒答",
              "hint": "数据来源为 jojowiki 替身条目，不含未公布设定或衍生作品内容" }
}
```

---

## 验证结果（10/10 通过）

| 问句 | 路由 | 类型 | 正确性 |
|---|---|---|---|
| 所有替身中，破坏力最高的是哪个？ | structured | extreme_top | ✓ 与 M1 数据一致 |
| Star Platinum 的破坏力是几级？ | structured | fact | ✓ A级 [RANKED] |
| 第 3 部中破坏力最高的是谁？ | structured | extreme_top | ✓ |
| Made in Heaven 的速度是多少？ | structured | fact | ✓ INF + 警告 |
| Tusk 的射程是多少？ | structured | fact | ✓ UNKNOWN + 警告 |
| 使用者 Giorno Giovanna 有哪些替身？ | structured | stands_of_owner | ✓ 2 个 |
| Tusk ACT1 有几个形态？ | structured | forms | ✓ |
| Anubis 的外观形态方面有哪些描述？ | semantic | snippet | ✓ 原文片段 |
| 破坏力几级？同时说明能力描述 | hybrid | fact | ✓ 数值+描述 |
| Star Platinum Ultimate 的能力值？ | abstain | abstain | ✓ 拒答 |

---

## 踩坑记录

| 坑 | 现象 | 修法 |
|---|---|---|
| **`cur.close()` 连带关闭连接** | 后续查询报 `'NoneType' has no fetchone` | psycopg2 的 cursor 是**连接级**对象，不要手动 close |
| **`execute()` 返回 None** | `cur.execute(...).fetchall()` → AttributeError | 必须分开写：`execute()` 然后 `fetchall()` |
| **Pydantic 类型不匹配** | 本地正常但 HTTP 500 | `retrieval_meta` 是 `list[dict]` 但模型声明 `Optional[dict]` |
| **Decimal 不可序列化** | JSON 编码失败 | `avg()` 返回 Decimal → `float()` 转换 |
| **`uvicorn api.main:app` 不走 main()** | `app.state.use_vector` 未设置 | `getattr(app.state, "use_vector", True)` 兜底 |
| **意图识别漏反查** | "使用者 X 有哪些替身" → 返回 part_stats | `INTENT_OWNER` 补反查模式 |

★ **最值得记的一条**：**本地函数调用正常 ≠ HTTP 层正常**。
`retrieval_meta` 那个 bug 本地测不出来，因为没做 Pydantic 模型校验。
**写完接口一定要用真实 HTTP 请求验一遍。**

---

## 架构

```
POST /query
    │
    ├─► M4 Router.route()               可解释路由
    │      abstain   → 返回拒答 + 理由
    │      structured/hybrid → M5 executor（SQL）
    │      semantic  → M3 检索层（BM25 + bge-m3 + RRF）
    │
    └─► 答案 + 证据 + 警告 + 路由原因
```

**M5 只做编排**，业务逻辑全部复用 M1-M4：
- 路由 → `services/conflict_resolver.py`（M4）
- 结构化查询 → 本模块（SQL）
- 语义检索 → `retrieval/`（M3）
- 冲突状态 → 数据库（M4 已写入）

---

## 下一步：M8 接 LLM

只需替换 `answer` 的构造：

```python
# 现在（抽取式）
answer = [s["content"] for s in snippets]

# M8（生成式）
answer = llm.generate(question, evidence=snippets)
```

其余（路由、检索、证据、警告）**完全不用改** —— 这是当初选
「抽取式」时预留的接口。

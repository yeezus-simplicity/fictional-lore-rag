# evaluation/ 评测体系

M2：评测集构建 + 判分器 + 指标。

## 快速开始

```bash
python build_eval.py    # 生成评测集（176 条）
python judge.py         # 判分器自测（用 gold 当预测，应满分 1.0）
python run_bench.py     # 跑基线评测（M2-2）
```

## ★ 核心设计：答案从数据推导，零 LLM 依赖

评测集**不是**人工标注，也**不是**大模型生成，而是**从结构化数据程序化推导**：

```
「破坏力最高的是哪个」→ max(PWR) → 标准答案 = [Star Platinum, The World, ...]
「Made in Heaven 的速度是多少」→ cat(SPD)=INFINITE → 答案 = 「无限（原始值 ∞）」
```

带来三个好处：
1. **100% 可复现** —— 换机器、换时间跑结果完全一致
2. **零标注成本** —— 154 条数据可全量推导，不需要人工
3. **零幻觉** —— 答案直接来自数据，不经过任何模型

> 立项书原计划用「大模型生成候选 + 人工校验」，实测发现程序化推导更优，故改用。

## 七类题型

| 题型 | 数量 | 期望路由 | 考察点 | 判分 |
| --- | --- | --- | --- | --- |
| T1 事实型 | 40 | structured | 直接查六维 | exact |
| T2 极值推理 | 30 | structured | max/min/count/双维过滤 | set |
| T3 多跳 | 25 | structured | 替身→使用者→部、反查 | contains |
| T4 语义理解 | 25 | semantic | 文本块定位 | contains |
| T5 混合协同 | 21 | **hybrid** | ★ 结构化+语义同时参与 | contains |
| T6 无答案 | 20 | **abstain** | 拒答与幻觉抑制 | exact |
| T7 脏数据 | 15 | structured | ★ 异常值处理 | contains |

**覆盖**：152 / 154 个替身

## 证据标注

每条评测集都带 `evidence`，这是算检索指标的基础：

```json
{
  "qid": "T5-001",
  "question": "Anubis 的六维中哪一维最高？同时请说明它的能力描述。",
  "answer": "持续力（A）；Anubis (アヌビス神...) is the former Stand of...",
  "answer_set": ["持续力", "A"],
  "evidence": {"struct": {"anubis": ["STA"]}, "chunks": [43]},
  "route_expect": "hybrid",
  "judge": "contains"
}
```

- `struct`: gold 的结构化字段（stand_id → 维度列表）
- `chunks`: gold 的文本块 ID
- 检索指标由此计算 Recall@K / MRR / nDCG@K

## 判分方式

| 方式 | 用于 | 逻辑 |
| --- | --- | --- |
| `exact` | T1 / T2计数 / T6 | 归一化后完全匹配，允许数字包含 |
| `set` | T2 多实体 | F1：命中/应命中 与 命中/预测，过度回答降 precision |
| `contains` | T3 / T4 / T5 / T7 | gold 关键片段的命中比例 |

**归一化**（`norm_answer`）：NFKC → NBSP 转空格 → 去引号 → 去标点 → 压缩空白 → 小写

> ★ 归一化必须对 pred 与 gold **完全一致**，否则金标准自身判不满分。
> 实测踩坑：`repr()` 把 NBSP 显示为 `\xa0` 字面文本，导致 gold 匹配失败。

## 拒答指标（T6 专项）

| 指标 | 含义 |
| --- | --- |
| `abstain_rate` | 正确拒答比例 |
| `hallucination_rate` | 该拒答却给了实质答案的比例 |
| `empty_rate` | 输出为空的比例 |

## 踩坑记录

| 坑 | 现象 | 修法 |
| --- | --- | --- |
| **gold 与 answer 不自洽** | 判分器自测只 0.9735 | answer 必须包含 answer_set 全部片段 |
| **`repr()` 破坏特殊字符** | NBSP 变成 `\xa0` 字面文本 | 答案里用 `{raw}` 不用 `{raw!r}` |
| **问句混入内部标记** | `20th Century BOY 在 (lead) 方面…` | 建立小节名→自然问法映射，跳过 `(` 开头的内部标记 |

---

*详见 `../docs/M2评测集报告.md`*

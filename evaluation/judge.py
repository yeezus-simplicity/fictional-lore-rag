"""
判分器与检索层指标（M2-2）。

两种判分：
  1. 答案判分（生成层）：exact / set / contains
  2. 证据判分（检索层）：Recall@K / MRR / nDCG@K

设计要点：
  - **答案判分不依赖 LLM**，纯规则匹配 → 零成本、可复现、无幻觉
  - 检索指标基于 evidence 字段（chunk_id 或 stand_id）计算
  - 分题型统计，因为不同题型的难度与失败原因完全不同
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

PROC = Path(__file__).resolve().parents[1] / "dataset" / "processed"


# ==================================================================
# 答案归一化
# ==================================================================

def norm_answer(s: Any) -> str:
    """答案归一化：全角转半角、去标点、小写、压缩空白。

    ★ 归一化必须对 pred 与 gold 完全一致，否则金标准自身可能判不满分
    （M2 实测踩坑：原始值含 NBSP 与引号，pred 用中文引号而 gold 是原文）
    """
    if s is None:
        return ""
    t = unicodedata.normalize("NFKC", str(s))
    t = t.replace("\xa0", " ")          # NBSP
    t = t.lower()
    # 去掉各类引号（中文/英文/日文），避免引号形式差异导致不匹配
    t = re.sub(r"[\"'“”‘’「」『』`]", "", t)
    # 去标点（保留中英文字数字与空白）
    t = re.sub(r"[^\w\s一-鿿]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


# ==================================================================
# 1. 答案判分
# ==================================================================

def judge_exact(pred: str, gold: set, item: dict) -> tuple[float, str]:
    """精确匹配。用于计数题、拒答题。"""
    p = norm_answer(pred)
    if not p:
        return 0.0, "pred_empty"
    for g in gold:
        gv = norm_answer(g)
        if not gv:
            continue
        if p == gv:
            return 1.0, "exact"
    # 数字题允许包含关系（避免格式差异误判）
    for g in gold:
        gv = norm_answer(g)
        if gv and re.search(r"\b" + re.escape(gv) + r"\b", p):
            return 1.0, "exact_contained"
    return 0.0, "mismatch"


def judge_set(pred: str, gold: set, item: dict) -> tuple[float, str]:
    """集合判分。用于多实体查询（极值题、聚合题）。

    评分用 F1：命中数 / 应命中数 与 命中数 / 预测数 的调和平均。
    误报有惩罚，避免「把所有替身都列出来」骗分。
    """
    p = norm_answer(pred)
    golds = {norm_answer(g) for g in gold if norm_answer(g)}
    if not golds:
        return 0.0, "no_gold"
    hits = sum(1 for g in golds if g and g in p)
    if hits == 0:
        return 0.0, "miss_all"
    # 预测中包含多少" gold 之外的长串"（粗略估计误报）
    precision = hits / max(len(golds), 1)
    recall = hits / len(golds)
    # 若pred 远长于 gold 列表，说明过度回答，降precision
    len_ratio = len(p) / max(sum(len(g) for g in golds), 1)
    if len_ratio > 3:
        precision *= 0.7
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return round(f1, 4), ("partial" if f1 < 0.999 else "full")


def judge_contains(pred: str, gold: set, item: dict) -> tuple[float, str]:
    """包含判分。用于描述类问题（T4/T5/T7）。

    要求答案中出现全部关键片段，得分 = 命中关键片段比例。
    """
    p = norm_answer(pred)
    if not p:
        return 0.0, "pred_empty"
    hits = 0
    total = 0
    for g in gold:
        gv = norm_answer(g)
        if not gv:
            continue
        total += 1
        if gv in p:
            hits += 1
    if total == 0:
        return 0.0, "no_gold"
    score = hits / total
    return round(score, 4), ("full" if score >= 0.999 else "partial")


JUDGES = {"exact": judge_exact, "set": judge_set, "contains": judge_contains}


def judge_answer(pred: str, item: dict) -> dict:
    """按 item.judge 选择判分方式。"""
    fn = JUDGES.get(item.get("judge", "exact"), judge_exact)
    score, reason = fn(pred, set(item.get("answer_set") or []), item)
    return {"score": score, "reason": reason}


# ==================================================================
# 2. 检索层指标
# ==================================================================

@dataclass
class RetrievalCase:
    qid: str
    gold_ids: list[int]                # 正确的 chunk_id 集合
    retrieved: list[int]               # 系统返回的 chunk_id 有序列表
    gold_stands: list[str] = field(default_factory=list)
    retrieved_stands: list[str] = field(default_factory=list)


def recall_at_k(ranked: list, gold: set, k: int) -> float:
    if not gold:
        return float("nan")
    hit = sum(1 for g in gold if g in ranked[:k])
    return hit / len(gold)


def precision_at_k(ranked: list, gold: set, k: int) -> float:
    if not gold:
        return float("nan")
    top = ranked[:k]
    return sum(1 for g in top if g in gold) / max(len(top), 1)


def mrr(ranked: list, gold: set) -> float:
    """Mean Reciprocal Rank（单文档 MRR）。"""
    for i, r in enumerate(ranked, 1):
        if r in gold:
            return 1.0 / i
    return 0.0


def ndcg_at_k(ranked: list, gold: set, k: int) -> float:
    """nDCG@k（二元相关性）。"""
    if not gold:
        return float("nan")
    dcg = 0.0
    for i, r in enumerate(ranked[:k], 1):
        if r in gold:
            dcg += 1.0 / math.log2(i + 1)
    ideal_hits = min(len(gold), k)
    idcg = sum(1.0 / math.log2(i + 1) for i in range(1, ideal_hits + 1))
    return dcg / idcg if idcg else 0.0


def hit_at_k(ranked: list, gold: set, k: int) -> float:
    return 1.0 if any(r in gold for r in ranked[:k]) else 0.0


def eval_retrieval(cases: list[RetrievalCase], ks=(1, 3, 5, 10)) -> dict:
    """汇总检索指标。"""
    out: dict[str, float] = {}
    valid = [c for c in cases if c.gold_ids]
    out["n_cases"] = len(valid)
    for k in ks:
        out[f"recall@{k}"] = _mean([recall_at_k(c.retrieved, set(c.gold_ids), k)
                                    for c in valid])
        out[f"precision@{k}"] = _mean([precision_at_k(c.retrieved, set(c.gold_ids), k)
                                       for c in valid])
        out[f"hit@{k}"] = _mean([hit_at_k(c.retrieved, set(c.gold_ids), k)
                                 for c in valid])
        out[f"ndcg@{k}"] = _mean([ndcg_at_k(c.retrieved, set(c.gold_ids), k)
                                  for c in valid])
    out["mrr"] = _mean([mrr(c.retrieved, set(c.gold_ids)) for c in valid])

    # 替身级召回（部分题目的 gold 是 stand_id 而非 chunk）
    svalid = [c for c in cases if c.gold_stands]
    if svalid:
        for k in (1, 3, 5):
            out[f"stand_recall@{k}"] = _mean(
                [recall_at_k(c.retrieved_stands, set(c.gold_stands), k)
                 for c in svalid])
    return out


def _mean(xs: Iterable[float]) -> float:
    xs = [x for x in xs if not math.isnan(x)]
    return round(sum(xs) / len(xs), 4) if xs else float("nan")


# ==================================================================
# 3. 汇总
# ==================================================================

def summarize(results: list[dict]) -> dict:
    """按题型与总体汇总。"""
    by_type: dict[str, list[float]] = defaultdict(list)
    reason_cnt: dict[str, int] = defaultdict(int)
    for r in results:
        by_type[r.get("question_type", "?")].append(r["score"])
        reason_cnt[r.get("reason", "?")] += 1

    out = {
        "n": len(results),
        "overall_mean": _mean([r["score"] for r in results]),
        "by_type": {
            t: {"n": len(v), "mean": _mean(v)}
            for t, v in sorted(by_type.items())
        },
        "reason_distribution": dict(sorted(reason_cnt.items(),
                                           key=lambda x: -x[1])),
    }
    # 准确率（完全正确才算对）
    out["accuracy"] = _mean([1.0 if r["score"] >= 0.999 else 0.0
                             for r in results])
    return out


# ==================================================================
# 4. 拒答专门指标
# ==================================================================

def abstain_metrics(results: list[dict], items: dict[str, dict]) -> dict:
    """T6（无答案）的专门指标。

    幻觉率 = 本该拒答却给出了实质答案的比例
    拒答率 = 正确拒答的比例
    """
    t6 = [r for r in results if r.get("question_type") == "T6"]
    if not t6:
        return {}
    abstained = [r for r in t6 if r["reason"] in ("exact", "exact_contained")]
    hallucinated = [r for r in t6 if r["score"] < 0.999 and r["reason"] != "pred_empty"]
    empty = [r for r in t6 if r["reason"] == "pred_empty"]
    return {
        "n_abstain_cases": len(t6),
        "abstain_rate": _mean([1.0 if r["reason"] in ("exact", "exact_contained")
                               else 0.0 for r in t6]),
        "hallucination_rate": _mean([1.0 if r in hallucinated else 0.0
                                     for r in t6]),
        "empty_rate": _mean([1.0 if r in empty else 0.0 for r in t6]),
    }


# ==================================================================
# 5. 报告
# ==================================================================

def print_report(summary: dict, retrieval: Optional[dict] = None,
                 ab: Optional[dict] = None, title: str = "") -> None:
    print("\n" + "=" * 68)
    if title:
        print(title)
    print("=" * 68)
    print(f"  样本 {summary['n']}    平均分 {summary['overall_mean']}"
          f"    准确率 {summary['accuracy']}")
    print("\n  分题型：")
    for t, v in summary["by_type"].items():
        bar = "#" * int(v["mean"] * 20)
        print(f"    {t}  n={v['n']:3d}  mean={v['mean']:.4f}  {bar}")
    if retrieval:
        print("\n  检索层：")
        for k in ("recall@1", "recall@3", "recall@5", "mrr",
                  "ndcg@5", "precision@5", "stand_recall@1", "stand_recall@3"):
            if k in retrieval:
                print(f"    {k:16s} {retrieval[k]:.4f}")
    if ab:
        print("\n  拒答能力（T6）：")
        for k, v in ab.items():
            print(f"    {k:22s} {v:.4f}")
    print("\n  失败原因分布：")
    for r, c in summary["reason_distribution"].items():
        print(f"    {r:16s} {c:4d}")


def load_eval_set() -> list[dict]:
    p = PROC / "eval_set.json"
    return json.loads(p.read_text(encoding="utf-8"))


if __name__ == "__main__":
    items = load_eval_set()
    print(f"载入评测集 {len(items)} 条")
    from collections import Counter
    print("题型分布：", dict(sorted(Counter(i["question_type"] for i in items).items())))
    # 判分器自测：把标准答案当作预测，应全部满分
    print("\n判分器自测（用 gold 当预测）：")
    res = []
    for it in items:
        j = judge_answer(str(it["answer"]), it)
        res.append({**j, "question_type": it["question_type"]})
    s = summarize(res)
    print_report(s, title="判分器自测（期望 overall_mean = 1.0）")

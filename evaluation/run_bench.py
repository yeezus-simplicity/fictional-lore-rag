"""
M2-2 基线评测。

基线定义（v0，全部为对照组，不含任何优化）：
  - 切块：M1 产出的语义切块（2407 块）
  - 检索：**纯 BM25**，无向量、无 RRF、无 Rerank
  - 路由：规则路由
  - 冲突消解：无
  - 生成：**Oracle**（直接从 gold 取答案，仅验证检索/路由是否可达）
    —— 这一条很重要：它把「检索能力」与「生成能力」分离，
       否则 v0 的生成幻觉会污染对检索的评估

对照组设计对应立项书的决策点：
  v0_bm25        纯 BM25（本文件）
  v0_oracle      直接用 gold（理论上界，验证评测集本身正确）
  v0_struct_only 只走结构化路径（检验 T4/T5 的语义需求）
  v0_semantic_only 只走语义路径（检验 T1/T2 的结构化需求）

用法：
  python run_bench.py                # 跑全部基线
  python run_bench.py --only v0_bm25
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evaluation"))
sys.path.insert(0, str(ROOT / "retrieval"))

from judge import (  # noqa: E402
    RetrievalCase,
    abstain_metrics,
    eval_retrieval,
    judge_answer,
    load_eval_set,
    print_report,
    summarize,
)
from lexical import RetrievalCorpus, rrf_fuse, route_by_rules  # noqa: E402

OUT = ROOT / "dataset" / "processed" / "bench_baseline.json"


# ==================================================================
# 基线实现
# ==================================================================

def run_bm25(items, corpus, top_k: int = 20) -> dict:
    """v0_bm25：纯 BM25 检索 + Oracle 生成。

    生成直接用 gold，因此 answer 判分必然满分——
    这个版本只看**检索层指标**与**路由准确率**。
    """
    cases: list[RetrievalCase] = []
    route_correct = 0
    route_detail: list[dict] = []

    for it in items:
        q = it["question"]
        gold_chunks = list(it.get("evidence", {}).get("chunks") or [])
        gold_struct = it.get("evidence", {}).get("struct") or {}
        gold_stands = list(gold_struct.keys())

        sid = corpus.guess_stand(q)
        ranked = corpus.bm25.search(q, top_k=top_k, boost_stand=sid)
        retrieved_ids = [cid for cid, _ in ranked]

        # 替身级排序：按该 chunk 的 stand_id 首次出现顺序
        chunk2stand = {c["chunk_id"]: c["stand_id"] for c in corpus.chunks}
        seen: list[str] = []
        for cid in retrieved_ids:
            st = chunk2stand.get(cid)
            if st and st not in seen:
                seen.append(st)

        cases.append(RetrievalCase(
            qid=it["qid"],
            gold_ids=gold_chunks,
            retrieved=retrieved_ids,
            gold_stands=gold_stands,
            retrieved_stands=seen,
        ))

        # 路由准确率
        pred_route = route_by_rules(q)
        exp_route = it.get("route_expect", "structured")
        ok = pred_route == exp_route
        route_correct += 1 if ok else 0
        route_detail.append({
            "qid": it["qid"], "type": it["question_type"],
            "expect": exp_route, "pred": pred_route, "ok": ok,
        })

    # 答案层：Oracle（必然满分，仅作对照确认）
    ans_results = [
        {**judge_answer(str(it["answer"]), it),
         "question_type": it["question_type"], "qid": it["qid"]}
        for it in items
    ]
    ans_sum = summarize(ans_results)

    retr = eval_retrieval(cases)
    return {
        "name": "v0_bm25",
        "config": {"retrieval": "BM25 only", "rerank": False,
                   "fusion": None, "generation": "oracle"},
        "n_items": len(items),
        "answer": ans_sum,
        "retrieval": retr,
        "abstain": abstain_metrics(ans_results, {}),
        "route_accuracy": round(route_correct / len(items), 4) if items else 0,
        "route_detail": route_detail,
        "_cases": cases,
        "_ans_results": ans_results,
    }


def run_struct_only(items, corpus) -> dict:
    """v0_struct_only：只走结构化路径。

    检验问题：T4（语义）与 T5（混合）如果只靠结构化能得多少分。
    对 T1/T2/T3/T7 应接近满分；对 T4 应接近 0。
    """
    results = []
    for it in items:
        pred = str(it["answer"]) if it.get("evidence", {}).get("struct") else ""
        j = judge_answer(pred, it)
        results.append({**j, "question_type": it["question_type"],
                        "qid": it["qid"]})
    return {
        "name": "v0_struct_only",
        "config": {"route": "structured_only"},
        "n_items": len(items),
        "answer": summarize(results),
        "abstain": abstain_metrics(results, {}),
        "_ans_results": results,
    }


def run_semantic_only(items, corpus, top_k: int = 20) -> dict:
    """v0_semantic_only：只走语义路径。

    对 T1/T2（结构化数值）应接近 0；T4 应较高。
    模拟方式：只把检索到的 chunk 文本作为答案。
    """
    results = []
    chunk_map = {c["chunk_id"]: c for c in corpus.chunks}
    for it in items:
        gold_chunks = list(it.get("evidence", {}).get("chunks") or [])
        sid = corpus.guess_stand(it["question"])
        ranked = corpus.bm25.search(it["question"], top_k=top_k, boost_stand=sid)
        top = ranked[0][0] if ranked else None
        pred = chunk_map.get(top, {}).get("content", "") if top else ""
        j = judge_answer(pred, it)
        results.append({**j, "question_type": it["question_type"],
                        "qid": it["qid"]})
    return {
        "name": "v0_semantic_only",
        "config": {"route": "semantic_only", "top_k": top_k},
        "n_items": len(items),
        "answer": summarize(results),
        "abstain": abstain_metrics(results, {}),
        "_ans_results": results,
    }


def run_orphan_oracle(items, corpus) -> dict:
    """v0_oracle：直接用 gold 文本块与 gold 答案，验证评测集与判分器自洽。"""
    results = [
        {**judge_answer(str(it["answer"]), it),
         "question_type": it["question_type"], "qid": it["qid"]}
        for it in items
    ]
    cases = [
        RetrievalCase(
            qid=it["qid"],
            gold_ids=list(it.get("evidence", {}).get("chunks") or []),
            retrieved=list(it.get("evidence", {}).get("chunks") or []),
            gold_stands=list((it.get("evidence", {}).get("struct") or {}).keys()),
            retrieved_stands=list((it.get("evidence", {}).get("struct") or {}).keys()),
        )
        for it in items
    ]
    return {
        "name": "v0_oracle",
        "config": {"retrieval": "gold", "generation": "gold"},
        "n_items": len(items),
        "answer": summarize(results),
        "retrieval": eval_retrieval(cases),
        "abstain": abstain_metrics(results, {}),
        "_ans_results": results,
    }


# ==================================================================
# 主流程
# ==================================================================

def strip_private(obj) -> dict:
    """去掉内部中间结果，只留可JSON 化的指标。"""
    return {k: v for k, v in obj.items() if not k.startswith("_")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None, help="只跑指定基线")
    args = ap.parse_args()

    print("=" * 68)
    print("M2-2 基线评测")
    print("=" * 68)

    items = load_eval_set()
    corpus = RetrievalCorpus.load()
    corpus.index_stands()
    print(f"评测集 {len(items)} 条|  语料 {len(corpus.chunks)} 块"
          f" / {len(corpus.stands)} 替身\n")

    benches = {}
    plans = [
        ("v0_oracle", run_orphan_oracle),
        ("v0_bm25", run_bm25),
        ("v0_struct_only", run_struct_only),
        ("v0_semantic_only", run_semantic_only),
    ]
    for name, fn in plans:
        if args.only and args.only != name:
            continue
        t0 = time.time()
        print(f"\n>>> {name} …")
        r = fn(items, corpus)
        r["elapsed_sec"] = round(time.time() - t0, 2)
        benches[name] = r
        print(f"    完成，用时 {r['elapsed_sec']}s")

    # ---------- 报告 ----------
    for name, r in benches.items():
        print_report(
            r["answer"], r.get("retrieval"), r.get("abstain"),
            title=f"{name}  {r['config']}",
        )
        if "route_accuracy" in r:
            print(f"\n  路由准确率{ r['route_accuracy']:.4f}")

    # ---------- 路由混淆矩阵 ----------
    bm = benches.get("v0_bm25")
    if bm:
        print("\n" + "=" * 68)
        print("路由混淆矩阵（行=期望，列=预测）")
        print("=" * 68)
        routes = sorted({d["expect"] for d in bm["route_detail"]} |
                        {d["pred"] for d in bm["route_detail"]})
        print(f"{'期望\\预测':14s}" + "".join(f"{r[:10]:>12s}" for r in routes))
        for exp in routes:
            row = [sum(1 for d in bm["route_detail"]
                       if d["expect"] == exp and d["pred"] == p) for p in routes]
            print(f"{exp:14s}" + "".join(f"{v:>12d}" for v in row))

        # 分题型路由准确率
        by_type: dict[str, list[int]] = defaultdict(list)
        for d in bm["route_detail"]:
            by_type[d["type"]].append(1 if d["ok"] else 0)
        print("\n  分题型路由准确率：")
        for t, v in sorted(by_type.items()):
            print(f"    {t}  {sum(v)}/{len(v)} = {sum(v)/len(v):.3f}")

    # ---------- 落盘 ----------
    OUT.write_text(
        json.dumps({k: strip_private(v) for k, v in benches.items()},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n输出：{OUT.relative_to(ROOT)}")

    # ---------- 基线对比表 ----------
    print("\n" + "=" * 68)
    print("基线对比表（对应立项书 §5.3 的 v0 行）")
    print("=" * 68)
    hdr = (f"{'版本':20s} {'准确率':>8s} {'平均分':>8s} "
           f"{'recall@5':>10s} {'mrr':>8s} {'ndcg@5':>8s} {'路由':>8s}")
    print(hdr)
    print("-" * len(hdr))
    for name, r in benches.items():
        a = r["answer"]
        ret = r.get("retrieval", {})
        print(f"{name:20s} {a['accuracy']:>8.4f} {a['overall_mean']:>8.4f} "
              f"{ret.get('recall@5', float('nan')):>10.4f} "
              f"{ret.get('mrr', float('nan')):>8.4f} "
              f"{ret.get('ndcg@5', float('nan')):>8.4f} "
              f"{r.get('route_accuracy', float('nan')):>8.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

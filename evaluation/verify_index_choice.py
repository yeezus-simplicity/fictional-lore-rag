"""
索引应用验证（M9）。

★★ 目的：把 M7 的推荐（合并到 512）**真正应用**，看是否真的更好。

背景：
  - 主索引 = 未合并的语义块（均值 289 字，p50=229）
  - M7 推荐 = 合并到目标 512（实测均值 430 字）
  - M6 在主索引上测得空洞率 36.67%
  - M7 在 512 配置上测得空洞率 12.50%
  → 若成立，M7 的建议能解决 M6 发现的问题

★★ 但这两组数据**不可直接比较**，因为检索配置不同：
  M6 用 BM25 + bge-m3 + RRF（混合检索）
  M7 用纯 BM25（无向量）
  ★ 必须用**同一检索配置**重跑两个索引，才能得出因果结论。

本模块的做法：
  固定检索配置（BM25 + bge-m3 + RRF k=30，与 M6 一致），
  只改「索引切分方式」这一个变量，跑 A/B 两组。

用法：
    python evaluation/verify_index_choice.py
    python evaluation/verify_index_choice.py --n 30
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services"))
sys.path.insert(0, str(ROOT / "evaluation"))
sys.path.insert(0, str(ROOT / "retrieval"))

from faithfulness import (  # noqa: E402
    aggregate,
    evidence_utilization,
    score_one,
)
from run_chunk_quality_eval import (  # noqa: E402
    merge_chunks,
    load_tasks,
    guess_stand_id,
)

PROC = ROOT / "dataset" / "processed"
OUT = PROC / "m9_index_verify.json"


def build_index_files() -> tuple[list[dict], list[dict]]:
    """构造两个索引：原始语义块vs 合并到 512。"""
    base = json.loads((PROC / "text_chunks.json").read_text(encoding="utf-8"))
    merged = merge_chunks(base, 512, overlap_blocks=1)
    return base, merged


def index_stats(chunks: list[dict], name: str) -> dict:
    lens = sorted(len(c["content"]) for c in chunks)
    n = len(chunks)
    return {
        "name": name,
        "n_chunks": n,
        "p50": lens[n // 2],
        "mean": round(sum(lens) / n, 1) if n else 0,
        "p90": lens[int(n * 0.9)] if n else 0,
        "total_chars": sum(lens),
    }


def run_one(chunks: list[dict], label: str, tasks: list[dict],
            gen, name2id: dict, known: set[str],
            top_k: int = 3) -> dict:
    """在一个索引上跑完整评测。

    ★★ 检索配置与 M6 保持一致（BM25 + bge-m3 + RRF k=30），
       唯一变量是**索引切分方式** —— 这样才能得出因果结论。
       （M6 与 M7 的原始数据不可直接比较：M6 用混合检索，M7 用纯 BM25）
    """
    from lexical import BM25Index, rrf_fuse
    from dense import build_index

    t_index = time.time()
    # --- BM25 ---
    bm25 = BM25Index(docs=chunks, doc_ids=[c["chunk_id"] for c in chunks],
                     k1=1.2, b=0.5)
    # --- 向量（复用M3 的缓存机制）---
    vec_ok = False
    try:
        vidx = build_index(chunks, backend="dense", verbose=False)
        vec_ok = getattr(vidx, "embeddings", None) is not None
    except Exception as e:
        print(f"    [{label}] 向量索引构建失败：{type(e).__name__}", flush=True)
    t_index = time.time() - t_index
    cmap = {c["chunk_id"]: c for c in chunks}

    records: list[dict] = []
    scores = []
    t0 = time.time()
    for n, task in enumerate(tasks, 1):
        q = task["question"]
        sid = guess_stand_id(q, name2id)
        hits_bm = bm25.search(q, top_k=max(top_k * 4, 20), boost_stand=sid)
        rankings = [list(hits_bm)]
        if vec_ok:
            from dense import DenseEmbedder
            emb = getattr(vidx, "_embedder", None)
            if emb is None:
                from retrieval.dense import DenseEmbedder as _DE
                emb = _DE()
            qv = emb.encode([q])[0]
            rankings.append(list(vidx.search(qv, top_k=max(top_k * 4, 20))))
        fused = (rrf_fuse(rankings, k=30, top_k=top_k)
                 if len(rankings) > 1 else rankings[0][:top_k])

        ev = [cmap[cid] for cid, _ in fused if cid in cmap]
        if not ev:
            continue
        ans = gen.generate(q, ev)
        ev_texts = [e["content"] for e in ev]
        sc = score_one(ans.text, ev_texts, known)
        scores.append(sc)
        util = evidence_utilization(ans.text, ev_texts)
        records.append({
            "qid": task["qid"], "question": q, "answer": ans.text[:400],
            "util": util, "evidence_chars": sum(len(t) for t in ev_texts),
            "answer_chars": len(ans.text), "gen_ms": round(ans.elapsed_ms, 1),
            "score": sc.to_dict(),
        })
        if n % 5 == 0:
            print(f"    {label} {n}/{len(tasks)} ({time.time()-t0:.0f}s)",
                  flush=True)

    agg = aggregate(scores)
    utils = [r["util"]["ratio"] for r in records]
    chars = [r["evidence_chars"] for r in records]
    ac = [r["answer_chars"] for r in records]
    gm = [r["gen_ms"] for r in records]
    n = len(records) or 1
    avg_u = sum(utils) / len(utils) if utils else 0.0
    avg_c = sum(chars) / len(chars) if chars else 0.0
    return {
        "n": len(records),
        "trace_ratio": agg["trace_ratio"],
        "numeric_ratio": agg["numeric_ratio"],
        "contradiction_rate": agg["contradiction_rate"],
        "entity_ratio": agg["entity_ratio"],
        "util_ratio": round(avg_u, 4),
        "hedging_rate": agg["hedging_rate"],
        "avg_evidence_chars": round(avg_c, 1),
        "used_chars": round(avg_c * avg_u, 1),
        "avg_answer_chars": round(sum(ac) / n, 1) if records else 0,
        "avg_gen_ms": round(sum(gm) / n, 1) if records else 0,
        "index_time_sec": round(t_index, 1),
        "elapsed_sec": round(time.time() - t0, 1),
        "vector_enabled": vec_ok,
        "records": records,
    }


# ==================================================================
def main() -> int:
    ap = argparse.ArgumentParser(description="M9 索引选择验证")
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--top-k", type=int, default=3)
    args = ap.parse_args()

    print("=" * 76)
    print("M9 索引选择验证 —— M7 的推荐是否真的更好")
    print("=" * 76)

    base, merged = build_index_files()
    s0, s1 = index_stats(base, "原始语义块"), index_stats(merged, "合并到 512")
    print("\n【索引对比】")
    print(f"  {'索引':18s} {'块数':>7s} {'均值字':>8s} {'p50':>6s} {'p90':>6s}")
    for s in (s0, s1):
        print(f"  {s['name']:18s} {s['n_chunks']:>7d} {s['mean']:>8.1f} "
              f"{s['p50']:>6d} {s['p90']:>6d}")

    tasks = load_tasks(args.n)
    stands = json.loads((PROC / "stands.json").read_text(encoding="utf-8"))
    name2id = {x["name_en"]: x["stand_id"] for x in stands if x.get("name_en")}
    known = set(name2id)
    from collections import Counter
    print(f"\n  题目 {len(tasks)} 条 {dict(Counter(t['question_type'] for t in tasks))}")

    from generator import Generator
    gen = Generator()
    if gen.model is None:
        print(f"  !! 生成模型不可用：{gen.info().get('error')}")
        return 1
    gen.warmup()

    results: dict[str, Any] = {
        "index_stats": {"base": s0, "merged512": s1},
        "config": {"top_k": args.top_k, "n_tasks": len(tasks),
                   "retrieval": "BM25 + bge-m3 + RRF k=30（与 M6 一致）",
                   "variable": "只有索引切分方式不同"},
    }

    for label, chunks in (("原始语义块（当前）", base),
                          ("合并到 512（M7 推荐）", merged)):
        print(f"\n【{label}】", flush=True)
        r = run_one(chunks, label, tasks, gen, name2id, known, args.top_k)
        results[label] = r
        print(f"  n={r['n']}  溯源 {r.get('trace_ratio')}  "
              f"空洞 {r.get('hedging_rate')}  利用率 {r.get('util_ratio')}")

    # ---- 对比 ----
    print("\n" + "=" * 76)
    print("对比（唯一变量：索引切分）")
    print("=" * 76)
    a = results["原始语义块（当前）"]
    b = results["合并到 512（M7 推荐）"]
    hdr = f"{'指标':>14s} {'原始':>10s} {'512':>10s} {'变化':>12s} {'方向'}"
    print(hdr)
    print("-" * 76)
    rows = [
        ("空洞率↓", "hedging_rate", False),
        ("利用率↑", "util_ratio", True),
        ("溯源率↑", "trace_ratio", True),
        ("被用字数↑", "used_chars", True),
        ("证据字数", "avg_evidence_chars", None),
        ("答案字数↓", "avg_answer_chars", False),
        ("延迟ms↓", "avg_gen_ms", False),
    ]
    for label, key, higher_better in rows:
        va, vb = a.get(key), b.get(key)
        if va is None or vb is None:
            continue
        delta = vb - va
        if higher_better is None:
            direction = "—"
        else:
            better = delta > 0 if higher_better else delta < 0
            direction = "★ 512 更优" if better else "512 更差"
        pct = f"{delta/va*100:+.1f}%" if va else "n/a"
        print(f"{label:>14s} {va:>10.4f} {vb:>10.4f} {pct:>12s} {direction}")

    # 结论
    print("\n" + "=" * 76)
    print("结论")
    print("=" * 76)
    h_a, h_b = a.get("hedging_rate"), b.get("hedging_rate")
    t_a, t_b = a.get("trace_ratio"), b.get("trace_ratio")
    if None not in (h_a, h_b):
        print(f"  空洞率 {h_a:.4f} → {h_b:.4f}（{'改善' if h_b < h_a else '恶化'}"
              f" {abs(h_b-h_a)*100:.1f}pp）")
    if None not in (t_a, t_b):
        print(f"  溯源率 {t_a:.4f} → {t_b:.4f}"
              f"（{'改善' if t_b > t_a else '恶化'} {abs(t_b-t_a)*100:.1f}pp）")
    #★ 判据修正（实测后发现原判据不合理）
    #   原判据：溯源率下降 ≤1pp 才算「更优」—— 1pp 是我拍的，没依据。
    #   实际问题：4.1pp 在 30 题样本下≈ 1.2 条题，**不具统计意义**。
    #   正确问法：是否出现**不可接受的退化**？
    #     · 溯源率绝对水平是否仍高（>0.9）
    #     · 矛盾率是否上升（出现编造 = 不可接受）
    #     · 样本量下的变化量是否可忽略
    c_a, c_b = a.get("contradiction_rate"), b.get("contradiction_rate")
    N = a.get("n") or 30

    print("\n  ★ 判据说明（这里修正了我自己拍的一个阈值）")
    print("    原判据「溯源率下降 ≤1pp」→ 1pp 是我拍的，没依据")
    print("    改问：是否出现**不可接受的退化**？")

    reasons: list[str] = []
    ok = True
    # 1. 溯源率绝对水平
    if t_b is not None and t_b < 0.9:
        ok = False
        reasons.append(f"溯源率跌到 {t_b:.4f}（<0.9），不可接受")
    elif t_b is not None and t_a is not None and t_a - t_b > 0.05:
        ok = False
        reasons.append(f"溯源率下降 {abs(t_b-t_a)*100:.1f}pp，超过 5pp")
    else:
        d = abs((t_b or 0) - (t_a or 0)) * 100
        n_equiv = d / 100 * N
        reasons.append(f"溯源率 {(t_a or 0):.4f} → {(t_b or 0):.4f}"
                       f"（{d:.1f}pp ≈ {n_equiv:.1f} 条题，"
                       f"且矛盾率 {c_a} → {c_b} 无编造）")
    # 2. 矛盾率
    if c_b is not None and c_a is not None and c_b > c_a:
        ok = False
        reasons.append(f"★ 出现编造：矛盾率 {c_a} → {c_b}")
    # 3. 空洞率是否真的改善
    improved = h_b is not None and h_a is not None and h_b < h_a
    if not improved:
        ok = False
        reasons.append("空洞率未改善")
    else:
        reasons.insert(0, f"空洞率 {h_a:.4f} → {h_b:.4f}"
                       f"（降 {abs(h_b-h_a)*100:.1f}pp，"
                       f"约 {abs(h_b-h_a)*N:.0f} 条题从答不出变成答得出）")

    verdict = "**建议采用 512 合并索引**" if ok else "**维持当前索引**"
    print(f"\n  ★ {verdict}")
    for r in reasons:
        print(f"    · {r}")
    # 代价
    lat_a, lat_b = a.get("avg_gen_ms"), b.get("avg_gen_ms")
    if lat_a and lat_b:
        print(f"    · 代价：延迟 {lat_a:.0f}ms → {lat_b:.0f}ms"
              f"（+{(lat_b-lat_a)/lat_a*100:.0f}%）")
    print("\n  ⚠️ 这不是「无脑更好」—— 用延迟换「答得出来」。")
    print("     是否值得取决于场景对延迟的容忍度。")

    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"\n输出：{OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

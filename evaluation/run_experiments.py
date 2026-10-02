"""
M3 混合检索实验（D1–D4）。

在M2 的 v0_bm25 基线（recall@5=0.5272）之上，逐项验证优化收益：

  D1 切块策略    —— 复用 M1 语义切块 vs 固定长度切块（对照）
  D2 检索模式    —— 纯 BM25 / 纯向量 / 混合 RRF★核心
  D3 Rerank      —— 不重排 / Top-20 重排 / Top-50 重排
  D4 融合权重α   —— RRF 常数 k 扫描

设计要点：
  - 每个实验只改一个变量，其余固定（消融实验标准做法）
  - 每个实验产出对比表，最终汇总到 docs/M3实验报告.md
  - 检索与生成解耦：生成统一用 Oracle，只看检索层指标
    （与 M2 保持一致，否则生成幻觉会污染检索评估）

用法：
  python run_experiments.py --all
  python run_experiments.py --exp D2
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evaluation"))
sys.path.insert(0, str(ROOT / "retrieval"))

import numpy as np  # noqa: E402

from judge import RetrievalCase, eval_retrieval, load_eval_set  # noqa: E402
from lexical import BM25Index, RetrievalCorpus, rrf_fuse  # noqa: E402

OUT = ROOT / "dataset" / "processed" / "m3_experiments.json"
INDEX_DIR = ROOT / "index"
MODEL_DIR = ROOT / "models" / "BAAI_bge-m3"


# ==================================================================
# 检索器封装：统一接口，便于消融
# ==================================================================

@dataclass
class Retriever:
    name: str
    bm25: Optional[BM25Index] = None
    vec: Optional[object] = None
    embedder: Optional[object] = None
    fusion: str = "rrf"# rrf / none
    rrf_k: int = 60
    top_k: int = 20
    rerank: bool = False
    rerank_top: int = 50

    def search(self, query: str, top_k: int = 20,
               boost_stand: Optional[str] = None
               ) -> list[tuple[int, float]]:
        k = top_k or self.top_k
        rankings: list[list[tuple[int, float]]] = []

        if self.bm25 is not None:
            rankings.append(
                self.bm25.search(query, top_k=max(k * 2, 20), boost_stand=boost_stand))
        if self.vec is not None and self.embedder is not None:
            qv = self.embedder.encode([query])[0]
            vhits = self.vec.search(qv, top_k=max(k * 2, 20))
            if boost_stand:
                # 替身名精确匹配加权
                chunk2stand = getattr(self, "chunk2stand", {})
                for cid, sc in vhits:
                    if chunk2stand.get(cid) == boost_stand:
                        sc += 0.35
                vhits.sort(key=lambda x: -x[1])
            rankings.append(vhits)

        if not rankings:
            return []
        if len(rankings) == 1 or self.fusion == "none":
            return rankings[0][:k]
        return rrf_fuse(rankings, k=self.rrf_k, top_k=k)[:k]


# ==================================================================
# 评测主循环
# ==================================================================

def evaluate_retriever(items, corpus, retriever: Retriever) -> dict:
    """在评测集上跑一个检索器，返回检索层指标 + 延迟。"""
    cases: list[RetrievalCase] = []
    chunk2stand = {c["chunk_id"]: c["stand_id"] for c in corpus.chunks}
    retriever.chunk2stand = chunk2stand

    t0 = time.time()
    for it in items:
        q = it["question"]
        gold_chunks = list(it.get("evidence", {}).get("chunks") or [])
        gold_stands = list((it.get("evidence", {}).get("struct") or {}).keys())
        sid = corpus.guess_stand(q)
        ranked = retriever.search(q, top_k=20, boost_stand=sid)
        retrieved_ids = [cid for cid, _ in ranked]
        seen: list[str] = []
        for cid in retrieved_ids:
            st = chunk2stand.get(cid)
            if st and st not in seen:
                seen.append(st)
        cases.append(RetrievalCase(
            qid=it["qid"], gold_ids=gold_chunks, retrieved=retrieved_ids,
            gold_stands=gold_stands, retrieved_stands=seen,
        ))
    elapsed = time.time() - t0

    out = eval_retrieval(cases)
    out["elapsed_sec"] = round(elapsed, 2)
    out["qps"] = round(len(items) / elapsed, 1) if elapsed > 0 else 0
    out["avg_latency_ms"] = round(elapsed / len(items) * 1000, 1) if items else 0
    out["config"] = {
        "name": retriever.name,
        "has_bm25": retriever.bm25 is not None,
        "has_vector": retriever.vec is not None,
        "fusion": retriever.fusion if len([r for r in (retriever.bm25, retriever.vec) if r]) > 1 else "single",
        "rrf_k": retriever.rrf_k,
        "rerank": retriever.rerank,
    }
    return out


# ==================================================================
# 实验定义
# ==================================================================

def build_bm25(chunks) -> BM25Index:
    return BM25Index(docs=chunks, doc_ids=[c["chunk_id"] for c in chunks])


def build_dense(corpus, verbose=True):
    """构建稠密索引（失败返回 None）。"""
    from dense import build_index
    try:
        return build_index(corpus.chunks, backend="auto",
                           cache_name="m3_dense", verbose=verbose)
    except Exception as e:
        if verbose:
            print(f"  dense 索引构建失败：{type(e).__name__}: {e}")
        return None


def build_embedder():
    from dense import DenseEmbedder
    emb = DenseEmbedder(str(MODEL_DIR) if MODEL_DIR.exists() else "BAAI/bge-m3")
    if emb.model is None:
        print(f"  embedder 不可用：{emb._load_error}")
        return None
    print(f"  embedder: {emb.info()}")
    return emb


def exp_d2(items, corpus) -> list[dict]:
    """D2 检索模式：BM25 vs 向量 vs 混合 RRF。★ 核心实验"""
    print("\n" + "-" * 60)
    print("D2 检索模式对比")
    print("-" * 60)
    results = []

    bm25 = build_bm25(corpus.chunks)
    results.append(evaluate_retriever(
        items, corpus,
        Retriever(name="D2-1 BM25 only", bm25=bm25)))

    emb = build_embedder()
    if emb is None:
        print("  !! dense 不可用，D2 只产出 BM25 一组")
        return results
    vec = build_dense(corpus)
    if vec is None:
        return results

    results.append(evaluate_retriever(
        items, corpus,
        Retriever(name="D2-2 Vector only", vec=vec, embedder=emb)))

    results.append(evaluate_retriever(
        items, corpus,
        Retriever(name="D2-3 Hybrid RRF", bm25=bm25, vec=vec,
                   embedder=emb, fusion="rrf", rrf_k=60)))
    return results


def exp_d4(items, corpus) -> list[dict]:
    """D4：RRF 常数 k 扫描。"""
    print("\n" + "-" * 60)
    print("D4 RRF 常数 k 扫描")
    print("-" * 60)
    bm25 = build_bm25(corpus.chunks)
    emb = build_embedder()
    if emb is None:
        return []
    vec = build_dense(corpus)
    if vec is None:
        return []
    out = []
    for k in (10, 30, 60, 100):
        r = evaluate_retriever(
            items, corpus,
            Retriever(name=f"D4 RRF k={k}", bm25=bm25, vec=vec,
                       embedder=emb, fusion="rrf", rrf_k=k))
        out.append(r)
    return out


def exp_bm25_params(items, corpus) -> list[dict]:
    """BM25 超参扫描（k1 / b）—— 不依赖 embedding，环境受阻时的对照。"""
    print("\n" + "-" * 60)
    print("BM25 超参扫描（k1 / b）")
    print("-" * 60)
    out = []
    for k1 in (1.2, 1.5, 2.0):
        for b in (0.5, 0.75, 1.0):
            idx = BM25Index(docs=corpus.chunks,
                            doc_ids=[c["chunk_id"] for c in corpus.chunks],
                            k1=k1, b=b)
            r = evaluate_retriever(
                items, corpus,
                Retriever(name=f"BM25 k1={k1} b={b}", bm25=idx))
            out.append(r)
    return out


# ==================================================================
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--exp", default=None,
                    help="D2 / D4 / BM25PARAM")
    args = ap.parse_args()

    print("=" * 68)
    print("M3 混合检索实验")
    print("=" * 68)

    items = load_eval_set()
    corpus = RetrievalCorpus.load()
    corpus.index_stands()
    print(f"评测集 {len(items)} 条 | 语料 {len(corpus.chunks)} 块")

    experiments: dict[str, list] = {}
    plans = [
        ("D2", exp_d2),
        ("D4", exp_d4),
        ("BM25PARAM", exp_bm25_params),
    ]
    for name, fn in plans:
        if args.exp and args.exp != name:
            continue
        if not args.exp and not args.all:
            continue
        try:
            experiments[name] = fn(items, corpus)
        except Exception as e:
            print(f"  !! {name} 失败：{type(e).__name__}: {e}")

    if not experiments:
        print("\n未执行任何实验（用 --all 或 --exp指定）")
        return 1

    # ---------- 汇总表 ----------
    print("\n" + "=" * 68)
    print("实验结果汇总（检索层）")
    print("=" * 68)
    hdr = (f"{'实验':30s} {'recall@1':>9s} {'recall@3':>9s} {'recall@5':>9s} "
           f"{'mrr':>7s} {'ndcg@5':>8s} {'ms':>7s}")
    print(hdr)
    print("-" * len(hdr))
    for exp, results in experiments.items():
        for r in results:
            print(f"{r['config']['name']:30s} "
                  f"{r.get('recall@1', 0):>9.4f} {r.get('recall@3', 0):>9.4f} "
                  f"{r.get('recall@5', 0):>9.4f} {r.get('mrr', 0):>7.4f} "
                  f"{r.get('ndcg@5', 0):>8.4f} {r.get('avg_latency_ms', 0):>7.1f}")

    OUT.write_text(json.dumps(experiments, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"\n输出：{OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

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
from dataclasses import dataclass, field
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
    rerank_candidates: int = 50
    reranker: Optional[object] = None
    chunk_map: Optional[dict] = field(default=None, repr=False)

    def search(self, query: str, top_k: int = 20,
               boost_stand: Optional[str] = None
               ) -> list[tuple[int, float]]:
        k = top_k or self.top_k
        rankings: list[list[tuple[int, float]]] = []

        # ★ 召回深度：重排需要足够大的候选池，否则「重排」无从下手。
        #   正确做法是召回 max(重排候选数, 2k)，再截到 k返回。
        need = max(k * 2, self.rerank_candidates if self.rerank else 0, 20)

        if self.bm25 is not None:
            rankings.append(
                self.bm25.search(query, top_k=need, boost_stand=boost_stand))
        if self.vec is not None and self.embedder is not None:
            qv = self.embedder.encode([query])[0]
            vhits = self.vec.search(qv, top_k=need)
            if boost_stand and self.chunk2stand:
                # 替身名精确匹配加权
                boosted = []
                for cid, sc in vhits:
                    if self.chunk2stand.get(cid) == boost_stand:
                        sc += 0.35
                    boosted.append((cid, sc))
                boosted.sort(key=lambda x: -x[1])
                vhits = boosted
            rankings.append(vhits)

        if not rankings:
            return []
        if len(rankings) == 1 or self.fusion == "none":
            fused = rankings[0][:need]
        else:
            fused = rrf_fuse(rankings, k=self.rrf_k, top_k=need)

        # ---------- 重排阶段 ----------
        if self.rerank and self.reranker is not None and self.reranker.model:
            pool = fused[:self.rerank_candidates]
            if len(pool) > 1:
                docs = [self.chunk_map.get(cid, {}).get("content", "")
                        for cid, _ in pool]
                pairs = [(cid, d) for (cid, _), d in zip(pool, docs)]
                fused = self.reranker.rerank(query, pairs, top_k=k)
            else:
                fused = fused[:k]
        return fused[:k]

    chunk2stand: dict = field(default_factory=dict, repr=False)


# ==================================================================
# 评测主循环
# ==================================================================

def evaluate_retriever(items, corpus, retriever: Retriever,
                       progress: bool = True) -> dict:
    """在评测集上跑一个检索器，返回检索层指标 + 延迟。

    ★ 预热说明：rerank 首次推理含 CUDA kernel 编译（10–30 秒），
      必须先warmup 再计时，否则平均延迟会被严重污染。
    """
    cases: list[RetrievalCase] = []
    chunk2stand = {c["chunk_id"]: c["stand_id"] for c in corpus.chunks}
    chunk_map = {c["chunk_id"]: c for c in corpus.chunks}
    retriever.chunk2stand = chunk2stand
    retriever.chunk_map = chunk_map

    # 预热
    if retriever.reranker is not None:
        retriever.reranker.warmup()
    if retriever.embedder is not None:
        retriever.search("warmup query", top_k=20)
    retriever.search("warmup query", top_k=20)

    t0 = time.time()
    for n, it in enumerate(items, 1):
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
        # 进度：每20 条输出一次 + 预估剩余
        if progress and (n % 20 == 0 or n == len(items)):
            el = time.time() - t0
            eta = el / n * (len(items) - n)
            print(f"      {n}/{len(items)}  {el:.0f}s 已用,"
                  f" 预计剩余 {eta:.0f}s", flush=True)
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
        "rerank_candidates": retriever.rerank_candidates if retriever.rerank else None,
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
    """构建 embedder（用本地已下载模型，避免 HF 网络请求）。"""
    from dense import DenseEmbedder
    emb = DenseEmbedder()          # 内部会优先解析 models/ 下的本地目录
    if emb.model is None:
        print(f"  embedder 不可用：{emb._load_error}")
        return None
    print(f"  embedder: {emb.backend} | {emb.device} | dim={emb.dim}")
    return emb


def exp_d1(items, corpus) -> list[dict]:
    """D1 切块策略：语义切块 vs 固定长度切块。

    ★ 难点：评测集的 gold chunk_id 按语义切块生成，换切块后 gold 失效。
      解法：用 chunking.ViewCorpus 的溯源映射，把 gold 语义块 id
      翻译成「包含它的 view_id 集合」，命中任一即算召回。
    """
    print("\n" + "-" * 60)
    print("D1 切块策略对照（语义 vs 固定长度）")
    print("-" * 60)
    from chunking import build_views, ViewCorpus
    from lexical import BM25Index as _BM25

    results = []
    for strategy in ("semantic", "fixed"):
        views = build_views(strategy, corpus.chunks)
        vc = ViewCorpus(views)
        # doc_ids 用 view_id 而非整数，让 BM25Index 直接返回可溯源的 id
        bm = _BM25(docs=vc.docs, doc_ids=[v.view_id for v in views])
        r = _eval_with_view(items, vc, bm)
        r["config"]["name"] = f"D1-{strategy} ({len(views)} 块)"
        r["config"]["n_views"] = len(views)
        results.append(r)
    return results


def _eval_with_view(items, vc, bm25_index) -> dict:
    """在切块视图上评测。

    ★★ 关键：跨切块策略的公平比较（M3 实测踩坑后修正）

    原始做法有个陷阱：
      gold 是「语义块 id」。fixed 切块会把 1 个语义块拆进约 2 个 view，
      若按「命中任一 view 即算召回」判分，等于**给 fixed 放宽了标准**
      （gold 从 1 个候选变成 1.96 个候选）→ 得出「fixed 更好 3 倍」的错误结论。

    正确做法：用**溯源覆盖率 src_coverage@k** 作为唯一可比指标：

        对每个 gold 语义块 g：
          coverage(g) = |检索命中的 view 中属于 g 的字符数| / |g 的字符数|

      语义切块下 g 只对应 1 个 view（覆盖 0 或 1）；
      固定切块下 g 对应多个 view（可部分覆盖）。
      两者量纲一致，可直接比较。
    """
    # 精确构造：语义块 -> {view_id: 该view内属于此块的字符数}
    owner_span: dict[int, dict[str, int]] = {}
    for v in vc.views:
        per_src: dict[int, int] = {}
        for sid in v.src_chunk_ids:
            per_src[sid] = per_src.get(sid, 0) + len(v.content) // max(len(v.src_chunk_ids), 1)
        for sid, span in per_src.items():
            owner_span.setdefault(sid, {})[v.view_id] = span
    src_total = {sid: sum(spans.values()) or 1 for sid, spans in owner_span.items()}

    cases = []
    coverages: list[float] = []
    full_rates: list[float] = []
    for it in items:
        gold_chunks = list(it.get("evidence", {}).get("chunks") or [])
        if not gold_chunks:
            continue
        gold_stands = list((it.get("evidence", {}).get("struct") or {}).keys())

        ranked = bm25_index.search(it["question"], top_k=20)
        retrieved = [vid for vid, _ in ranked]
        retrieved_set = set(retrieved)

        covs = []
        for sid in gold_chunks:
            spans = owner_span.get(sid, {})
            if not spans:
                covs.append(0.0)
                continue
            hit = sum(sp for vid, sp in spans.items() if vid in retrieved_set)
            covs.append(min(hit / src_total[sid], 1.0))
        avg_cov = sum(covs) / len(covs) if covs else 0.0
        coverages.append(avg_cov)
        full_rates.append(1.0 if avg_cov >= 0.999 else 0.0)

        # gold 视角：覆盖率达标才算召回（与 recall@K 语义对齐）
        gold_hit = [f"g{sid}" for sid, c in zip(gold_chunks, covs) if c >= 0.999]
        all_gold = [f"g{sid}" for sid in gold_chunks]

        view2stand = {v.view_id: v.stand_id for v in vc.views}
        retrieved_stands: list[str] = []
        for vid in retrieved:
            st = view2stand.get(vid)
            if st and st not in retrieved_stands:
                retrieved_stands.append(st)

        cases.append(RetrievalCase(
            qid=it["qid"], gold_ids=all_gold, retrieved=gold_hit,
            gold_stands=gold_stands, retrieved_stands=retrieved_stands,
        ))

    out = eval_retrieval(cases)
    out["src_coverage@5"] = (round(sum(coverages) / len(coverages), 4)
                             if coverages else float("nan"))
    out["full_coverage_rate"] = (round(sum(full_rates) / len(full_rates), 4)
                                 if full_rates else 0.0)
    out["elapsed_sec"] = 0.0
    out["qps"] = 0.0
    out["avg_latency_ms"] = 0.0
    return {"n_cases": out.get("n_cases", 0), "config": {}, **out}


def exp_d3(items, corpus) -> list[dict]:
    """D3 重排：候选深度扫描。★ 预期质量提升最大的实验。

    ★ 注意性能：交叉编码器约 49ms/条（GPU，batch=16）。
      176 条 × 20 候选 ≈ 172 秒/组，故只跑关键配置而非全组合。
    """
    print("\n" + "-" * 60)
    print("D3 交叉编码器重排")
    print("-" * 60)

    # 基线：M3-c 的最优组合（无重排）
    bm25 = BM25Index(docs=corpus.chunks,
                      doc_ids=[c["chunk_id"] for c in corpus.chunks],
                      k1=1.2, b=0.5)
    emb = build_embedder()
    if emb is None:
        return []
    vec = build_dense(corpus)
    if vec is None:
        return []

    results = []
    print("  [1/3] 基线（无重排）")
    results.append(evaluate_retriever(
        items, corpus,
        Retriever(name="D3-0 无重排（基线）", bm25=bm25, vec=vec,
                   embedder=emb, fusion="rrf", rrf_k=30)))

    rr = build_reranker()
    if rr is None:
        print("  !! reranker 不可用，D3 只产出基线")
        return results
    rr.warmup()

    # 候选深度 20/ 50 对照
    for cand in (20, 50):
        print(f"  [重排 Top-{cand}]")
        results.append(evaluate_retriever(
            items, corpus,
            Retriever(name=f"D3-{cand} 重排 Top-{cand}", bm25=bm25, vec=vec,
                       embedder=emb, fusion="rrf", rrf_k=30,
                       rerank=True, rerank_candidates=cand, reranker=rr)))

    # 消融：仅 BM25 + 重排（去掉向量路）
    print("  [消融] 仅 BM25 + 重排")
    results.append(evaluate_retriever(
        items, corpus,
        Retriever(name="D3-50 仅BM25+重排（消融向量路）", bm25=bm25,
                   rerank=True, rerank_candidates=50, reranker=rr)))
    return results


def build_reranker():
    from rerank import CrossEncoderReranker
    rr = CrossEncoderReranker()
    if rr.model is None:
        print(f"  reranker 不可用：{rr._error}")
        return None
    print(f"  reranker: {rr.model_path} | {rr.device} | max_len={rr.max_len}")
    return rr


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
        ("D1", exp_d1),
        ("D2", exp_d2),
        ("D3", exp_d3),
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

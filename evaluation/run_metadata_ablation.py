"""元数据消融实验：把「规模不是自变量」从论点变成证据。

== 要回答的问题 ==
用户最初问「数据库感觉有点少啊 真的适合做成 rag 吗」。
当时的回答是：**问题不在规模，在"语料的规整程度"** ——
本项目的语料是从 jojowiki 结构化抽取的，每块都带
`stand_name` / `section` 元数据，BM25 靠关键词就能定位，
所以向量检索使不上劲。

但这在当时只是**论点**。本实验把它变成**可复核的证据**。

== 实验设计 ==
对同一批 46 条有 chunk-gold 的评测问题（T4 semantic + T5 hybrid），
比较两个条件下 `hybrid 相对 BM25` 的增益：

  条件 A（完整）  文档文本 = content + stand_name×2 + section
  条件 B（消融）  文档文本 = content            ← 抹掉元数据

★ `stand_name×2` 是 BM25 里的替身名**加权**（见 lexical.py:101-104），
  消融后这部分优势消失。

== 预期（可被证伪的假设）==
  · 条件 B 下 **BM25 绝对性能明显下降**（定位不到替身）
  · 条件 B 下 **hybrid 的相对增益上升**（向量顶上了元数据的缺）
  → 若成立，说明「向量的价值 = f(语料的非结构化程度)」，
    而**不是** f(语料规模) —— 与本项目语料小但规整的事实不矛盾。

== 立场 ==
结果**可能是证伪**。若消融后增益没有上升，就如实报告
—— 本项目的既有做法（M3/M4 的负面结论也照实写）是保留负面结果。

用法：
    python evaluation/run_metadata_ablation.py              # 含向量（慢）
    python evaluation/run_metadata_ablation.py --no-vector  # 只看 BM25
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROC = ROOT / "dataset" / "processed"
OUT = PROC / "metadata_ablation.json"
TMP = PROC / "_ablation_no_meta.json"

sys.path.insert(0, str(ROOT / "retrieval"))
from lexical import RetrievalCorpus, rrf_fuse      # noqa: E402

# 被消融的元数据字段
ABLATE_FIELDS = ("stand_name", "section", "entity", "alias",
                 "phonetic", "debut")


def load_base_chunks() -> list[dict]:
    """★ 与服务的检索语料保持一致（M43 起是「原语料 + 文章块」）。"""
    base = json.loads((PROC / "text_chunks.json").read_text(encoding="utf-8"))
    art = PROC / "article_chunks.json"
    if art.exists():
        base = base + json.loads(art.read_text(encoding="utf-8"))
    return base


def load_gold() -> list[dict]:
    """取有 chunk-gold 的评测问题。"""
    ev = json.loads((PROC / "eval_set.json").read_text(encoding="utf-8"))
    out = []
    for x in ev:
        ch = (x.get("evidence") or {}).get("chunks")
        if not ch:
            continue
        ids = set()
        for c in ch:
            cid = c if isinstance(c, int) else c.get("chunk_id")
            if isinstance(cid, int):
                ids.add(cid)
        if ids:
            out.append({"q": x["question"], "gold": ids,
                        "type": x["question_type"]})
    return out


def recall_at(ranked: list, gold: set, k: int) -> float:
    top = {d for d, _ in ranked[:k]}
    return 1.0 if (top & gold) else 0.0


def write_ablated(chunks: list[dict]) -> int:
    """把抹掉元数据后的语料写到临时文件，返回写入块数。"""
    out = []
    for c in chunks:
        c2 = {k: v for k, v in c.items() if k not in ABLATE_FIELDS}
        # 用空串占位而不是删键 —— 保持 schema 形状，避免下游 KeyError
        for k in ABLATE_FIELDS:
            c2[k] = ""
        out.append(c2)
    TMP.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return len(out)


def run_condition(tag: str, chunks_file: str | None,
                  embedder=None, vec_cache: dict | None = None
                  ) -> dict:
    """跑一个条件：BM25 vs hybrid 的 recall@{1,5,10}。"""
    golds = load_gold()
    t0 = time.time()
    if chunks_file:
        corpus = RetrievalCorpus.load(chunks_file)
    else:
        # 完整条件：直接构造（合并语料不在单个文件里）
        chunks = load_base_chunks()
        corpus = RetrievalCorpus(
            chunks=chunks,
            stands=json.loads((PROC / "stands.json").read_text(encoding="utf-8")),
            stats=json.loads(
                (PROC / "stand_stats.json").read_text(encoding="utf-8")),
            bm25=None)
        from lexical import BM25Index
        corpus.bm25 = BM25Index(docs=chunks,
                                doc_ids=[c["chunk_id"] for c in chunks])
    # ★ 必须显式调用：guess_stand() 依赖 name2id
    corpus.index_stands()

    res = {"bm25": {1: 0.0, 5: 0.0, 10: 0.0},
           "hybrid": {1: 0.0, 5: 0.0, 10: 0.0},
           "n": len(golds), "n_chunks": len(corpus.chunks)}

    # 向量索引（按条件缓存，避免重复编码）
    vec = None
    if embedder is not None:
        from dense import build_index
        cache_key = f"abl_{tag}"
        if vec_cache is not None and cache_key in vec_cache:
            vec = vec_cache[cache_key]
        else:
            # ★ 条件 A（完整语料）与服务用的是同一批语料 →
            #   复用服务已建好的缓存（与 semantic_executor.py 的 key 规则一致），
            #   省一次 6761 块的编码。
            cache_name = (f"m3_bge_m3_n{len(corpus.chunks)}"
                          if tag == "full" else f"abl_{tag}")
            vec = build_index(corpus.chunks, backend="dense",
                              cache_name=cache_name, verbose=True)
            if vec_cache is not None:
                vec_cache[cache_key] = vec

    for g in golds:
        q = g["q"]
        sid = corpus.guess_stand(q)
        r1 = corpus.bm25.search(q, top_k=20, boost_stand=sid)
        for k in (1, 5, 10):
            res["bm25"][k] += recall_at(r1, g["gold"], k)
        if vec is not None:
            qv = embedder.encode([q])[0]
            r2 = vec.search(qv, top_k=20)
            fused = rrf_fuse([r1, r2], k=30, top_k=20)
            for k in (1, 5, 10):
                res["hybrid"][k] += recall_at(fused, g["gold"], k)

    n = max(len(golds), 1)
    for k in (1, 5, 10):
        res["bm25"][k] /= n
        res["hybrid"][k] /= n
    res["elapsed_s"] = round(time.time() - t0, 1)
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-vector", action="store_true",
                    help="跳过向量（只看 BM25 消融）")
    args = ap.parse_args()

    base = load_base_chunks()
    n_art = sum(1 for c in base
                if c.get("chunk_type") == "article_section")
    print("=" * 72)
    print("元数据消融实验：向量价值 = f(非结构化程度) 还是 f(规模)？")
    print("=" * 72)
    print(f"语料 {len(base)} 块（其中文章块 {n_art}）｜"
          f"消融字段 {ABLATE_FIELDS}")

    n = write_ablated(base)
    print(f"已生成消融语料（{n} 块 → 临时文件 {TMP.name}）\n")

    embedder = None
    if not args.no_vector:
        from dense import DenseEmbedder
        embedder = DenseEmbedder()
        if embedder.model is None:
            print("⚠ 向量模型不可用，退回只跑 BM25")
            embedder = None

    vec_cache: dict = {}
    print("--- 条件 A：完整语料（content + stand_name×2 + section）---")
    A = run_condition("full", None, embedder, vec_cache)
    print(f"    BM25 {A['bm25']}")
    if embedder is not None:
        print(f"    hybrid {A['hybrid']}")

    print("\n--- 条件 B：消融（只留 content）---")
    B = run_condition("nometa", TMP.name, embedder, vec_cache)
    print(f"    BM25 {B['bm25']}")
    if embedder is not None:
        print(f"    hybrid {B['hybrid']}")

    # ---- 结论 ----
    print("\n" + "=" * 72)
    print("结论")
    print("=" * 72)
    print(f"{'recall@k':<12}{'A:BM25':>10}{'B:BM25':>10}{'Δ':>9}")
    for k in (1, 5, 10):
        d = B["bm25"][k] - A["bm25"][k]
        print(f"  @{k:<9}{A['bm25'][k]:>10.4f}{B['bm25'][k]:>10.4f}"
              f"{d:>+9.4f}")

    result = {"condition_A_full": A, "condition_B_ablated": B,
              "ablate_fields": list(ABLATE_FIELDS),
              "n_chunks": len(base),
              "_note": "A = content+stand_name*2+section；B = 仅 content"}

    if embedder is not None:
        print(f"\n{'hybrid 相对 BM25 的增益 (pp)':<28}{'@1':>8}{'@5':>8}{'@10':>8}")
        for tag, r in (("A 完整", A), ("B 消融", B)):
            ds = [(r["hybrid"][k] - r["bm25"][k]) * 100
                  for k in (1, 5, 10)]
            print(f"  {tag:<26}" + "".join(f"{x:>+8.2f}" for x in ds))
        gA = (A["hybrid"][5] - A["bm25"][5]) * 100
        gB = (B["hybrid"][5] - B["hybrid"].get(5, 0) * 0) * 0  # placeholder
        gB = (B["hybrid"][5] - B["bm25"][5]) * 100
        result["gain_pp"] = {"A_full": gA, "B_ablated": gB}
        print(f"\n  → 消融后 hybrid 增益 @5：{gA:+.2f}pp → {gB:+.2f}pp")
        if gB > gA + 1:
            print("  ✓ 假设成立：元数据一没，向量顶上来了")
            print("    → 向量价值取决于**语料的非结构化程度**，与规模无关")
        elif abs(gB - gA) <= 1:
            print("  – 无明显变化：元数据不是向量的主要价值来源")
        else:
            print("  ✗ 假设被证伪：消融后增益反而下降")
            print("    → 如实记录，不修饰")

    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    TMP.unlink(missing_ok=True)
    print(f"\n结果 → {OUT.relative_to(ROOT)}（临时语料已删除）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""验证核心假设：向量检索的价值是否取决于语料的结构化程度。

== 假设 ==
现有 2407 块是从 `Stand_Stats` 主表抽的**结构化短块**
（中位 229 字符，stand_name/section 元数据 100% 覆盖），
实测向量几乎没帮上忙：recall@1 与纯 BM25 相同、recall@10 甚至更低。

**假设**：向量检索的增益 = f(语料的非结构化程度)，而非 f(规模)。
若成立，则在引入 jojowiki 文章（角色页/替身页正文，非结构化长文本）后，
hybrid 相对纯 BM25 的增益应当**上升**。

== 测法 ==
同一批 46 条有 chunk-gold 的评测问题（T4 semantic + T5 hybrid），
在两种语料上各跑「纯 BM25」与「BM25+向量 RRF」，比 recall@k。
    baseline : text_chunks.json      （2407 结构化短块）
    augmented: text_chunks_aug.json  （+ 文章块，非结构化长文本）

★ 两处必须踩稳：
  1. **向量缓存 key 必须含语料标识** —— 否则两种语料会读到同一份缓存
     （项目此前踩过：缓存按 chunk_id 列表算，合并参数不同却命中同一缓存）。
  2. gold chunk_id 在原语料里，aug 只是**追加**新块，原 id 不变 → 可直接复用。

用法：
    python evaluation/run_aug_retrieval_eval.py
    python evaluation/run_aug_retrieval_eval.py --no-vector   # 只测 BM25（快）
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROC = ROOT / "dataset" / "processed"
sys.path.insert(0, str(ROOT / "retrieval"))

from lexical import RetrievalCorpus, rrf_fuse      # noqa: E402

CORPORA = [
    ("baseline", "text_chunks.json", "结构化短块 2407"),
    ("augmented", "text_chunks_aug.json", "+ 文章非结构化长文本"),
]


def load_gold() -> list[dict]:
    """取有 chunk-gold 的评测问题（T4/T5）。"""
    ev = json.loads((PROC / "eval_set.json").read_text(encoding="utf-8"))
    out = []
    for x in ev:
        ch = (x.get("evidence") or {}).get("chunks")
        if ch:
            out.append({"qid": x["qid"], "question": x["question"],
                        "gold": set(ch), "type": x["question_type"]})
    return out


def recall_at(ranked: list, gold: set, k: int) -> float:
    """ranked: [(doc_id, score)]；gold 命中即算召回（set 型 gold）。"""
    top = {d for d, _ in ranked[:k]}
    return 1.0 if (top & gold) else 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-vector", action="store_true",
                    help="跳过向量（只测 BM25，秒出）")
    ap.add_argument("--k", type=int, default=30, help="RRF 常数")
    args = ap.parse_args()

    golds = load_gold()
    print(f"评测问题 {len(golds)} 条（有 chunk-gold）\n")

    summary: dict[str, dict] = {}
    for tag, fname, desc in CORPORA:
        p = PROC / fname
        if not p.exists():
            print(f"跳过 {tag}：缺 {fname}")
            continue
        t0 = time.time()
        corpus = RetrievalCorpus.load(fname)
        # ★ 必须显式调用：guess_stand() 依赖 name2id，
        #   而 name2id 是 index_stands() 建的，load() 不会自动调
        #   （漏了会报 AttributeError: 'RetrievalCorpus' object has no attribute 'name2id'）
        corpus.index_stands()
        chunks = corpus.chunks
        n_chars = sum(len(c["content"]) for c in chunks)
        print(f"=== {tag} ({desc}) ===")
        print(f"  块 {len(chunks)} / {n_chars:,} 字符 "
              f"（均 {n_chars/len(chunks):.0f} 字）")

        bm25 = corpus.bm25
        vec = None
        embedder = None
        if not args.no_vector:
            try:
                from dense import build_index, DenseEmbedder
                embedder = DenseEmbedder()
                if embedder.model is None:
                    print(f"  ! 向量不可用：{embedder._load_error}")
                else:
                    # ★ 缓存 key 必须含语料标识，否则两种语料共用同一份缓存
                    vec = build_index(chunks, backend="dense",
                                      cache_name=f"aug_eval_{tag}")
            except Exception as e:                      # noqa: BLE001
                print(f"  ! 建向量索引失败：{type(e).__name__}: {e}")

        res = {"bm25": {1: 0.0, 3: 0.0, 5: 0.0, 10: 0.0},
               "hybrid": {1: 0.0, 3: 0.0, 5: 0.0, 10: 0.0},
               "n": len(golds), "n_chunks": len(chunks),
               "n_chars": n_chars}
        for g in golds:
            q = g["question"]
            # ★★ 必须对齐 m3 的口径：先识别替身名，再带 boost 检索 ★★
            #   实测踩坑：漏掉 boost_stand 时 BM25 recall@5 只有 0.3478，
            #   而 m3 记录的是 0.5707 —— 差了一大截，会把 hybrid 增益
            #   虚高到 +19.6pp（真实值约 +5.4pp）。
            #   本项目大多数问题都含替身名，规则层的实体识别是 BM25 的
            #   主力贡献，漏掉它等于把基线做残。
            sid = corpus.guess_stand(q)
            r1 = bm25.search(q, top_k=20, boost_stand=sid)
            for k in (1, 3, 5, 10):
                res["bm25"][k] += recall_at(r1, g["gold"], k)
            if vec is not None:
                qv = embedder.encode([q])[0]
                r2 = vec.search(qv, top_k=20)
                fused = rrf_fuse([r1, r2], k=args.k, top_k=20)
                for k in (1, 3, 5, 10):
                    res["hybrid"][k] += recall_at(fused, g["gold"], k)
        for k in (1, 3, 5, 10):
            res["bm25"][k] /= len(golds)
            res["hybrid"][k] /= len(golds)
        summary[tag] = res

        print(f"  recall@1  BM25 {res['bm25'][1]:.4f}"
              + (f"   hybrid {res['hybrid'][1]:.4f}" if vec is not None else ""))
        for k in (3, 5, 10):
            line = f"  recall@{k:<2} BM25 {res['bm25'][k]:.4f}"
            if vec is not None:
                d = res["hybrid"][k] - res["bm25"][k]
                line += f"   hybrid {res['hybrid'][k]:.4f}  ({d:+.4f})"
            print(line)
        print(f"  用时 {time.time()-t0:.0f}s\n")

    # ---------- 核心对比：hybrid 相对 BM25 的增益 ----------
    if len(summary) == 2 and not args.no_vector:
        print("=" * 62)
        print("★ 核心结论：hybrid 相对纯 BM25 的增益（百分点）")
        print("=" * 62)
        print(f"  {'语料':<12}{'recall@1':>12}{'recall@3':>12}"
              f"{'recall@5':>12}{'recall@10':>12}")
        deltas = {}
        for tag in ("baseline", "augmented"):
            if tag not in summary:
                continue
            s = summary[tag]
            ds = {k: (s["hybrid"][k] - s["bm25"][k]) * 100
                  for k in (1, 3, 5, 10)}
            deltas[tag] = ds
            print(f"  {tag:<12}" + "".join(f"{ds[k]:>+11.2f} " for k in
                                           (1, 3, 5, 10)))
        if "baseline" in deltas and "augmented" in deltas:
            print()
            for k in (5, 10):
                diff = deltas["augmented"][k] - deltas["baseline"][k]
                verdict = ("↑ 向量在非结构化语料上更有用 —— 假设成立"
                           if diff > 1 else
                           "→ 无显著变化 —— 假设不成立"
                           if diff > -1 else
                           "↓ 反而更差 —— 假设被证伪")
                print(f"  recall@{k}: 增益变化 {diff:+.2f}pp  {verdict}")
        out = PROC / "aug_retrieval_eval.json"
        out.write_text(json.dumps({"summary": summary, "deltas_pp": deltas},
                                  ensure_ascii=False, indent=1),
                       encoding="utf-8")
        print(f"\n→ {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

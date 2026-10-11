"""答案级判分 + 自动扩展 gold —— 给「向量增益」一个公平结论。

== 为什么需要这一步 ==
`run_aug_retrieval_eval.py` 用的是**块级 gold**（只标了原语料的 chunk_id）。
引入 3262 个新块后，即使新块包含正确答案也不计入命中 —— 实测 46 题中
有 **23 题（50%）**的答案片段能在新块里找到，却被判为 miss。
→ 那个「向量增益未上升」的结论**系统性低估了 hybrid**，不可靠。

== 两个互补的纠正 ==
1. **答案级判分**：不看 chunk_id，看 top-k 块的**文本**是否包含答案片段。
   这直接回答用户关心的问题——「能不能找到答案」。
2. **自动扩展 gold**：扫描全部块（原+新），凡含答案片段的都并入 gold。
   ★ 这一步替代了「人工重新标注 46 题」——用可复现的文本匹配，
     而不是人肉标注（也无法保证标注一致性）。
     原 gold 始终保留（不丢失事实标准），只是**增补**。

== 判据设计（避免自欺）==
- probe = 归一化后的答案前 N 个词。N 太短会误匹配，太长会因分块切断而漏。
  → 取 3 档（8/12/16 词）同时报告，**结论需在多档下一致**才可信。
- 归一化：小写 + 非字母数字转空格 + 压缩空格。
  ★ 与语料清洗后的形态对齐（标点已大量去除）。

用法：
    python evaluation/run_answer_level_eval.py
    python evaluation/run_answer_level_eval.py --no-vector   # 只测 BM25（秒出）
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROC = ROOT / "dataset" / "processed"
sys.path.insert(0, str(ROOT / "retrieval"))

from lexical import RetrievalCorpus, rrf_fuse      # noqa: E402

CORPORA = [
    ("baseline", "text_chunks.json", "结构化短块"),
    ("augmented", "text_chunks_aug.json", "+ 文章非结构化长文本"),
]
PROBE_LENS = (8, 12, 16)


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s.lower())).strip()


def make_probe(answer: str, n: int) -> str:
    return " ".join(norm(answer).split()[:n])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-vector", action="store_true")
    ap.add_argument("--k", type=int, default=30)
    args = ap.parse_args()

    ev = json.loads((PROC / "eval_set.json").read_text(encoding="utf-8"))
    items = []
    for x in ev:
        ch = (x.get("evidence") or {}).get("chunks")
        if ch and str(x.get("answer") or ""):
            items.append({"qid": x["qid"], "question": x["question"],
                          "answer": str(x["answer"]), "gold0": set(ch)})
    print(f"评测问题 {len(items)} 条（有 chunk-gold）\n")

    summary: dict[str, dict] = {}
    for tag, fname, desc in CORPORA:
        p = PROC / fname
        if not p.exists():
            print(f"跳过 {tag}：缺 {fname}")
            continue
        t0 = time.time()
        corpus = RetrievalCorpus.load(fname)
        corpus.index_stands()          # ★ 不调则 guess_stand 报 AttributeError
        chunks = corpus.chunks
        bm25 = corpus.bm25
        # ★ 自己建映射，不依赖内部私有属性（_chunk_map 是 optional 字段，
        #   RetrievalCorpus 未必填充）
        ctext = {c["chunk_id"]: norm(c["content"]) for c in chunks}

        # ---- 预计算：每题在各 probe 长度下的「含答案块集合」 ----
        #   gold_ext = 原 gold ∪ 含答案片段的块（自动补标注）
        per_probe: dict[int, dict] = {}
        for n in PROBE_LENS:
            gold_ext, answer_containers = {}, {}
            for it in items:
                probe = make_probe(it["answer"], n)
                hit = {c["chunk_id"] for c in chunks
                       if probe and probe in norm(c["content"])}
                answer_containers[it["qid"]] = hit
                gold_ext[it["qid"]] = it["gold0"] | hit
            per_probe[n] = {"gold_ext": gold_ext,
                            "containers": answer_containers}

        vec = embedder = None
        if not args.no_vector:
            try:
                from dense import build_index, DenseEmbedder
                embedder = DenseEmbedder()
                if embedder.model is not None:
                    vec = build_index(chunks, backend="dense",
                                      cache_name=f"aug_eval_{tag}")
                else:
                    print(f"  ! 向量不可用：{embedder._load_error}")
            except Exception as e:                      # noqa: BLE001
                print(f"  ! 建向量索引失败：{type(e).__name__}: {e}")

        print(f"=== {tag}（{desc}，{len(chunks)} 块）===")
        res = {"n": len(items), "n_chunks": len(chunks)}
        for n in PROBE_LENS:
            ge = per_probe[n]["gold_ext"]
            n_ext = sum(len(ge[i["qid"]] - i["gold0"]) for i in items)
            res[n] = {"auto_added": n_ext,
                      "bm25_ans": {k: 0.0 for k in (1, 5, 10)},
                      "hyb_ans": {k: 0.0 for k in (1, 5, 10)},
                      "bm25_gold": {k: 0.0 for k in (1, 5, 10)},
                      "hyb_gold": {k: 0.0 for k in (1, 5, 10)}}
            for it in items:
                q = it["question"]
                sid = corpus.guess_stand(q)
                r1 = bm25.search(q, top_k=20, boost_stand=sid)
                rankings = [r1]
                if vec is not None:
                    qv = embedder.encode([q])[0]
                    rankings.append(vec.search(qv, top_k=20))
                fused = (rrf_fuse(rankings, k=args.k, top_k=20)
                         if len(rankings) > 1 else r1)
                probes = make_probe(it["answer"], n)
                for k in (1, 5, 10):
                    for ids, tag2 in ((r1, "bm25"), (fused, "hyb")):
                        top = ids[:k]
                        txt = " ".join(ctext.get(c, "") for c, _ in top)
                        # 答案级：top-k 文本里能否找到答案片段
                        if probes and probes in txt:
                            res[n][f"{tag2}_ans"][k] += 1
                        # 块级（扩展 gold）
                        if set(c for c, _ in top) & ge[it["qid"]]:
                            res[n][f"{tag2}_gold"][k] += 1
            for key in ("bm25_ans", "hyb_ans", "bm25_gold", "hyb_gold"):
                for k in res[n][key]:
                    res[n][key][k] /= len(items)
            print(f"  -- 答案片段取前 {n} 词（自动补入 gold {n_ext} 块）--")
            print(f"     答案级 recall@5 : BM25 {res[n]['bm25_ans'][5]:.4f}"
                  + (f"  hybrid {res[n]['hyb_ans'][5]:.4f}"
                     f"  ({res[n]['hyb_ans'][5]-res[n]['bm25_ans'][5]:+.4f})"
                     if vec is not None else ""))
            print(f"     块级   recall@5 : BM25 {res[n]['bm25_gold'][5]:.4f}"
                  + (f"  hybrid {res[n]['hyb_gold'][5]:.4f}"
                     f"  ({res[n]['hyb_gold'][5]-res[n]['bm25_gold'][5]:+.4f})"
                     if vec is not None else ""))
        res["elapsed"] = round(time.time() - t0, 1)
        summary[tag] = res
        print()

    # ---------- 核心对比 ----------
    if len(summary) == 2 and not args.no_vector:
        print("=" * 66)
        print("★ hybrid 相对纯 BM25 的增益（个百分点）")
        print("=" * 66)
        for kind, label in (("gold", "块级（扩展 gold）"), ("ans", "答案级")):
            print(f"\n  [{label}]")
            print(f"  {'语料':<11}{'probe':>7}{'@1':>10}{'@5':>10}{'@10':>10}")
            for tag in ("baseline", "augmented"):
                if tag not in summary:
                    continue
                for n in PROBE_LENS:
                    r = summary[tag][n]
                    ds = [ (r[f"hyb_{kind}"][k] - r[f"bm25_{kind}"][k]) * 100
                           for k in (1, 5, 10)]
                    print(f"  {tag:<11}{n:>7}" +
                          "".join(f"{d:>+9.2f} " for d in ds))
        out = PROC / "answer_level_eval.json"
        out.write_text(json.dumps(summary, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        print(f"\n→ {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

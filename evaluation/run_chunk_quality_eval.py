"""
块大小 × 生成质量联合实验（M7）。

★★ 解决 M3 遗留的两个问题：

  问题 1（D1b 指标退化）：chunk_size 扫到 2048 时所有指标恰好 1.0000
    根因：块够大 → 检索到的块数少 → precision/nDCG 必然上升。
    ★ 指标选错了：「块越大越准」在检索指标上**必然成立**，
      因为大块把答案都装进去了，代价是无关内容也一起进来。
      → 检索指标**测不出**块大小的真实影响。

  问题 2（gold 定义过严）：gold 是「生成问句时用的那个单块」
    → 重排返回同替身的其他块被判为恶化。

★ 本实验的核心思路：
  **用生成层的「证据利用率」当主指标。**
  - 块小：内容精准，但可能装不下完整答案 → 利用率低
  - 块大：装得下，但塞满无关内容 → 利用率也低
  - **存在最优中间值** —— 这是检索指标永远看不到的权衡

★ 为什么这个指标能测出来：
  检索指标问「gold 块在不在 Top-K」，
  利用率问「检索到的内容有多少**真的被答案用上了**」。
  后者直接对应用户体验：答案里有多少是在念检索结果。

用法：
    python run_chunk_quality_eval.py --sizes 256,384,512,768,1024
    python run_chunk_quality_eval.py --quick# 只跑 3 个尺寸
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services"))
sys.path.insert(0, str(ROOT / "evaluation"))
sys.path.insert(0, str(ROOT / "retrieval"))
sys.path.insert(0, str(ROOT / "dataset" / "pipeline"))

from chunk import build_all, clean_text, is_noise_section  # noqa: E402
from faithfulness import (  # noqa: E402
    aggregate,
    evidence_utilization,
    extract_dim_values,
    score_one,
    split_sentences,
)

PROC = ROOT / "dataset" / "processed"
OUT = PROC / "m7_chunk_quality.json"

# 与 M3 D1b 相同的尺寸，便于对照
SIZES = [(256, 64), (384, 64), (512, 64), (768, 64), (1024, 128), (2048, 128)]


# ==================================================================
# 固定长度切块（带重叠）
# ==================================================================

def merge_chunks(base: list[dict], target: int,
                 overlap_blocks: int = 0) -> list[dict]:
    """把语义块按目标长度合并（同一替身内相邻合并）。

    ★★ 与 M3 D1b 的区别（重要）：
      M3 从**原始段落**定长切 → 段落本身均452 字，
         target=1024 时几乎切不动（只出 17 块），实验失效。
      本实现从**语义块**（p50=229 字）出发 → 合并才是真的在控块大小。

    ★ 为什么这更合理：
      语义块是 wiki 的天然语义单元（section / ability_overview 等），
      合并它们不会破坏语义完整性；而从段落重切会把句子拦腰截断。

    Args:
        base: 基准语义块（text_chunks.json）
        target: 目标块长（字符）
        overlap_blocks: 重叠块数（0 = 不重叠）
    """
    out: list[dict] = []
    # 按 stand_id 分组，保持原有顺序
    by_stand: dict[str, list[dict]] = {}
    for c in base:
        by_stand.setdefault(c.get("stand_id") or "", []).append(c)

    for sid, items in by_stand.items():
        buf: list[dict] = []
        buf_len = 0

        def flush(buf: list[dict]) -> None:
            if not buf:
                return
            out.append({
                "chunk_id": len(out),
                "stand_id": sid,
                "stand_name": buf[0].get("stand_name"),
                "part": buf[0].get("part"),
                "chunk_type": buf[0].get("chunk_type"),
                "section": " | ".join(dict.fromkeys(
                    b.get("section", "") for b in buf))[:120],
                "entity": sid,
                "content": "\n".join(b["content"] for b in buf),
                "n_chars": sum(len(b["content"]) for b in buf),
                "n_base_blocks": len(buf),
                "source_url": buf[0].get("source_url"),
            })

        for c in items:
            L = len(c["content"])
            # 单块就超目标 → 独立成块（不硬切）
            if L >= target:
                flush(buf)
                buf, buf_len = [], 0
                out.append({
                    "chunk_id": len(out), "stand_id": sid,
                    "stand_name": c.get("stand_name"), "part": c.get("part"),
                    "chunk_type": c.get("chunk_type"),
                    "section": c.get("section", ""), "entity": sid,
                    "content": c["content"], "n_chars": L, "n_base_blocks": 1,
                    "source_url": c.get("source_url"),
                })
                continue
            if buf_len + L > target and buf:
                flush(buf)
                # ★ 重叠：保留末尾若干块
                keep = buf[-overlap_blocks:] if overlap_blocks else []
                buf = list(keep)
                buf_len = sum(len(b["content"]) for b in buf)
            buf.append(c)
            buf_len += L
        flush(buf)
    return out


# ==================================================================
# 检索（BM25，复用 M3 结论的参数）
# ==================================================================

class SimpleBM25:
    """轻量 BM25（k1=1.2, b=0.5 —— M3 实测最优）。"""

    def __init__(self, chunks: list[dict]):
        import math
        from collections import Counter
        self.chunks = chunks
        self.k1, self.b = 1.2, 0.5
        self.docs: list[Counter] = []
        self.lens: list[int] = []
        df: Counter = Counter()
        for c in chunks:
            toks = self._tok(c["content"])
            tc = Counter(toks)
            self.docs.append(tc)
            self.lens.append(len(toks))
            for t in set(toks):
                df[t] += 1
        self.N = len(chunks)
        self.avgdl = sum(self.lens) / self.N if self.N else 1.0
        self.idf = {
            t: math.log(1 + (self.N - n + 0.5) / (n + 0.5))
            for t, n in df.items()
        }

    @staticmethod
    def _tok(s: str) -> list[str]:
        return re.findall(r"[a-z][a-z0-9'’]{1,}|[一-鿿]{2,}|\d+", s.lower())

    def search(self, query: str, top_k: int = 3,
               boost_stand: Optional[str] = None) -> list[tuple[int, float]]:
        q = self._tok(query)
        scores: list[float] = []
        for i, tc in enumerate(self.docs):
            s = 0.0
            for t in q:
                if t not in tc:
                    continue
                f = tc[t]
                dl = self.lens[i] or 1
                s += self.idf.get(t, 0) * f * (self.k1 + 1) / \
                     (f + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
            if boost_stand and self.chunks[i].get("stand_id") == boost_stand:
                s *= 1.5           # M3 用的 boost 系数
            scores.append(s)
        idx = sorted(range(len(scores)), key=lambda i: -scores[i])[:top_k]
        return [(self.chunks[i]["chunk_id"], scores[i]) for i in idx
                if scores[i] > 0]


# ==================================================================
# 评测
# ==================================================================

def load_rendered_pages() -> list[dict]:
    """载入渲染抓取的正文，按「段落」摊平。

    ★ 实测踩坑：details.json 只有 infobox 和 forms，**没有正文**。
      正文在 dataset/sources/rendered/*.json，结构：
        {stand_id, name_raw, url, overview: [...], sections: [{heading, paras, lists}]}
    """
    import glob
    out: list[dict] = []
    for f in sorted(glob.glob(str(ROOT / "dataset" / "sources" /
                                  "rendered" / "*.json"))):
        try:
            d = json.loads(Path(f).read_text(encoding="utf-8"))
        except Exception:
            continue
        sid = d.get("stand_id")
        url = d.get("url")
        # overview 是段落列表
        for para in (d.get("overview") or []):
            if isinstance(para, str) and len(para) > 30:
                out.append({"stand_id": sid, "content": para,
                            "section": "(lead)", "url": url})
        for sec in (d.get("sections") or []):
            head = sec.get("heading") or ""
            for para in (sec.get("paras") or []):
                if isinstance(para, str) and len(para) > 30:
                    out.append({"stand_id": sid, "content": para,
                                "section": head, "url": url})
            for li in (sec.get("lists") or []):
                items = li if isinstance(li, list) else [li]
                txt = " ".join(x for x in items if isinstance(x, str))
                if len(txt) > 30:
                    out.append({"stand_id": sid, "content": txt,
                                "section": head, "url": url})
    return out


def load_tasks(n: int, seed: int = 42) -> list[dict]:
    """取有证据结构的题目（T4 + T1 + T7）。"""
    items = json.loads((PROC / "eval_set.json").read_text(encoding="utf-8"))
    pool = [x for x in items if x["question_type"] in ("T4", "T1", "T7")]
    per = max(1, n // 3)
    picked: list[dict] = []
    import random
    rng = random.Random(seed)
    for t in ("T4", "T1", "T7"):
        sub = [x for x in pool if x["question_type"] == t]
        picked.extend(rng.sample(sub, min(per, len(sub))))
    rng.shuffle(picked)
    return picked[:n]


def guess_stand_id(q: str, name2id: dict[str, str]) -> Optional[str]:
    for name in sorted(name2id, key=len, reverse=True):
        if name and name.lower() in q.lower():
            return name2id[name]
    return None


def run_size(target: int, overlap: int, tasks: list[dict],
             base: list[dict], gen, name2id: dict, known: set[str],
             top_k: int = 3) -> dict:
    """跑一个块尺寸。"""
    t0 = time.time()
    # overlap 参数换算成「重叠块数」（按 229 字/块 估）
    ov_blocks = max(0, round(overlap / 229))
    chunks = merge_chunks(base, target, ov_blocks)
    t_chunk = time.time() - t0

    bm = SimpleBM25(chunks)
    cmap = {c["chunk_id"]: c for c in chunks}

    records: list[dict] = []
    scores = []
    t1 = time.time()
    for task in tasks:
        q = task["question"]
        sid = guess_stand_id(q, name2id)
        hits = bm.search(q, top_k=top_k, boost_stand=sid)
        ev = [cmap[cid] for cid, _ in hits]
        if not ev:
            continue
        ans = gen.generate(q, ev)
        s = score_one(ans.text, [e["content"] for e in ev], known)
        # ★ 追加：块级统计
        n_chars = sum(e["n_chars"] for e in ev)
        s.to_dict()["avg_evidence_chars"] = n_chars
        scores.append(s)
        records.append({
            "qid": task["qid"], "question": q,
            "answer": ans.text[:400],
            "util": evidence_utilization(ans.text, [e["content"] for e in ev]),
            "score": s.to_dict(),
            "evidence_chars": n_chars,
        })
    t_total = time.time() - t0

    agg = aggregate(scores)
    # 块级统计
    util = [r["util"]["ratio"] for r in records]
    chars = [r["evidence_chars"] for r in records]
    n_chunks_per_q = [len(r["util"].get("_n", [])) or top_k for r in records]

    return {
        "target_len": target, "overlap": overlap,
        "n_chunks": len(chunks),
        "avg_chunk_chars": (sum(c["n_chars"] for c in chunks) / len(chunks)
                            if chunks else 0),
        "chunk_time_sec": round(t_chunk, 1),
        "total_time_sec": round(t_total, 1),
        "trace_ratio": agg["trace_ratio"],
        "numeric_ratio": agg["numeric_ratio"],
        "contradiction_rate": agg["contradiction_rate"],
        "util_ratio": sum(util) / len(util) if util else 0.0,
        "hedging_rate": agg["hedging_rate"],
        "avg_evidence_chars": sum(chars) / len(chars) if chars else 0,
        "n": len(records),
    }


# ==================================================================
def print_report(results: list[dict]) -> None:
    print("\n" + "=" * 76)
    print("块大小 × 生成质量（M7）")
    print("=" * 76)
    hdr = (f"{'块长':>6s} {'重叠':>5s} {'块数':>6s} {'块均字数':>9s} "
           f"{'溯源':>7s} {'利用率':>7s} {'矛盾率':>7s} {'空洞率':>7s} {'证据字数':>9s}")
    print(hdr)
    print("-" * 76)
    for r in results:
        def f(v):
            return "n/a" if v is None else f"{v:.4f}"
        print(f"{r['target_len']:>6d} {r['overlap']:>5d} {r['n_chunks']:>6d} "
              f"{r['avg_chunk_chars']:>9.0f} {f(r['trace_ratio']):>7s} "
              f"{f(r['util_ratio']):>7s} {f(r['contradiction_rate']):>7s} "
              f"{f(r['hedging_rate']):>7s} {r['avg_evidence_chars']:>9.0f}")

    # ---- 判读 ----
    print("\n" + "=" * 76)
    print("判读")
    print("=" * 76)
    valid = [r for r in results if r["util_ratio"] is not None]
    if not valid:
        print("  无有效数据")
        return
    best = max(valid, key=lambda r: r["util_ratio"])
    worst = min(valid, key=lambda r: r["util_ratio"])
    print(f"  ★ 利用率最优：{best['target_len']}（{best['util_ratio']:.4f}）")
    print(f"    利用率最差：{worst['target_len']}（{worst['util_ratio']:.4f}）")

    # 是否存在中间最优？
    sizes = [r["target_len"] for r in valid]
    utils = [r["util_ratio"] for r in valid]
    mid_peak = (sizes[0] not in (max(utils and
                                     [u for u, s in zip(utils, sizes)
                                      if s == max(utils)] or [0], default=0))
                )
    if mid_peak:
        print("  → ★ **存在中间最优** —— 这正是检索指标看不到的权衡")
    else:
        print("  → 利用率随块大小单调变化，未见中间最优")

    # 溯源 vs 利用率的对比
    tr_max = max(valid, key=lambda r: r["trace_ratio"])
    print(f"\n  ★ 两个指标给出了不同答案：")
    print(f"      溯源最优  = {tr_max['target_len']}（{tr_max['trace_ratio']:.4f}）")
    print(f"      利用率最优= {best['target_len']}（{best['util_ratio']:.4f}）")
    if tr_max["target_len"] != best["target_len"]:
        print(f"      → ★★ 指标选型直接改变结论，这是 M3 问题的根源")


# ==================================================================
def main() -> int:
    ap = argparse.ArgumentParser(description="M7 块大小 × 生成质量")
    ap.add_argument("--sizes", default="", help="逗号分隔，如 256,512,1024")
    ap.add_argument("--n", type=int, default=24, help="题目数")
    ap.add_argument("--top-k", type=int, default=3)
    ap.add_argument("--quick", action="store_true", help="只跑 3 个尺寸")
    ap.add_argument("--no-llm", action="store_true")
    args = ap.parse_args()

    print("=" * 76)
    print("M7 块大小 × 生成质量联合实验")
    print("=" * 76)

    sizes = SIZES
    if args.sizes:
        want = {int(x) for x in args.sizes.split(",")}
        sizes = [s for s in SIZES if s[0] in want]
    elif args.quick:
        sizes = [s for s in SIZES if s[0] in (384, 512, 1024)]
    print(f"  尺寸：{[s[0] for s in sizes]}  题目：{args.n}")

    # ★★ 基准用**语义块**而非原始段落
    #   实测踩坑：从原始段落定长切时，段落本身均 452 字，
    #   target=1024 几乎切不动（只出 17 块）→ 实验失效。
    #   语义块 p50=229 字，合并才真能控制块大小。
    base = json.loads((PROC / "text_chunks.json").read_text(encoding="utf-8"))
    lens = sorted(len(c["content"]) for c in base)
    print(f"  基准语义块：{len(base)} 个"
          f"（p50={lens[len(lens)//2]} p90={lens[9*len(lens)//10]}）")


    tasks = load_tasks(args.n)
    stands = json.loads((PROC / "stands.json").read_text(encoding="utf-8"))
    name2id = {x["name_en"]: x["stand_id"] for x in stands if x.get("name_en")}
    known = set(name2id)
    print(f"  题目 {len(tasks)} 条")

    gen = None
    if not args.no_llm:
        from generator import Generator
        gen = Generator()
        if gen.model is None:
            print(f"  !! 生成模型不可用：{gen.info().get('error')}")
        else:
            gen.warmup()

    results: list[dict] = []
    for target, overlap in sizes:
        print(f"\n[{target}/{overlap}] 切块中...", flush=True)
        if gen is None:
            print("  无生成模型，跳过")
            break
        r = run_size(target, overlap, tasks, base, gen, name2id, known,
                     args.top_k)
        results.append(r)
        print(f"  块数 {r['n_chunks']}  溯源 {r['trace_ratio']}  "
              f"利用率 {r['util_ratio']}  空洞 {r['hedging_rate']}")

    print_report(results)
    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"\n输出：{OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

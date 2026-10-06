"""
忠实度评测编排（M6）。

跑四个配置的对照实验，回答三个问题：
  1. 生成式答案的忠实度是多少？
  2. **证据到底起作用了吗？**（配置 C vs A）
  3. 指标能区分证据质量吗？（配置 D vs A）

用法：
    python run_faithfulness_eval.py--n 30
    python run_faithfulness_eval.py --configs A,C # 只跑指定配置
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services"))
sys.path.insert(0, str(ROOT / "evaluation"))
sys.path.insert(0, str(ROOT / "retrieval"))

from faithfulness import (  # noqa: E402
    aggregate,
    score_one,
    shuffle_evidence,
    strip_evidence,
)

PROC = ROOT / "dataset" / "processed"
OUT = PROC / "m6_faithfulness.json"

CONFIGS = {
    "A": {"name": "生成式 + 真实证据", "mode": "generate",
          "evidence": "real",
          "desc": "本项目主指标"},
    "B": {"name": "抽取式（M5 现状）", "mode": "extractive",
          "evidence": "real",
          "desc": "忠实度上界（照抄不可能幻觉）"},
    "C": {"name": "生成式 + 空证据", "mode": "generate",
          "evidence": "none",
          "desc": "★ 关键对照：测模型编造程度"},
    "D": {"name": "生成式 + 无关注证据", "mode": "generate",
          "evidence": "shuffled",
          "desc": "★ 敏感度检验：指标能否区分证据质量"},
}


# ==================================================================
def load_tasks(n: int, seed: int = 42,
               types: tuple[str, ...] = ("T4", "T1", "T7")) -> list[dict]:
    """取评测集里「证据 → 答案」结构的题目。

    ★★ 修正（实测发现的设计缺陷）：
      最初只取 T4（语义理解），结果 **numeric_ratio 全为 None** ——
      因为 T4 全是描述类问题（「外观形态方面有哪些描述」），
      根本没有「X 是几级」这种数值断言，指标用不上。

      忠实度的两个维度需要**不同题型**来测：
        - 溯源/矛盾 → T4（描述类，长文本，容易改写）
        - 数值正确  → T1（几级）/ T7（多少，含∞/未知等异常值）

    → 默认三类都取，才能同时覆盖两个维度。
    """
    items = json.loads(
        (ROOT / "dataset" / "processed" / "eval_set.json").read_text(
            encoding="utf-8"))
    pool = [x for x in items if x["question_type"] in types]
    # 每题型配额，避免某类独大
    per = max(1, n // len(types))
    picked: list[dict] = []
    rng = random.Random(seed)
    for t in types:
        sub = [x for x in pool if x["question_type"] == t]
        picked.extend(rng.sample(sub, min(per, len(sub))))
    rng.shuffle(picked)
    return picked[:n]


def load_known_stands() -> set[str]:
    stands = json.loads((PROC / "stands.json").read_text(encoding="utf-8"))
    return {s["name_en"] for s in stands if s.get("name_en")}


def guess_stand_id(question: str, name2id: dict[str, str]) -> Optional[str]:
    """从问句里猜替身 stand_id（长名优先）。

    ★★ 绝不能用评测集的 gold 块来 boost —— 那是用答案去检索，
       属于作弊，会让检索指标虚高。必须只从**问句文本**推断。
    """
    for name in sorted(name2id, key=len, reverse=True):
        if name and name.lower() in question.lower():
            return name2id[name]
    return None


# ==================================================================
def run_config(cfg_key: str, tasks: list[dict], gen, sem,
               known: set[str], name2id: dict[str, str],
               top_k: int = 3) -> dict:
    """跑一个配置。"""
    cfg = CONFIGS[cfg_key]
    scores: list = []
    records: list[dict] = []
    t_cfg = time.time()

    for n, task in enumerate(tasks, 1):
        q = task["question"]
        # 取真实证据（各配置共用同一份，保证唯一变量是「证据如何处理」）
        # ★ 从问句推断替身（不用 gold，避免作弊）
        sid = guess_stand_id(q, name2id)
        raw_ev, _ = sem.search(q, top_k=top_k, boost_stand=sid)

        if not raw_ev:
            continue

        # ★ 统一保持 dict 形态（strip/shuffle 返回的是字符串）
        texts = [e["content"] for e in raw_ev]
        if cfg["evidence"] == "real":
            ev = raw_ev
        elif cfg["evidence"] == "none":
            # ★ 保留块数与元信息，只清空内容 —— 唯一变量是「有没有内容」
            ev = [{**e, "content": ""} for e in raw_ev]
            assert len(ev) == len(texts)
        elif cfg["evidence"] == "shuffled":
            # ★ 换成**其他替身的真实文本**（不是乱码）——
            #   词袋模型下打乱顺序不改变 token 集合，测不出差异
            sh = shuffle_evidence(texts)
            ev = [{**e, "content": sh[i] if i < len(sh) else ""}
                  for i, e in enumerate(raw_ev)]
        else:
            ev = raw_ev

        # 生成
        try:
            if cfg["mode"] == "generate":
                ans = gen.generate(q, ev)
            else:
                ans = gen.extractive(q, ev)
        except Exception as e:
            print(f"    [{n}] 生成失败: {type(e).__name__}: {e}")
            continue

        ev_texts = [e["content"] for e in ev]
        s = score_one(ans.text, ev_texts, known)
        scores.append(s)
        records.append({
            "qid": task["qid"],
            "question": q,
            "answer": ans.text[:600],
            "score": s.to_dict(),
            "gen_ms": round(ans.elapsed_ms, 1),
            "n_gen_tokens": ans.n_gen_tokens,
        })

        if n % 5 == 0:
            print(f"    {n}/{len(tasks)}  ({time.time() - t_cfg:.0f}s)")

    agg = aggregate(scores)
    agg["config_key"] = cfg_key
    agg["config_name"] = cfg["name"]
    agg["config_desc"] = cfg["desc"]
    agg["elapsed_sec"] = round(time.time() - t_cfg, 1)
    return {"summary": agg, "records": records}


# ==================================================================
def print_report(results: dict[str, dict]) -> None:
    print("\n" + "=" * 74)
    print("忠实度评测结果（M6）")
    print("=" * 74)

    def fmt(v, nd=4):
        #★ 区分「真的是 0」与「该指标不适用（None）」
        #   实测踩坑：T4 没有数值断言 → ratio=None，
        #   若显示成 0.0000 会让人误以为「数值全错」
        if v is None:
            return "   n/a"
        if isinstance(v, float) and math.isnan(v):
            return "   n/a"
        return f"{v:.{nd}f}"

    hdr = (f"{'配置':22s} {'溯源':>7s} {'数值':>7s} {'实体':>7s} "
           f"{'矛盾率':>7s} {'利用率':>7s} {'空洞率':>7s}")
    print(hdr)
    print("-" * len(hdr.encode('gbk', errors='ignore')))
    for k in ("A", "B", "C", "D"):
        if k not in results:
            continue
        s = results[k]["summary"]
        print(f"{s['config_name'][:20]:22s} {fmt(s['trace_ratio']):>7s} "
              f"{fmt(s['numeric_ratio']):>7s} {fmt(s['entity_ratio']):>7s} "
              f"{fmt(s['contradiction_rate']):>7s} {fmt(s['util_ratio']):>7s} "
              f"{fmt(s['hedging_rate']):>7s}")

    # ---- 判读 ----
    print("\n" + "=" * 74)
    print("判读")
    print("=" * 74)
    A = results.get("A", {}).get("summary")
    B = results.get("B", {}).get("summary")
    C = results.get("C", {}).get("summary")
    D = results.get("D", {}).get("summary")

    if A and B:
        print(f"  忠实度上界（B 抽取式）: {fmt(B['trace_ratio'])}")
        print(f"  生成式（A组）      : {fmt(A['trace_ratio'])}")
        gap = (B['trace_ratio'] or 0) - (A['trace_ratio'] or 0)
        if gap > 0.05:
            print(f"  → 生成带来了{ gap * 100:.1f}pp 的忠实度损失")

    if A and C:
        at = A['trace_ratio'] or 0
        ct = C['trace_ratio'] or 0
        print(f"\n  ★ 证据有效性检验：")
        print(f"      A（有证据）溯源 = {fmt(at)}")
        print(f"      C（无证据）溯源 = {fmt(ct)}")
        if at - ct > 0.15:
            print(f"      → 证据起作用了（差 {(at-ct)*100:.1f}pp）✓ RAG 有效")
        else:
            print(f"      → ★ 证据几乎没起作用（差 {(at-ct)*100:.1f}pp）")
            print(f"        ⚠️  RAG 可能只是装饰层，需排查")

    if A and D:
        at = A['trace_ratio'] or 0
        dt = D['trace_ratio'] or 0
        print(f"\n  ★ 指标敏感度检验：")
        print(f"      A（相关证据）溯源 = {fmt(at)}")
        print(f"      D（无关证据）溯源 = {fmt(dt)}")
        if at - dt > 0.2:
            print(f"      → 指标能区分证据质量（差 {(at-dt)*100:.1f}pp）✓")
        else:
            print(f"      → ★ 指标测不出证据质量差异（差 {(at-dt)*100:.1f}pp）")
            print(f"        ⚠️  指标本身失效，需重新设计")

    if C:
        ct = C['trace_ratio'] or 0
        print(f"\n  模型编造程度（C 组无证据时仍能编出{ct*100:.0f}% 的「有据」答案）")
        if ct > 0.5:
            print("  ⚠️  1.5B 模型在没有证据时也会「自信地编造」")
            print("      → 这正是 RAG 存在的必要性，也是本项目的核心论点")


# ==================================================================
def main() -> int:
    ap = argparse.ArgumentParser(description="M6 忠实度评测")
    ap.add_argument("--n", type=int, default=30, help="题目数")
    ap.add_argument("--configs", default="A,B,C,D", help="要跑的配置")
    ap.add_argument("--top-k", type=int, default=3, help="每题检索块数")
    ap.add_argument("--no-llm", action="store_true",
                    help="不加载生成模型（只跑 B 抽取式对照）")
    args = ap.parse_args()

    print("=" * 74)
    print("M6 忠实度评测")
    print("=" * 74)

    tasks = load_tasks(args.n)
    known = load_known_stands()
    stands = json.loads((PROC / "stands.json").read_text(encoding="utf-8"))
    name2id = {x["name_en"]: x["stand_id"] for x in stands if x.get("name_en")}
    n_boost = sum(1 for t in tasks if guess_stand_id(t["question"], name2id))
    from collections import Counter as _C
    _tc = _C(t["question_type"] for t in tasks)
    print(f"  题目 {len(tasks)} 条 {dict(_tc)} / 已知替身 {len(known)} 个")
    print(f"  其中 {n_boost} 条可从问句推断替身（用于检索加权）")

    # 语义检索
    from semantic_executor import SemanticExecutor
    sem = SemanticExecutor(use_vector=True, verbose=True)
    t0 = time.time()
    use_vec = sem.load()
    print(f"  检索: {'BM25 + bge-m3' if use_vec else '仅 BM25'}"
          f"（{time.time() - t0:.1f}s）")

    # 生成模型
    gen = None
    if not args.no_llm:
        from generator import Generator
        gen = Generator()
        print(f"  生成: {gen.info()}")
        if gen.model is None:
            print("  !! 生成模型不可用，只跑 B 组")
        else:
            gen.warmup()

    results: dict[str, dict] = {}
    for key in args.configs.split(","):
        key = key.strip().upper()
        if key not in CONFIGS:
            print(f"  跳过未知配置 {key}")
            continue
        if CONFIGS[key]["mode"] == "generate" and gen is None:
            print(f"\n[{key}] 跳过（无生成模型）")
            continue
        print(f"\n[{key}] {CONFIGS[key]['name']}")
        results[key] = run_config(key, tasks, gen, sem, known, name2id,
                                   args.top_k)

    print_report(results)
    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"\n输出：{OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

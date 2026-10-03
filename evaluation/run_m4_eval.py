"""
M4 评测：冲突消解（D5 消融）+ 路由改进（D9）。

对照M2 基线：
  路由准确率 0.8352（规则路由），两个短板：
    - T5 混合协同 0/21 = 0.000
    - T6 无答案   12/20 = 0.600
  冲突消解：M1 全部用 prefer_primary，无消融实验

本脚本产出：
  1. 路由准确率对比（M2 规则路由 vs M4 增强路由）
  2. 冲突消解策略消融（prefer_primary vs prefer_consensus）
  3. **消解的敏感性分析** —— 哪些结论换个策略就翻转
     这是 M4 的核心贡献：证明「冲突消解不是免费的」

用法：
    python run_m4_eval.py
    python run_m4_eval.py --consensus-only
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services"))
sys.path.insert(0, str(ROOT / "evaluation"))
sys.path.insert(0, str(ROOT / "retrieval"))

from conflict_resolver import (  # noqa: E402
    SOURCE_LABEL,
    ConflictResolver,
    Router,
)
from judge import load_eval_set, print_report  # noqa: E402
from lexical import route_by_rules  # noqa: E402

PROC = ROOT / "dataset" / "processed"
OUT = PROC / "m4_results.json"


# ==================================================================
# 1. 路由评测
# ==================================================================

def eval_routing(items: list[dict]) -> dict:
    """对比 M2 规则路由与 M4 增强路由。"""
    stands = json.loads((PROC / "stands.json").read_text(encoding="utf-8"))

    # ★ 已知实体必须包含三类，否则会把真实存在的实体误判为编造：
    #   替身名 + 部名 + 使用者名
    #   （只用替身名会让「Stardust Crusaders（第3部）」「Giorno Giovanna」被误判，
    #实测导致 T2 掉 20%、T3 掉 40%）
    known = {s["name_en"] for s in stands if s.get("name_en")}
    known |= {s.get("part_name_en") for s in stands if s.get("part_name_en")}
    known |= {s.get("owner_name") for s in stands if s.get("owner_name")}
    # 部名也可能是纯数字形式
    known |= {f"第{s['part']}部" for s in stands if s.get("part")}
    known.discard(None)
    known.discard("")
    router = Router(known_stands={s["name_en"] for s in stands},
                    known_entities=known)
    results = {"m2_rules": [], "m4_router": []}
    for it in items:
        exp = it.get("route_expect", "structured")
        results["m2_rules"].append({
            "qid": it["qid"], "type": it["question_type"],
            "expect": exp, "pred": route_by_rules(it["question"]),
        })
        d = router.route(it["question"])
        results["m4_router"].append({
            "qid": it["qid"], "type": it["question_type"],
            "expect": exp, "pred": d.route, "reason": d.reason,
        })

    return results


def routing_report(rows: list[dict], label: str) -> dict:
    by_type: dict[str, list[int]] = defaultdict(list)
    for r in rows:
        by_type[r["type"]].append(1 if r["expect"] == r["pred"] else 0)
    overall = sum(1 for r in rows if r["expect"] == r["pred"]) / len(rows)
    return {
        "label": label,
        "n": len(rows),
        "accuracy": round(overall, 4),
        "by_type": {t: {"n": len(v), "acc": round(sum(v) / len(v), 4)}
                    for t, v in sorted(by_type.items())},
    }


def print_routing_comparison(a: dict, b: dict) -> None:
    print("\n" + "=" * 68)
    print("路由对比（M4 的 D9）")
    print("=" * 68)
    hdr = f"{'题型':8s} {'n':>4s} {'M2 规则':>10s} {'M4 增强':>10s} {'变化':>10s}"
    print(hdr)
    print("-" * len(hdr))
    types = sorted(set(a["by_type"]) | set(b["by_type"]))
    for t in types:
        aa = a["by_type"].get(t, {"n": 0, "acc": 0})
        bb = b["by_type"].get(t, {"n": 0, "acc": 0})
        delta = bb["acc"] - aa["acc"]
        mark = " ←" if abs(delta) > 0.001 else ""
        print(f"{t:8s} {aa['n']:>4d} {aa['acc']:>10.4f} {bb['acc']:>10.4f} "
              f"{delta:>+10.4f}{mark}")
    print("-" * len(hdr))
    print(f"{'总体':8s} {a['n']:>4d} {a['accuracy']:>10.4f} {b['accuracy']:>10.4f} "
          f"{b['accuracy'] - a['accuracy']:>+10.4f}")


def print_confusion(rows: list[dict], label: str) -> None:
    routes = sorted({r["expect"] for r in rows} | {r["pred"] for r in rows})
    print(f"\n  混淆矩阵（{label}）：")
    print(f"  {'期望\\预测':14s}" + "".join(f"{r[:10]:>12s}" for r in routes))
    for exp in routes:
        line = [sum(1 for r in rows
                    if r["expect"] == exp and r["pred"] == p) for p in routes]
        print(f"  {exp:14s}" + "".join(f"{v:>12d}" for v in line))


# ==================================================================
# 2. 冲突消解消融（D5）
# ==================================================================

def eval_resolution(items: list[dict]) -> dict:
    conflicts = json.loads((PROC / "conflicts.json").read_text(encoding="utf-8"))
    stands = json.loads((PROC / "stands.json").read_text(encoding="utf-8"))

    out = {}
    for flag, label in ((False, "M1 策略（prefer_primary）"),
                        (True, "M4 策略（含共识）")):
        r = ConflictResolver(use_consensus=flag)
        res = r.resolve_all(conflicts, stands)
        strategies = Counter(x.strategy for x in res)
        conf = [x.confidence for x in res]
        fragile = [x for x in res if x.sensitivity.startswith("**")]
        out[label] = {
            "n": len(res),
            "avg_confidence": round(sum(conf) / len(conf), 4),
            "min_confidence": round(min(conf), 4),
            "strategy_dist": dict(strategies.most_common()),
            "n_fragile": len(fragile),
            "fragile_ratio": round(len(fragile) / len(res), 4),
            "resolutions": [x.to_dict() for x in res],
        }
    return out


def print_resolution_report(a: dict, b: dict) -> None:
    print("\n" + "=" * 68)
    print("冲突消解消融（D5）")
    print("=" * 68)
    hdr = f"{'指标':22s} {'M1 仅主源':>12s} {'M4 含共识':>12s} {'变化':>12s}"
    print(hdr)
    print("-" * len(hdr))
    for key, name in [("avg_confidence", "平均置信度"),
                      ("min_confidence", "最低置信度"),
                      ("n_fragile", "脆弱结论数"),
                      ("fragile_ratio", "脆弱结论占比")]:
        va, vb = a[key], b[key]
        print(f"{name:22s} {va:>12.4f} {vb:>12.4f} {vb - va:>+12.4f}")

    print(f"\n  策略分布：")
    keys = sorted(set(a["strategy_dist"]) | set(b["strategy_dist"]))
    print(f"  {'策略':22s} {'M1':>8s} {'M4':>8s}")
    for k in keys:
        print(f"  {k:22s} {a['strategy_dist'].get(k, 0):>8d} "
              f"{b['strategy_dist'].get(k, 0):>8d}")


def print_sensitivity(b: dict) -> None:
    """敏感性分析：换策略就翻转的结论。"""
    print("\n" + "=" * 68)
    print("★ 敏感性分析 —— 冲突消解不是免费的")
    print("=" * 68)
    rows = b["resolutions"]
    flips = [r for r in rows if r["sensitivity"].startswith("**")]
    stable = [r for r in rows if not r["sensitivity"].startswith("**")]
    print(f"  稳健结论（不依赖策略）：{len(stable)} 条")
    print(f"  脆弱结论（换策略即翻转）：{len(flips)} 条"
          f"  ← 这些是消解的「代价」")
    print()
    print(f"  {'替身':22s} {'维度':5s} {'M1 结论':>10s} {'M4 结论':>10s} 原因")
    for r in flips[:8]:
        print(f"  {r['stand_name'][:20]:22s} {r['stat_dim']:5s} "
              f"{str(r['value_a'])[:8]:>10s} {str(r['final_raw'])[:8]:>10s} "
              f"{r['rationale'][:44]}")

    # 关键洞察
    print()
    print("  ★ 核心洞察：")
    f1 = [r for r in flips if r["conflict_type"] == "CATEGORY_DIFF"]
    f2 = [r for r in flips if r["conflict_type"] == "VALUE_MISMATCH"]
    print(f"    · {len(f1)} 条脆弱结论源于「主源标?，镜像源给具体值」")
    print(f"      → 换策略会翻转，但本项目选择保留 UNKNOWN（不臆测）")
    print(f"    · {len(f2)} 条脆弱结论源于「主源与镜像源不一致」")
    print(f"      → 共识策略下翻转，主源策略下取主源")
    print(f"    ★ 这两类都需要在报告中显式说明，不能让读者以为「已确定」")


# ==================================================================
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--consensus-only", action="store_true")
    args = ap.parse_args()

    print("=" * 68)
    print("M4 评测：冲突消解 + 路由")
    print("=" * 68)

    items = load_eval_set()
    print(f"评测集 {len(items)} 条")

    # ---- 路由 ----
    routes = eval_routing(items)
    rep_m2 = routing_report(routes["m2_rules"], "M2 规则路由")
    rep_m4 = routing_report(routes["m4_router"], "M4 增强路由")
    print_routing_comparison(rep_m2, rep_m4)
    print_confusion(routes["m4_router"], "M4 增强路由")

    # ---- 消解 ----
    res = eval_resolution(items)
    a = res["M1 策略（prefer_primary）"]
    b = res["M4 策略（含共识）"]
    print_resolution_report(a, b)
    print_sensitivity(b)

    # ---- 落盘 ----
    OUT.write_text(json.dumps({
        "routing": {"m2": rep_m2, "m4": rep_m4,
                    "m2_detail": routes["m2_rules"],
                    "m4_detail": routes["m4_router"]},
        "resolution": res,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n输出：{OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

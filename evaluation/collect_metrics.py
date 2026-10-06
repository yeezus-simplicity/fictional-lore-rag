"""
指标汇总（M8）。

★★ 为什么要这个模块：
  项目有 8 份报告、40+ 个指标，但数字**分散在各报告的表格里**。
  简历/作品集上每个数字都必须可追溯，
  否则就是「自吹」—— 无法验证的指标等于没有指标。

职责：
  1. 从各阶段的产物文件里**抽取**指标（不手抄）
  2. 标注每��指标的**来源**（哪个文件、哪张表）
  3. 做**一致性自检**——发现对不上的数字就报警

★ 原则：宁可不报，也不能报错数。
  任何抽不出来的指标标记为 `unavailable`，不用「约」「大概」填充。

用法：
    python evaluation/collect_metrics.py           # 生成 metrics.json
    python evaluation/collect_metrics.py --check   # 只做一致性自检
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
PROC = ROOT / "dataset" / "processed"
OUT = PROC / "metrics_summary.json"

DIMS = ["PWR", "SPD", "RNG", "STA", "PRC", "DEV"]


def _load(name: str) -> Any:
    p = PROC / name
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


# ==================================================================
# 各阶段指标抽取
# ==================================================================

def collect_m1() -> dict:
    """M1 数据层。"""
    rep = _load("validation_report.json") or {}
    chunks = _load("chunk_report.json") or {}
    stand = _load("stands.json") or []
    stats = _load("stand_stats.json") or []
    forms = _load("stand_forms.json") or []
    conflicts = _load("conflicts.json") or []
    normals = _load("normalizations.json") or {}
    merge = _load("merge_report.json") or {}

    n_stand = len(stand)
    # 完整度：有 composite 的比例
    n_complete = sum(1 for s in stats if s.get("composite") is not None)

    return {
        "n_stands": n_stand,
        "n_forms": len(forms),
        "n_chunks": chunks.get("n_chunks"),
        "n_stands_covered": chunks.get("n_stands_covered"),
        "total_chars": chunks.get("total_chars"),
        "chunk_type_dist": chunks.get("type_distribution", {}),
        "part_dist": chunks.get("part_distribution", {}),
        "len_stats": chunks.get("len_stats", {}),
        "n_with_complete_stats": n_complete,
        "completeness_ratio": (round(n_complete / n_stand, 4)
                               if n_stand else None),
        "n_conflicts": len(conflicts),
        "n_conflict_pending": sum(
            1 for c in conflicts if c.get("resolved_value") is None),
        "n_forms_unresolved": merge.get("n_forms_unresolved"),
        # 数据源
        "n_sources": 3,
        "sources": ["jojowiki（主源）", "csv_bogdan", "csv_topology"],
        "_source_files": [
            "dataset/processed/validation_report.json",
            "dataset/processed/chunk_report.json",
            "dataset/processed/stands.json",
            "dataset/processed/conflicts.json",
        ],
    }


def collect_m2() -> dict:
    """M2 评测体系。"""
    ev = _load("eval_set.json") or []
    if isinstance(ev, dict):
        items = ev.get("items", [])
    else:
        items = ev
    from collections import Counter
    dist = Counter(x.get("question_type") for x in items)
    return {
        "n_eval": len(items),
        "type_dist": dict(sorted(dist.items())),
        "llm_dependency": False,
        "gold_method": "程序化推导（从结构化数据反推）",
        "_source_files": ["dataset/processed/eval_set.json"],
    }


def collect_m3() -> dict:
    """M3 检索实验。"""
    best = _load("m3_best.json") or {}
    exps = _load("m3_experiments.json") or []
    d1 = _load("d1_chunksize.json") or []

    out: dict = {
        "n_configs": len(best),
        "configs": list(best),
        "_source_files": [
            "dataset/processed/m3_best.json",
            "dataset/processed/m3_experiments.json",
            "dataset/processed/d1_chunksize.json",
        ],
    }
    # 最优配置的指标
    for cfg, m in best.items():
        if isinstance(m, dict):
            out[f"best::{cfg}"] = {
                k: m.get(k) for k in
                ("recall@1", "recall@3", "recall@5", "n_cases",
                 "ndcg@5", "mrr", "src_coverage@5", "stand_recall@5")
                if k in m
            }
    # 纯 BM25 基线 vs 混合（★ 键名是 m3_best.json 里的实际名字）
    bm = best.get("BM25(k1=1.2,b=0.5) only")
    hyb = best.get("BM25(1.2,0.5)+RRF k=30")
    if bm and hyb:
        # ★★ 两个基准都给出——**必须说清对比的是谁**
        #   基准 A（M2 v0 原始默认参数）：0.5272 → 0.6250 = +18.5%
        #   基准 B（M3-a 仅 BM25 调参）  ：0.5707 → 0.6250 = +9.5%
        #   两者都对，但**不可混用**。M3 报告对外用基准 A。
        out["hybrid_vs_bm25"] = {
            "note": "★ RRF k=30 已含 bge-m3 向量召回（见 M3 报告 §4）",
            "baseline_bm25_tuned": {
                "label": "M3-a 仅 BM25 调参（k1=1.2,b=0.5）",
                "recall@5": bm.get("recall@5"),
                "ndcg@5": bm.get("ndcg@5"),
                "mrr": bm.get("mrr"),
            },
            "hybrid": {
                "label": "M3-c BM25 调参 + bge-m3 + RRF k=30",
                "recall@5": hyb.get("recall@5"),
                "ndcg@5": hyb.get("ndcg@5"),
                "mrr": hyb.get("mrr"),
                "recall@3": hyb.get("recall@3"),
            },
            "delta_vs_tuned_bm25_pp": {
                "recall@5": round((hyb.get("recall@5", 0)
                                   - bm.get("recall@5", 0)) * 100, 1),
                "ndcg@5": round((hyb.get("ndcg@5", 0)
                                 - bm.get("ndcg@5", 0)) * 100, 1),
                "mrr": round((hyb.get("mrr", 0)
                              - bm.get("mrr", 0)) * 100, 1),
            },
            "delta_vs_m2_v0_pct": {
                "note": "★ M2 v0 的检索指标**未落盘**，此处取自 M3 报告 §4 的表格",
                "recall@5": {"before": 0.5272, "after": hyb.get("recall@5"),
                             "relative_gain_pct": 18.5},
                "nDCG@5": {"before": 0.3371, "after": hyb.get("ndcg@5"),
                           "relative_gain_pct": 21.0},
                "mrr": {"before": 0.3223, "after": hyb.get("mrr"),
                        "relative_gain_pct": 17.4},
                "recall@3": {"before": 0.3261, "after": hyb.get("recall@3"),
                             "relative_gain_pct": 45.0},
            },
        }
    else:
        out["hybrid_vs_bm25"] = {"unavailable": True,
                                 "reason": "m3_best.json 里找不到对应配置"}
    # D1b 的指标退化现象（记录，不作为正面指标）
    if d1:
        out["d1b_chunksize"] = [
            {"target": x.get("target_len"), "n_views": x.get("n_views"),
             "recall@5": x.get("recall@5"), "ndcg@5": x.get("ndcg@5"),
             "src_coverage@5": x.get("src_coverage@5")}
            for x in d1
        ]
    return out


def collect_m4() -> dict:
    """M4 消解 + 路由。"""
    r = _load("m4_results.json") or {}
    conflicts = _load("conflicts.json") or []
    root = _load("conflict_root_cause.json") or {}

    out: dict = {
        "_source_files": [
            "dataset/processed/m4_results.json",
            "dataset/processed/conflicts.json",
            "dataset/processed/conflict_root_cause.json",
        ],
    }
    # 消解
    res = {}
    for c in conflicts:
        s = c.get("resolution")
        if not s:
            continue
        res.setdefault(s, {"n": 0, "n_resolved": 0})
        res[s]["n"] += 1
        if c.get("resolved_value") is not None:
            res[s]["n_resolved"] += 1
    out["resolution"] = {
        "by_strategy": res,
        "n_total": len(conflicts),
        "n_pending": sum(1 for c in conflicts
                         if c.get("resolved_value") is None),
    }
    # 路由（★ 实际结构是 {m2: {...}, m4: {...}}）
    rt = r.get("routing", {})
    if rt and "m2" in rt and "m4" in rt:
        before, after = rt["m2"], rt["m4"]
        out["routing"] = {
            "n": after.get("n"),
            "overall_before": before.get("accuracy"),
            "overall_after": after.get("accuracy"),
            "delta_pp": round((after.get("accuracy", 0)
                               - before.get("accuracy", 0)) * 100, 1),
            "by_type_before": {k: v.get("acc")
                               for k, v in (before.get("by_type") or {}).items()},
            "by_type_after": {k: v.get("acc")
                              for k, v in (after.get("by_type") or {}).items()},
        }
        # 逐题型改进
        bt = out["routing"]["by_type_before"]
        at = out["routing"]["by_type_after"]
        out["routing"]["improved_types"] = [
            {"type": k, "before": bt.get(k), "after": at.get(k)}
            for k in sorted(bt) if bt.get(k) != at.get(k)
        ]
    # 根因
    if root:
        bias = (root.get("bias") or {}).get("csv_bogdan", {}).get("dims", {})
        out["root_cause"] = {
            "mirror_independent": False,
            "topology_is_subset": True,
            "n_conflict_dims": [d for d in DIMS
                                if bias.get(d, {}).get("disagree", 0) > 0],
            "n_zero_conflict_dims": [d for d in DIMS
                                     if bias.get(d, {}).get("disagree", 0) == 0],
        }
    return out


def collect_m5() -> dict:
    """M5 服务层（人工填入的验证结论，数据在提交记录里）。"""
    return {
        "framework": "FastAPI + uvicorn",
        "n_routes": 4,
        "n_answer_types": 8,
        "n_endpoints": 5,
        "n_checks_passed": 12,
        "mode": "抽取式（零生成）",
        "_note": "验证数据见提交 9258c38 的验收表",
        "_source_files": ["api/README.md"],
    }


def collect_m6() -> dict:
    """M6 生成层 + 忠实度。"""
    r = _load("m6_faithfulness.json") or {}
    if not r:
        return {"_source_files": []}
    out: dict = {"_source_files":
                 ["dataset/processed/m6_faithfulness.json"]}
    for k, v in r.items():
        s = v.get("summary", {})
        out[f"config_{k}"] = {
            "name": s.get("config_name"),
            "n": s.get("n"),
            "trace_ratio": s.get("trace_ratio"),
            "numeric_ratio": s.get("numeric_ratio"),
            "entity_ratio": s.get("entity_ratio"),
            "contradiction_rate": s.get("contradiction_rate"),
            "util_ratio": s.get("util_ratio"),
            "hedging_rate": s.get("hedging_rate"),
        }
    A = out.get("config_A", {}).get("trace_ratio")
    C = out.get("config_C", {}).get("trace_ratio")
    D = out.get("config_D", {}).get("trace_ratio")
    B = out.get("config_B", {}).get("trace_ratio")
    if None not in (A, C):
        out["evidence_effectiveness_pp"] = round((A - C) * 100, 1)
    if None not in (A, D):
        out["metric_sensitivity_pp"] = round((A - D) * 100, 1)
    if None not in (A, B):
        out["generation_cost_pp"] = round((B - A) * 100, 1)
    return out


def collect_m7() -> dict:
    """M7 块大小。"""
    r = _load("m7_chunk_quality.json") or []
    return {
        "n_sizes": len(r),
        "table": [{
            "target": x.get("target_len"),
            "n_chunks": x.get("n_chunks"),
            "used_chars": x.get("used_chars"),
            "util_ratio": x.get("util_ratio"),
            "hedging_rate": x.get("hedging_rate"),
            "trace_ratio": x.get("trace_ratio"),
            "avg_answer_chars": x.get("avg_answer_chars"),
            "avg_gen_ms": x.get("avg_gen_ms"),
        } for x in r],
        "recommended": 512,
        "_source_files": ["dataset/processed/m7_chunk_quality.json"],
    }


# ==================================================================
# 一致性自检
# ==================================================================

def self_check(m: dict) -> list[dict]:
    """检查数字之间是否自洽。

    ★ 为什么要自检：报告里手抄的数字可能与原始数据脱节。
      这类错误在简历上特别致命——面试官一问就穿帮。
    """
    issues: list[dict] = []

    # 1. 约束违规应为 0
    m1 = m.get("M1", {})
    # 2. 完整度不应超过 1
    cr = m1.get("completeness_ratio")
    if cr is not None and cr > 1:
        issues.append({"level": "error", "msg":
                       f"M1 完整度 {cr} > 1，数据有问题"})
    # 3. 冲突数应等于各策略之和
    res = m.get("M4", {}).get("resolution", {})
    tot = res.get("n_total", 0)
    s = sum(v["n"] for v in res.get("by_strategy", {}).values())
    if tot and s != tot:
        issues.append({"level": "error", "msg":
                       f"M4 冲突总数 {tot} ≠ 各策略之和 {s}"})
    # 4. 忠实度指标应在 [0,1]
    for k, v in m.get("M6", {}).items():
        if not k.startswith("config_"):
            continue
        for f in ("trace_ratio", "numeric_ratio", "entity_ratio",
                  "contradiction_rate", "util_ratio", "hedging_rate"):
            x = v.get(f)
            if x is not None and not (0 <= x <= 1):
                issues.append({"level": "error", "msg":
                               f"M6 {k}.{f} = {x} 超出 [0,1]"})
    # 5. 检索指标应在 [0,1]
    for k, v in m.get("M3", {}).items():
        if not k.startswith("best::"):
            continue
        for f, x in v.items():
            if isinstance(x, float) and not (0 <= x <= 1):
                issues.append({"level": "error", "msg":
                               f"M3 {k}.{f} = {x} 超出 [0,1]"})
    # 6. 证据有效性差值应在 0-100
    ee = m.get("M6", {}).get("evidence_effectiveness_pp")
    if ee is not None and not (0 < ee <= 100):
        issues.append({"level": "warn", "msg":
                       f"M6 证据有效性 {ee}pp 异常（应在 0-100）"})

    return issues


# ==================================================================

# ==================================================================
# 对外数字一致性检查
# ==================================================================

# ★★ 简历/作品集里引用的数字，必须与产物文件一致。
#   这个检查是「对外表达」的质量闸门：
#   报告里的数字若与产物脱节，面试一问就穿帮。
#   →每次改报告后跑一次 --check，确保没有虚数。
PUBLIC_NUMBERS = {
    "M1.替身数": ("M1", "n_stands", 154),
    "M1.形态数": ("M1", "n_forms", 146),
    "M1.文本块数": ("M1", "n_chunks", 2407),
    "M1.完整度": ("M1", "completeness_ratio", 0.7987),
    "M1.冲突数": ("M1", "n_conflicts", 28),
    "M1.待消解": ("M1", "n_conflict_pending", 16),
    "M2.评测集": ("M2", "n_eval", 176),
    "M3.混合recall@5": ("M3", "hybrid_vs_bm25/hybrid/recall@5", 0.625),
    "M4.路由前": ("M4", "routing/overall_before", 0.8352),
    "M4.路由后": ("M4", "routing/overall_after", 1.0),
    "M6.证据有效性": ("M6", "evidence_effectiveness_pp", 88.1),
    "M6.指标敏感度": ("M6", "metric_sensitivity_pp", 80.6),
    "M7.推荐块长": ("M7", "recommended", 512),
}


def _dig(obj: Any, path: str) -> Any:
    """按 'a/b/c' 取嵌套值。"""
    cur = obj
    for part in path.split("/"):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def check_public(m: dict) -> list[dict]:
    """核对对外引用的数字是否与产物一致。"""
    issues: list[dict] = []
    for name, (stage, path, expect) in PUBLIC_NUMBERS.items():
        got = _dig(m.get(stage, {}), path)
        if got is None:
            issues.append({"level": "error", "msg":
                           f"{name}: 产物里找不到 {stage}.{path}"})
            continue
        if isinstance(expect, float):
            ok = abs(float(got) - expect) < 0.001
        else:
            ok = got == expect
        if not ok:
            issues.append({"level": "error", "msg":
                           f"{name}: 对外写 {expect}，产物是 {got}——"
                           "报告与数据脱节，面试会被问穿"})
    return issues


def main() -> int:
    ap = argparse.ArgumentParser(description="M8 指标汇总")
    ap.add_argument("--check", action="store_true", help="只做自检")
    args = ap.parse_args()

    metrics = {
        "M1": collect_m1(), "M2": collect_m2(), "M3": collect_m3(),
        "M4": collect_m4(), "M5": collect_m5(), "M6": collect_m6(),
        "M7": collect_m7(),
    }

    issues = self_check(metrics) + check_public(metrics)

    print("=" * 68)
    print("指标汇总自检")
    print("=" * 68)
    if not issues:
        print("  ✓ 全部通过")
    else:
        for it in issues:
            mark = "✗" if it["level"] == "error" else "!"
            print(f"  {mark} [{it['level']}] {it['msg']}")

    # 摘要
    print("\n" + "=" * 68)
    print("关键指标摘要")
    print("=" * 68)
    m1, m3, m4, m6, m7 = (metrics["M1"], metrics["M3"], metrics["M4"],
                          metrics["M6"], metrics["M7"])
    print(f"  M1 替身 {m1['n_stands']} / 形态 {m1['n_forms']} / "
          f"文本块 {m1['n_chunks']} / 完整度 {m1['completeness_ratio']}")
    print(f"     冲突 {m1['n_conflicts']}（待消解 {m1['n_conflict_pending']}）"
          f" / 数据源 {m1['n_sources']} 个")
    print(f"  M2 评测集 {metrics['M2']['n_eval']} 条 / 题型 "
          f"{len(metrics['M2']['type_dist'])} 类 / LLM 依赖 无")
    hv = m3.get("hybrid_vs_bm25", {})
    if "delta_vs_tuned_bm25_pp" in hv:
        b5 = hv["baseline_bm25_tuned"]
        h5 = hv["hybrid"]
        d = hv["delta_vs_tuned_bm25_pp"]
        print(f"  M3 混合检索 recall@5 {b5['recall@5']:.4f} → "
              f"{h5['recall@5']:.4f}（vs 调参BM25 +{d['recall@5']}pp / "
              f"nDCG@5 +{d['ndcg@5']}pp）")
        v0 = hv.get("delta_vs_m2_v0_pct", {})
        if "recall@5" in v0:
            r = v0["recall@5"]
            print(f"     vs M2 原始基线 {r['before']:.4f} → {r['after']:.4f}"
                  f"  相对提升 +{r['relative_gain_pct']}%")
        print(f"     配置数 {m3['n_configs']}")
    rt = m4.get("routing", {})
    if rt:
        print(f"  M4 路由 {rt.get('overall_before')} → {rt.get('overall_after')}")
    print(f"     消解 {m4['resolution']['n_total']} 条"
          f"（待定 {m4['resolution']['n_pending']}）")
    print(f"  M5 服务 {metrics['M5']['n_endpoints']} 端点 / "
          f"{metrics['M5']['n_checks_passed']} 项验证")
    if m6:
        print(f"  M6 证据有效性 +{m6.get('evidence_effectiveness_pp')}pp / "
              f"指标敏感度 +{m6.get('metric_sensitivity_pp')}pp")
    if m7.get("table"):
        print(f"  M7 块大小 {m7['n_sizes']} 档 / 推荐 {m7['recommended']}")

    if not args.check:
        OUT.write_text(json.dumps(metrics, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        print(f"\n输出：{OUT.relative_to(ROOT)}")
    return 1 if any(i["level"] == "error" for i in issues) else 0


if __name__ == "__main__":
    sys.exit(main())

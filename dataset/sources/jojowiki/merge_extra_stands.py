"""把 extra_stands.json（31 个第 7/8/9 部替身）合并进 stands / stand_stats。

★ 六维一律用 **NOT_APPLICABLE**，不是 EMPTY_SLOT
  - EMPTY_SLOT 的语义是「源里给了空位符号 ∅」（原数据里有这种情况）
  - 而这里的情况是「**官方压根没给这个替身评过级**」（第7部之后普遍如此）
  - CAT_CN 里两者都有，NOT_APPLICABLE = 「该源未提供」——语义才对得上
★ 绝不填 0：0 会被读成「能力为零」，比留空更糟。

用法：
    python dataset/sources/jojowiki/merge_extra_stands.py            # 预览
    python dataset/sources/jojowiki/merge_extra_stands.py --apply    # 实际写入
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PROC = ROOT / "dataset" / "processed"
HERE = Path(__file__).resolve().parent

EXTRA = HERE / "extra_stands.json"
STANDS = PROC / "stands.json"
STATS = PROC / "stand_stats.json"

DIMS = ("pwr", "spd", "rng", "sta", "prc", "dev")
WIKI2DIM = {"destpower": "pwr", "speed": "spd", "range": "rng",
            "stamina": "sta", "precision": "prc", "potential": "dev"}


def to_stand_record(rec: dict) -> dict:
    """转成 stands.json 的既有形状。"""
    sid = rec["stand_id"]
    return {
        "stand_id": sid,
        "name_en": rec["name_en"],
        "name_raw": rec.get("title") or rec["name_en"],
        "part": rec.get("part"),
        "part_name_en": rec.get("part_name_en"),
        "owner_id": None,          # 既有数据里全为 None，保持一致
        "owner_name": rec.get("owner_name"),
        "stand_type": rec.get("stand_type"),
        "reference": rec.get("reference"),
        "name_ja": rec.get("name_ja"),
        "name_romaji": rec.get("name_romaji"),
        "manga_debut": rec.get("manga_debut"),
        "anime_debut": rec.get("anime_debut"),
        "form_count": 1,
        "form_chain": [f"{sid}__f0"],
        "main_table_registrations": 0,   # ★ 不在 Stand_Stats 主表里（正是缺失原因）
        "data_sources": ["jojowiki_article"],
        "detail_url": "https://jojowiki.com/"
                      + (rec.get("title") or rec["name_en"]).replace(" ", "_"),
    }


def to_stats_record(rec: dict) -> dict:
    """生成 stand_stats 行。六维全 None + NOT_APPLICABLE。"""
    row: dict = {
        "stand_id": rec["stand_id"],
        "composite": None,
        "missing_count": 6,
        "has_partial": 1,
    }
    for w, d in WIKI2DIM.items():
        v = (rec.get("stats") or {}).get(w)
        row[d] = None
        row[f"{d}_cat"] = "NOT_APPLICABLE" if v is None else "RANKED"
        row[f"{d}_note"] = None
        row[f"{d}_raw"] = None if v is None else str(v)
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="实际写入（否则只预览）")
    args = ap.parse_args()

    extra = json.loads(EXTRA.read_text(encoding="utf-8"))
    stands = json.loads(STANDS.read_text(encoding="utf-8"))
    stats = json.loads(STATS.read_text(encoding="utf-8"))

    have = {s["stand_id"] for s in stands}
    new = [r for r in extra if r["stand_id"] not in have]
    print(f"extra {len(extra)} 条 → 其中确属新增 {len(new)} 条")
    if len(new) != len(extra):
        print("  ⚠ 有条目与现有 stand_id 冲突，已跳过")

    new_stands = [to_stand_record(r) for r in new]
    new_stats = [to_stats_record(r) for r in new]

    # 校验：六维必须全空（防止上游填了 0 之类的假值）
    bad = [r["stand_id"] for r in new_stats
           if any(r[d] is not None for d in DIMS)]
    if bad:
        print(f"  ✗ 以下记录的六维非空，拒绝写入：{bad}")
        return 1
    zeros = [r["stand_id"] for r in new_stats
             if any(r[d] == 0 for d in DIMS)]
    if zeros:
        print(f"  ✗ 出现 0 值（会被读成「能力为零」）：{zeros}")
        return 1
    print("  ✓ 六维全部为 None（未推测为 0）")

    print(f"\nstands:      {len(stands)} → {len(stands) + len(new_stands)}")
    print(f"stand_stats: {len(stats)} → {len(stats) + len(new_stats)}")
    if not args.apply:
        print("\n（预览模式，未写入。加 --apply 生效）")
        for r in new_stands:
            print(f"  {r['stand_id']:28s} 第{r['part']}部  "
                  f"{r['name_en']}")
        return 0

    # 备份后写入
    for p in (STANDS, STATS):
        bak = p.with_suffix(p.suffix + ".bak")
        shutil.copy2(p, bak)
        print(f"  备份 → {bak.name}")
    STANDS.write_text(json.dumps(stands + new_stands,
                                 ensure_ascii=False, indent=1),
                      encoding="utf-8")
    STATS.write_text(json.dumps(stats + new_stats,
                                ensure_ascii=False, indent=1),
                     encoding="utf-8")
    print("\n✓ 已写入")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""
把 M4 的消解结果写回数据库。

设计要点：
  - 消解结果**不覆盖原始冲突记录**，而是更新 resolution / resolved_* 字段
  - 同时写一张消解日志表，记录每次消解的时间与依据（可追溯）
  - 人工消解优先：若 resolved_by 已非空（说明人工介入过），跳过

用法：
    python apply_resolutions.py             # 应用到数据库
    python apply_resolutions.py --dry-run   # 只打印不写库
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services"))
sys.path.insert(0, str(ROOT / "database"))

from conflict_resolver import ConflictResolver  # noqa: E402
from load_db import connect  # noqa: E402

PROC = ROOT / "dataset" / "processed"

# 消解日志表（幂等创建）
LOG_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS resolution_log (
    log_id      BIGSERIAL PRIMARY KEY,
    logged_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    stand_id    TEXT NOT NULL,
    stand_name  TEXT,
    stat_dim    TEXT NOT NULL,
    strategy    TEXT NOT NULL,
    prev_resolution    TEXT,
    new_resolution    TEXT,
    prev_value        SMALLINT,
    new_value         SMALLINT,
    confidence        REAL,
    sensitivity      TEXT,
    rationale        TEXT,
    applied_by       TEXT NOT NULL DEFAULT 'm4_resolver'
);

CREATE INDEX IF NOT EXISTS idx_reslog_stand ON resolution_log(stand_id);
CREATE INDEX IF NOT EXISTS idx_reslog_time  ON resolution_log(logged_at DESC);
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--strategy", default="consensus",
                    choices=["consensus", "primary"],
                    help="consensus=启用镜像源共识（默认）")
    args = ap.parse_args()

    conflicts = json.loads((PROC / "conflicts.json").read_text(encoding="utf-8"))
    stands = json.loads((PROC / "stands.json").read_text(encoding="utf-8"))
    sname = {s["stand_id"]: s["name_en"] for s in stands}

    resolver = ConflictResolver(use_consensus=(args.strategy == "consensus"))
    resolutions = resolver.resolve_all(conflicts, stands)
    print(f"消解 {len(resolutions)} 条冲突"
          f"（策略：{'含共识' if args.strategy == 'consensus' else '仅主源'}）")
    # ★ 打印镜像源独立性 —— 共识策略的前提
    if resolver.mirror_evidence:
        ev = resolver.mirror_evidence
        print(f"  镜像源独立性: {ev.get('verdict')}")
        print(f"    topology ⊆ bogdan: {ev.get('topology_is_subset')}"
              f"  真正的等级分歧: {ev.get('real_value_conflicts')} 处")
        if not resolver.mirror_independent:
            print(f"    ★ 共识策略自动失效，降级为prefer_primary")

    if args.dry_run:
        print("\n--dry-run，不写库。消解结果预览：\n")
        for r in resolutions[:12]:
            print(f"  {r.stand_name[:20]:22s} {r.stat_dim}  "
                  f"{str(r.value_a)[:14]:16s} vs {str(r.value_b)[:10]:12s} → "
                  f"{str(r.final_raw)[:10]:12s} [{r.strategy}]")
        print(f"\n  ... 共 {len(resolutions)} 条")
        return 0

    conn = connect()
    cur = conn.cursor()

    # 建日志表
    cur.execute(LOG_TABLE_SQL)
    conn.commit()
    print("  [OK] resolution_log 表就绪")

    # 应用消解
    updated = 0
    skipped_manual = 0
    for r in resolutions:
        # 跳过人工已消解的
        cur.execute("""
            SELECT resolved_by FROM stat_conflicts
            WHERE stand_id=%s AND stat_dim=%s AND value_a IS NOT DISTINCT FROM %s
              AND source_a=%s AND value_b IS NOT DISTINCT FROM %s AND source_b=%s
        """, (r.stand_id, r.stat_dim, r.value_a, r.source_a,
              r.value_b, r.source_b))
        row = cur.fetchone()
        if row and row[0] and row[0] != "m4_resolver":
            skipped_manual += 1
            continue

        cur.execute("""
            UPDATE stat_conflicts
            SET resolution=%s, resolved_value=%s, resolved_cat=%s,
                note=%s
            WHERE stand_id=%s AND stat_dim=%s
              AND value_a IS NOT DISTINCT FROM %s AND source_a=%s
              AND value_b IS NOT DISTINCT FROM %s AND source_b=%s
        """, (r.strategy, r.final_value,
              r.final_category,          # 详细依据写进 note
              r.rationale,
              r.stand_id, r.stat_dim, r.value_a, r.source_a,
              r.value_b, r.source_b))
        if cur.rowcount:
            updated += cur.rowcount
            # 写日志
            cur.execute("""
                INSERT INTO resolution_log
                    (stand_id, stand_name, stat_dim, strategy,
                     new_resolution, new_value, confidence, sensitivity, rationale)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (r.stand_id, r.stand_name, r.stat_dim, r.strategy,
                  r.strategy, r.final_value, r.confidence,
                  r.sensitivity, r.rationale))
    conn.commit()
    print(f"  更新 {updated} 条冲突消解结果")
    if skipped_manual:
        print(f"  跳过 {skipped_manual} 条（人工已消解，不覆盖）")

    # 验证
    cur.execute("""
        SELECT resolution, count(*),
               count(resolved_value) AS 已定值
        FROM stat_conflicts GROUP BY 1 ORDER BY 2 DESC
    """)
    print("\n  消解状态：")
    for res, n, done in cur.fetchall():
        print(f"    {str(res):20s} {n:3d} 条（已定值 {done}）")

    cur.execute("SELECT count(*) FROM v_conflicts_pending")
    print(f"\n  剩余待消解：{cur.fetchone()[0]} 条")
    print("  ★ 说明：keep_unknown 是**刻意的保守结论**（主源标?，不臆测），"
          "不是未处理")

    cur.execute("SELECT count(*) FROM resolution_log")
    print(f"  消解日志：{cur.fetchone()[0]} 条")

    cur.close()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

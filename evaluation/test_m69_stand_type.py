"""M69：替身类型串结构化（stand_type 从「一坨粘串」到可查询的三段）

背景（M67 报告 §8 记的遗留项）：
  `stands.stand_type` 是 Infobox 多项**直接拼接**来的，
  实测形如 `"Close-RangeRange IrrelevantNatural HumanoidSentientEvolved"`。
  M32 在**展示层**清洗过一次，但：
    ① 词表只有 15 词，漏掉语料真实出现的 Natural Non-Humanoid /
       Psychological Assault / Sentient / Multi-Unit / Multi-User /
       Foresight / Evolved；
    ② `Natural Humanoid` 是 `Natural Non-Humanoid` 的**子串** → 先命中短的会切错；
    ③ 清洗只在输出时做，底层值仍是粘的 → 按类型过滤/统计做不了。

★ 本轮与 M67 当初放弃这件事的区别：
  M67 的理由是 `{{ST}}` 是缩写模板、**无法从 wikitext 可靠展开**。
  本轮**不做展开**：只切分 `stands.json` 里已经展开好的值，
  词表由实测反推。判据是「残余为 0」——每个字符都能被某个已知词解释，
  没有猜测成分。

跑法：python evaluation/test_m69_stand_type.py
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dataset" / "pipeline"))

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          + (f"   {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    print("=" * 74)
    print("M69：替身类型串结构化（stand_type）")
    print("=" * 74)

    import stand_type_parts as stp

    # ---- ① 纯函数：子串歧义是本轮的核心风险 ----
    print("\n[1] 切分的核心风险：`Humanoid` 子串歧义")
    p = stp.split_stand_type("Natural Non-HumanoidMaterialized")
    check("Natural Non-Humanoid 不被切成 Natural Humanoid",
          p.kinds == ("Natural Non-Humanoid",), f"实得 {p.kinds}")
    check("★ 残余为空", p.residual == "", f"残余={p.residual!r}")
    check("中文译名对应「自然非人型」而非「自然人型」",
          p.readable_cn().startswith("自然非人型"),
          f"实得 {p.readable_cn()}")

    p2 = stp.split_stand_type("Close-RangeRange IrrelevantNatural Humanoid")
    check("多段串能全切出（按源串顺序）",
          p2.all_en() == ["Close-Range", "Range Irrelevant", "Natural Humanoid"],
          f"实得 {p2.all_en()}")
    check("★ 两个矛盾射程都记录、主射程取第一个",
          p2.ranges == ("Close-Range", "Range Irrelevant")
          and p2.range_type == "Close-Range", f"实得 {p2.ranges}")
    check("★ Range Irrelevant 不被误当特性段（它是射程类词）",
          "Range Irrelevant" not in p2.extras, f"extras={p2.extras}")
    check("★ 三段互斥且完备（无重复计入、无遗漏）",
          len(p2.all_en()) == len(set(p2.all_en()))
          == len(p2.ranges) + len(p2.kinds) + len(p2.extras),
          f"{p2.all_en()}")

    # 源站确实存在"同一替身两段体质"，必须如实记录而不是二选一
    p5 = stp.split_stand_type(
        "AutomaticNatural Non-Humanoid (Post-Mortem)Natural Humanoid (Original)")
    check("★ 同替身两段体质如实保留（不替源站二选一）",
          p5.kinds == ("Natural Non-Humanoid", "Natural Humanoid"),
          f"实得 {p5.kinds}")

    # ---- ② 边界值 ----
    print("\n[2] 边界值不崩、不猜")
    check("None → 全空", stp.split_stand_type(None).all_en() == [])
    check("空串 → 全空", stp.split_stand_type("").all_en() == [])
    check("readable_cn(None) 返回 None", stp.readable_cn(None) is None)
    check("★ 完全无法识别时不返回原文（不瞎掰）",
          stp.readable_cn("Zzz Unknown Word") is None,
          f"实得 {stp.readable_cn('Zzz Unknown Word')!r}")
    p3 = stp.split_stand_type("Zzz Unknown Word")
    check("★ 无法识别时残余如实保留（可被审计发现）",
          p3.residual != "" and not p3.complete, f"残余={p3.residual!r}")

    # 括注是自由文本，不入类型
    p4 = stp.split_stand_type("Range Irrelevant (Other)Phenomenon")
    check("括注被剥离、不进类型表", p4.all_en() == ["Range Irrelevant", "Phenomenon"],
          f"实得 {p4.all_en()}")

    # 聚合标记是 load_db 合成的父子标记，不是真实类型
    check("★ Aggregate (form group) 被识别为聚合标记",
          stp.is_aggregate("Aggregate (form group)"))
    check("聚合标记不参与切分（否则会当成残余）",
          stp.is_aggregate("Aggregate (form group)")
          and stp.split_stand_type("Aggregate (form group)").residual == "Aggregate")

    # ---- ③ 全量语料审计：残余必须为 0 ----
    print("\n[3] 全量语料审计（硬判据：残余为 0）")
    stands = json.load(open(ROOT / "dataset" / "processed" / "stands.json",
                            encoding="utf-8"))
    aud = stp.audit(stands)
    check("stands.json 里有可切分的类型值", aud["n_with_type"] > 100,
          f"n={aud['n_with_type']}")
    check("★ 残余为 0（词表能解释每一个字符）",
          aud["n_incomplete"] == 0,
          f"残余 {aud['n_incomplete']} 条：" +
          str([(r['name_en'], r['raw']) for r in aud['incomplete'][:5]]))
    check("每条都有可读中文串",
          all(r["readable"] for r in aud["rows"]),
          str([r["name_en"] for r in aud["rows"] if not r['readable']][:5]))

    # ---- ④ migration 004 存在且带审计字段 ----
    print("\n[4] migration 004 的字段设计")
    mig = ROOT / "dataset" / "schema" / "migrations" / "004_stand_type_parts.sql"
    check("migration 存在", mig.exists())
    if mig.exists():
        sql = mig.read_text(encoding="utf-8")
        for col in ("stand_type_range", "stand_type_ranges",
                    "stand_type_kinds", "stand_type_extras",
                    "stand_type_residual", "stand_type_aggregate"):
            check(f"★ 声明了 {col}", col in sql)
        check("★ 保留原 stand_type 列（原文可回溯，不删）",
              "ADD COLUMN IF NOT EXISTS stand_type_range" in sql
              and "DROP COLUMN" not in sql)
        check("索引覆盖了射程与数组列",
              "idx_stands_type_range" in sql and "GIN" in sql)

    # ---- ⑤ 静态检查：load_db 与 api 共用同一份词表 ----
    print("\n[5] 展示层与数据层同源（两份词表各切各的 = 隐蔽的不一致）")
    api_src = (ROOT / "api" / "main.py").read_text(encoding="utf-8")
    check("api/main.py 引用共享词表模块",
          "stand_type_parts" in api_src)
    check("★ 旧的本地球表只作为 fallback 保留",
          "except" in api_src and "_st_readable_cn" in api_src)

    db_src = (ROOT / "database" / "load_db.py").read_text(encoding="utf-8")
    check("load_db.py 引用同一模块",
          "stand_type_parts" in db_src)
    check("★ 派生列缺失时不阻断装载（老 schema 向后兼容）",
          "_has_type_cols" in db_src and "跳过 M69 派生列" in db_src)

    # 语法层面：新增的 executemany 确实带上了新列
    tree = ast.parse(db_src)
    ins = [n for n in ast.walk(tree)
           if isinstance(n, ast.Constant) and isinstance(n.value, str)
           and "stand_type_range" in n.value and "INSERT INTO stands" in n.value]
    check("★ INSERT 语句包含 M69 的新列",
          bool(ins) and all(c in ins[0].value for c in (
              "stand_type_range", "stand_type_ranges", "stand_type_kinds",
              "stand_type_extras", "stand_type_residual",
              "stand_type_aggregate", "stand_type_cn")),
          str([getattr(i, 'value', '')[:80] for i in ins]))
    check("★ 数组列用 string_to_array 还原（psycopg2 裸数组字面量不可用）",
          bool(ins) and "string_to_array" in ins[0].value)

    # ---- ⑥ DB 实况（有库才跑；无库跳过而不是报错） ----
    print("\n[6] DB 实况")
    try:
        sys.path.insert(0, str(ROOT / "database"))
        from load_db import connect
        conn = connect()
        cur = conn.cursor()
    except BaseException as e:            # connect() 失败会 SystemExit(1)
        print(f"  SKIP  数据库不可用（{type(e).__name__}）")
        conn = None

    if conn is not None:
        cur.execute("""
            SELECT count(*) FILTER (WHERE stand_type_residual <> ''),
                   count(*) FILTER (WHERE stand_type_aggregate),
                   count(*) FILTER (WHERE stand_type_cn IS NOT NULL),
                   count(*) FILTER (
                       WHERE stand_type IS NOT NULL AND stand_type <> '')
            FROM stands
        """)
        n_res, n_agg, n_cn, n_type = cur.fetchone()
        check("DB 里类型值全部可切分（残余 0）", n_res == 0, f"残余 {n_res}")
        check("★ 聚合标记被单独标记出来", n_agg >= 1, f"n={n_agg}")
        check("非空类型都有中文串", n_cn == n_type - n_agg,
              f"cn={n_cn} type={n_type} agg={n_agg}")

        # ★ 判据修正过一次：我原本断言「不可能同时命中人型与非人型」，
        #   DB 跑出来 Aqua Necklace / Notorious B.I.G 两条真同时命中 ——
        #   查原始串确认**源站就是两段**（原体人型、死后非人型）。
        #   → 那不是错切，数组列如实记录才对。真正要防的是
        #     「把 Natural Non-Humanoid 切成 Natural Humanoid」这种子串歧义，
        #     那是纯函数层的事（见 [1]），这里查的是它的 DB 落地。
        cur.execute("""
            SELECT count(*) FROM stands
            WHERE stand_type LIKE '%%Non-Humanoid%%'
              AND cardinality(stand_type_kinds) > 0
              AND NOT ('Natural Non-Humanoid' = ANY(stand_type_kinds)
                       OR 'Artificial Non-Humanoid' = ANY(stand_type_kinds))
        """)
        check("★ 含 Non-Humanoid 的串必切出非人型（子串歧义未复发）",
              cur.fetchone()[0] == 0)

        cur.execute("""
            SELECT count(*) FROM stands
            WHERE stand_type_range IS DISTINCT FROM stand_type_ranges[1]
        """)
        check("★ 主射程 == 射程数组的第一个元素", cur.fetchone()[0] == 0)

        # 结构化后终于能做类型检索
        cur.execute("""
            SELECT count(*) FROM stands
            WHERE stand_type_range = 'Close-Range'
              AND 'Natural Humanoid' = ANY(stand_type_kinds)
        """)
        n_close = cur.fetchone()[0]
        check("★ 可按「近距离型 + 自然人型」检索（旧结构做不到）",
              n_close > 0, f"n={n_close}")

        cur.close()
        conn.close()

    print("\n" + "=" * 74)
    if FAILS:
        print(f"✗ {len(FAILS)} 项未通过：")
        for f in FAILS:
            print(f"    - {f}")
        return 1
    print("✓ M69 全部检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())

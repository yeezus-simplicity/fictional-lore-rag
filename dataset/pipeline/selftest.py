"""
编码层与校验层的自测。

数据源：`docs/字段映射/jojowiki_stand_stats_raw.html`（真实快照，157 条 × 6 维 = 942 格子）
运行：cd dataset/pipeline && python selftest.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from lxml import html as LH

from encode import Category, encode_row, encode_stat, make_stand_id
from validate import (
    collect_source_duplicates,
    find_exact_duplicates,
    validate_all,
    validate_columns,
    validate_row,
)

SNAPSHOT = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "字段映射"
    / "jojowiki_stand_stats_raw.html"
)

# ------------------------------------------------------------------
# 单元测试：判定表九步全覆盖
# ------------------------------------------------------------------

CASES = [
    # (原始字面量, 期望数值, 期望类别, 期望说明)
    # 规则 4：正常等级
    ("A", 5, Category.RANKED, None),
    ("E", 1, Category.RANKED, None),
    # 规则 5：明确无能力
    ("None", 0, Category.NONE, None),
    ("none", 0, Category.NONE, None),
    # 规则 6：空位（多源字面量）
    ("∅", None, Category.EMPTY_SLOT, None),
    ("undefined", None, Category.EMPTY_SLOT, None),
    ("N/A", None, Category.EMPTY_SLOT, None),
    # 规则 7：未知（独立语义，不并入空位）
    ("?", None, Category.UNKNOWN, None),
    ("unknown", None, Category.UNKNOWN, None),
    # 规则 8：无限
    ("∞", None, Category.INFINITE, None),
    ("Infi", None, Category.INFINITE, None),
    # 规则 8b：随情境变化（镜像源表达条件值的方式）
    ("situational", None, Category.CONDITIONAL_NO_BASE, "varies by context"),
    # 空字符串 = 该源未提供，不是「值为空」
    ("", None, Category.NOT_APPLICABLE, None),
    # 规则 1：条件值有基础等级（括号必须完整保留）
    ("B※20-30 meters", 4, Category.CONDITIONAL, "20-30 meters"),
    ("C※Range: 2 m (6.6 ft)", 3, Category.CONDITIONAL, "Range: 2 m (6.6 ft)"),
    ('D※Speed: Refers to "Melt your Heart" Ability (JoJoveller p.245)', 2,
     Category.CONDITIONAL, 'Speed: Refers to "Melt your Heart" Ability (JoJoveller p.245)'),
    ("A※Only during replays", 5, Category.CONDITIONAL, "Only during replays"),
    # 规则 2：特殊值带说明
    ("?※Power: Likely A (JoJoveller p.245)", None, Category.CONDITIONAL_NO_BASE,
     "Power: Likely A (JoJoveller p.245)"),
    # 规则 3：无基础等级
    ("※Complete", None, Category.CONDITIONAL_NO_BASE, "Complete"),
    ("※Depending on Education", None, Category.CONDITIONAL_NO_BASE, "Depending on Education"),
    # 兜底
    ("A A A A A A", None, Category.UNPARSED, "A A A A A A"),
]


def test_cases() -> bool:
    print("=" * 70)
    print("单元测试：判定表九步")
    print("=" * 70)
    passed = failed = 0
    for raw, exp_val, exp_cat, exp_note in CASES:
        got = encode_stat(raw)
        ok = got.value == exp_val and got.category == exp_cat and (got.note or None) == exp_note
        flag = "PASS" if ok else "FAIL"
        if ok:
            passed += 1
        else:
            failed += 1
            print(f"  [{flag}] {raw!r}")
            print(f"期望: value={exp_val} cat={exp_cat.value} note={exp_note!r}")
            print(f"实际: value={got.value} cat={got.category.value} note={got.note!r}")
    print(f"\n通过 {passed} /失败 {failed}")
    return failed == 0


def test_id() -> bool:
    print("\n" + "=" * 70)
    print("单元测试：ID 规范化")
    print("=" * 70)
    cases = [
        ("Star Platinum", "star_platinum"),
        ("Foo Fighters\"", "foo_fighters"),# 尾随引号
        ("Echoes (ACT3)", "echoes_act3"),        # 括号
        ("Weather Report\"", "weather_report"),
        ("C-MOON", "c_moon"),
    ]
    ok_all = True
    for raw, exp in cases:
        got = make_stand_id(raw)
        ok = got == exp
        ok_all &= ok
        print(f"  [{'PASS' if ok else 'FAIL'}] {raw!r} -> {got!r}"
              + ("" if ok else f"  期望 {exp!r}"))
    return ok_all


def test_composite() -> bool:
    print("\n" + "=" * 70)
    print("单元测试：composite 禁止取平均")
    print("=" * 70)
    # 6 维齐全 → 有 composite。A=5 A=5 C=3 A=5 A=5 A=5 = 28
    full = encode_row("Star Platinum", ["A", "A", "C", "A", "A", "A"])
    ok1 = full.composite == 28 and full.missing_count == 0
    print(f"  [{'PASS' if ok1 else 'FAIL'}] 齐全行 composite={full.composite} (期望 28)")

    # 缺一维 → composite 必须为 None，绝不能取平均。
    # Whitesnake 真实值：?※ / D※ / ? / A / ? / ? → 4 个 NULL
    # （注：?※ 是 CONDITIONAL_NO_BASE，也算无值）
    partial = encode_row(
        "Whitesnake",
        ["?※Power: Likely A (p.245)", 'D※Speed: "Melt your Heart"', "?", "A", "?", "?"],
    )
    ok2 = partial.composite is None and partial.missing_count == 4
    print(f"  [{'PASS' if ok2 else 'FAIL'}] 残缺行 composite={partial.composite} "
          f"(期望 None) missing_count={partial.missing_count} (期望 4)")

    # 极端情况：6 维全 NULL → composite 必须 None，不能崩溃
    empty = encode_row("Unknown", ["?", "?", "?", "?", "?", "?"])
    ok3 = empty.composite is None
    print(f"  [{'PASS' if ok3 else 'FAIL'}] 全缺失行 composite={empty.composite} (期望 None)")

    return ok1 and ok2 and ok3


# ------------------------------------------------------------------
# 集成测试：真实快照全量跑
# ------------------------------------------------------------------

def load_snapshot() -> list[list[str]]:
    if not SNAPSHOT.exists():
        print(f"!! 快照不存在：{SNAPSHOT}")
        return []
    root = LH.parse(str(SNAPSHOT)).getroot()
    table = root.xpath("//table")[0]
    rows = []
    for tr in table.xpath(".//tr")[1:]:
        cells = [c.text_content().strip().replace("\n", " ")
                 for c in tr.xpath("./th|./td")]
        if len(cells) == 7:
            rows.append(cells)
    return rows


def test_real_data() -> bool:
    print("\n" + "=" * 70)
    print("集成测试：真实快照 157 条 × 6 维 = 942 格子")
    print("=" * 70)
    raw_rows = load_snapshot()
    if not raw_rows:
        return False
    print(f"  载入 {len(raw_rows)} 条")

    rows = []
    for cells in raw_rows:
        try:
            rows.append(encode_row(cells[0], cells[1:]))
        except ValueError as e:
            print(f"  !! 编码失败：{cells[0]!r} {e}")
            return False

    # 类别分布
    from collections import Counter
    cnt: Counter = Counter()
    unparsed = []
    for r in rows:
        for dim, st in r.stats.items():
            cnt[st.category.value] += 1
            if st.category == Category.UNPARSED:
                unparsed.append((r.name_raw, dim, st.raw))

    print(f"\n  类别分布（共{sum(cnt.values())} 格子）：")
    for cat, n in cnt.most_common():
        print(f"    {cat:22s} {n:4d}  ({n / sum(cnt.values()) * 100:5.1f}%)")

    # 关键验收：UNPARSED 必须为 0（判定表覆盖全部取值）
    ok_unparsed = len(unparsed) == 0
    print(f"\n  [{'PASS' if ok_unparsed else 'FAIL'}] UNPARSED 数量 = {len(unparsed)}（要求 0）")
    for name, dim, raw in unparsed[:10]:
        print(f"      {name}:{dim} raw={raw!r}")

    # 统计异常值总数
    anomalous = sum(n for c, n in cnt.items()
                    if c not in ("RANKED", "NONE"))
    print(f"\n  异常值总数 = {anomalous} / {sum(cnt.values())} "
          f"({anomalous / sum(cnt.values()) * 100:.1f}%)")

    # composite 覆盖率
    full = sum(1 for r in rows if r.composite is not None)
    print(f"  composite可计算行 = {full} / {len(rows)} ({full / len(rows) * 100:.1f}%)")

    # 源内重复登记（形态差异，非错误）
    dupes = collect_source_duplicates(rows)
    print(f"\n  源内重复登记（DUPLICATE_IN_SOURCE）= {len(dupes)} 维度冲突")
    for sid, dim, a, b, ct in dupes[:6]:
        print(f"      {sid}:{dim}  {a!r} vs {b!r}")

    exact = find_exact_duplicates(rows)
    print(f"  完全重复（需去重）= {len(exact)} 组")

    return ok_unparsed


def test_validation() -> bool:
    print("\n" + "=" * 70)
    print("集成测试：V1–V11 校验器")
    print("=" * 70)
    raw_rows = load_snapshot()
    if not raw_rows:
        return False
    rows = [encode_row(c[0], c[1:]) for c in raw_rows]

    rep = validate_all(rows)
    print(f"  {rep.summary()}")
    for e in rep.errors[:15]:
        print(f"    {e}")
    for w in rep.warnings[:5]:
        print(f"    {w}")

    # 无 ERROR 即通过（源内重复是 WARN 而非 ERROR —— 它是真实的形态差异）
    ok = rep.ok
    print(f"  [{'PASS' if ok else 'FAIL'}] 全部 {len(rows)} 条通过 V1-V11（无 ERROR）")

    # V9：列名白名单
    print("\n  V9 列名白名单测试：")
    for cols, expect_ok in [
        (["Stand", "PWR", "SPD", "RNG", "STA", "PRC", "DEV"], True),
        (["Stand", "PWR", "SPD", "RNG", "PER", "PRC", "DEV"], True),   #已知别名 → WARN
        (["Stand", "PWR", "FOO", "RNG", "STA", "PRC", "DEV"], False),  # 未知列 → ERROR
    ]:
        r = validate_columns(cols, "test")
        good = r.ok == expect_ok
        print(f"    [{'PASS' if good else 'FAIL'}] {cols[3]:5s} "
              f"ok={r.ok} (期望 {expect_ok}) {r.summary()}")

    return ok


# ------------------------------------------------------------------

def main() -> int:
    print("\n" + "=" * 70)
    print("rag-kb M1编码层自测")
    print("=" * 70)

    results = {
        "判定表九步": test_cases(),
        "ID 规范化": test_id(),
        "composite 约束": test_composite(),
        "真实快照集成": test_real_data(),
        "V1-V11 校验": test_validation(),
    }

    print("\n" + "=" * 70)
    print("汇总")
    print("=" * 70)
    for k, v in results.items():
        print(f"  [{'PASS' if v else 'FAIL'}] {k}")
    all_ok = all(results.values())
    print(f"\n总结果: {'全部通过' if all_ok else '存在失败'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())

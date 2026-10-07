"""
模糊匹配的**误匹配率评测**（M15）。

★★ 为什么先评测再启用 ★★
用户要求「要模糊匹配，但必须先评测误匹配率」。
这不是流程形式 —— M14 刚踩过「单字中文名抢匹配」的坑：
  Strength 的中文名「力」抢走了「黄金体验的能力」。
模糊匹配本质上是**放宽匹配**，风险只会更高。

==评测设计 ==
  正例：用户输入是某个中文名的**片段**（如「黄金」→ 黄金体验）
        期望命中正确
  负例：用户输入是**别的东西**（其他替身名的一部分、六维词、疑问词）
        期望**不命中**

  指标 = 负例中被误匹配的比例（越低越好）
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services"))

from aliases import build_alias_table  # noqa: E402

PROC = ROOT / "dataset" / "processed"


def main() -> int:
    alias2id, id2alias = build_alias_table()
    stands = json.loads((PROC / "stands.json").read_text(encoding="utf-8"))
    en2id = {s["name_en"]: s["stand_id"] for s in stands}

    #★ 候选集：所有中文名（≥2 字）
    zh_aliases: list[tuple[str, str]] = []
    for sid, names in id2alias.items():
        for n in names:
            if re.search(r"[\u4e00-\u9fff]", n) and len(n) >= 2:
                zh_aliases.append((n, sid))
    print("=" * 70)
    print("模糊匹配误匹配率评测（M15）")
    print("=" * 70)
    print(f"\n中文别名候选：{len(zh_aliases)} 个"
          f"（覆盖 {len({sid for _, sid in zh_aliases})} 个替身）")

    # ---------- 正例：片段应命中 ----------
    print("\n[1] 正例 —— 输入是中文名的片段")
    pos_cases: list[tuple[str, str, str]] = []
    for name, sid in zh_aliases:
        if len(name) >= 3:
            # 取前半段（模拟用户只输入几个字）
            for cut in (2, len(name) - 1):
                frag = name[:cut]
                if len(frag) >= 2 and frag != name:
                    pos_cases.append((frag, sid, name))
    # 只取一部分，避免输出过长
    rng_pos = pos_cases[::max(1, len(pos_cases) // 25)][:25]

    # ---------- 负例：其他东西不该命中 ----------
    print("\n[2] 负例 —— 与替身无关的输入")
    neg_cases = [
        # 六维维度词（M14 踩过「力」抢「能力」的坑）
        "破坏力", "速度", "射程", "持续力", "精密性", "成长性",
        "能力", "形态", "外观", "描述", "招式", "历史",
        # 疑问词
        "什么", "是谁", "多少", "最高", "每个", "所有",
        # 无关话题
        "今天天气", "讲个笑话", "写代码", "吃了吗", "多少钱",
        # 其他替身名的一部分（★ 危险：可能误匹配到别的替身）
        "世界", "钻石", "手", "力量", "太阳", "皇帝",
        "门", "锁", "鸟", "猫", "车",
    ]

    # ---------- 评测不同阈值 ----------
    print("\n[3] 阈值扫描（匹配长度下限）")
    print(f"  {'min_len':>8s}{'正例命中':>12s}{'负例误匹配':>14s}  判定")
    print("  " + "-" * 52)

    results = {}
    for min_len in (2, 3, 4):
        def match(q: str) -> list[str]:
            """模拟将来要用的模糊匹配：子串双向包含。"""
            hits = set()
            for name, sid in zh_aliases:
                if len(name) < min_len:
                    continue
                # 用户输入是别名的一部分，或别名是输入的一部分
                if name in q or (len(q) >= min_len and q in name):
                    hits.add(sid)
            return list(hits)

        # 正例
        ok = sum(1 for frag, sid, _ in rng_pos if sid in match(frag))
        # 负例
        bad = [q for q in neg_cases if match(q)]
        bad_rate = len(bad) / len(neg_cases)
        ok_rate = ok / len(rng_pos) if rng_pos else 0

        if bad_rate == 0:
            verdict = "✓ 可用"
        elif bad_rate <= 0.15:
            verdict = "△ 勉强（需限定使用场景）"
        else:
            verdict = "✗ 不可用"
        results[min_len] = (ok_rate, bad_rate, bad)
        print(f"  {min_len:>8d}{ok_rate:>11.1%}{bad_rate:>13.1%}  {verdict}")

    # ---------- 详细看 min_len=3 的负例误匹配 ----------
    print("\n[4] min_len=3 时被误匹配的负例（★ 需要人工确认能否接受）")
    _, _, bad3 = results.get(3, (0, 0, []))
    if bad3:
        for q in bad3:
            hits = []
            for name, sid in zh_aliases:
                if len(name) >= 3 and (name in q or q in name):
                    hits.append(f"{name}→{sid}")
            print(f"    {q:14s} 命中 {hits[:3]}")
    else:
        print("    （无）")

    # ---------- 推荐 ----------
    print("\n" + "=" * 70)
    safe = [k for k, (ok, bad, _) in results.items() if bad == 0]
    print("★ 结论")
    print("=" * 70)
    if safe:
        best = max(safe)
        ok = results[best][0]
        print(f"  阈值 min_len={best}：负例零误匹配，正例命中 {ok:.0%}")
        print(f"  → **可以启用**，但仍需日志监控")
    else:
        print("  ★ 所有阈值都有负例误匹配 → **建议不启用**，"
              "或只在「无精确命中时」作为提示候选（不直接替换答案）")
    print("""
★ 设计建议（若启用）：
  模糊匹配**只用于给候选提示**，不直接决定答案——
  「你是不是想问『黄金体验』？」让用户确认，
  而不是系统自己认定。这样误匹配的危害从「答错」降为「多问一句」。""")
    return 0


if __name__ == "__main__":
    sys.exit(main())

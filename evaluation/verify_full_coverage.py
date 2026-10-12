"""全量覆盖率核查：**每一个替身，用户到底能不能查到？**

起因（用户反馈）：
  「数据库感觉有点少啊 真的适合做成 rag 吗」
  「还是缺失很多替身啊 第九部的替身我基本没看见」
  「还是缺失很多替身啊 比如繁荣之墓 再度失言」

★ 三次反馈的共同点：**用户说"缺"，但数据其实在库里** ——
  缺的是「能不能被查到」这一层（中文名映射 / 异译收录 / 意图识别）。
  → 所以不能只看「表里有多少行」，必须逐个替身验一遍**检索路径**。

本脚本做的事（离线，不需要起服务）：
  对每一个替身，把它的**所有可匹配写法**（英文名、中文主名、异译别名）
  丢进别名表，看是否都能映射回**它自己**。
  任何一条映射不到、或映射到别的替身，都会报出来。

这样能一次性回答：
  - 有没有替身是**完全查不到**的（数据在但无路可走）
  - 有没有译名**映射错**（指向了别的替身 —— 比查不到更危险）

用法：
    python evaluation/verify_full_coverage.py
退出码 0 = 全部可查；1 = 有缺口。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROC = ROOT / "dataset" / "processed"
sys.path.insert(0, str(ROOT / "services"))

import aliases  # noqa: E402


def han(s: str) -> int:
    return len(re.findall(r"[\u4e00-\u9fff]", s or ""))


def main() -> int:
    stands = json.loads((PROC / "stands.json").read_text(encoding="utf-8"))
    zh = json.loads((PROC / "stand_name_zh.json").read_text(encoding="utf-8"))
    alias2id, id2alias = aliases.build_alias_table()

    # ★ 有意不收中文名的替身（单字名会抢匹配，M14 实测）
    EXEMPT_ZH = {"tusk", "tusk_act1", "tusk_act2", "tusk_act3",
                 "tusk_act4", "surface"}

    print("=" * 70)
    print("全量覆盖率核查：逐个替身验证「能不能查到」")
    print("=" * 70)
    print(f"替身总数 {len(stands)}｜别名表 {len(alias2id)} 个可匹配写法")

    no_route: list[tuple[str, str]] = []      # 完全查不到
    wrong: list[tuple[str, str, str]] = []    # 映射到别的替身
    zh_missing: list[tuple[str, str]] = []    # 无中文名（非豁免）

    for s in stands:
        sid = s["stand_id"]
        en = s.get("name_en") or ""

        # 1) 英文名必须能命中自己
        hit = alias2id.get(en)
        if hit is None:
            no_route.append((sid, en))
        elif hit != sid:
            wrong.append((en, sid, hit))

        # 2) 中文名（主名 + 异译）必须都能命中自己
        rec = zh.get(sid)
        names: list[str] = []
        if rec:
            names = [rec.get("name_zh")] + list(rec.get("name_zh_variants") or [])
            names = [n for n in names if n]
        if not names:
            if sid not in EXEMPT_ZH:
                zh_missing.append((sid, en))
            continue
        for n in names:
            hit = alias2id.get(n)
            if hit is None:
                no_route.append((sid, f"{n}（{en} 的中文名）"))
            elif hit != sid:
                wrong.append((n, sid, hit))

    # ---- 报告 ----
    print(f"\n[1] 完全查不到的替身：{len(no_route)}")
    for sid, what in no_route[:20]:
        print(f"      ✗ {sid:26s} ← {what}")
    if not no_route:
        print("      ✓ 无 —— 每个替身都至少有一条可查路径")

    print(f"\n[2] 译名映射到**别的**替身（比查不到更危险）：{len(wrong)}")
    for name, want, got in wrong[:20]:
        print(f"      ✗ 「{name}」应指向 {want}，实际指向 {got}")
    if not wrong:
        print("      ✓ 无 —— 没有张冠李戴")

    print(f"\n[3] 无中文名（豁免 {len(EXEMPT_ZH)} 个单字名替身）：{len(zh_missing)}")
    for sid, en in zh_missing[:20]:
        print(f"      · {sid:26s} {en}")
    if not zh_missing:
        print("      ✓ 无 —— 豁免名单之外全部有中文名")

    # ---- 汇总 ----
    n_zh = sum(1 for s in stands if zh.get(s["stand_id"]))
    print("\n" + "=" * 70)
    print(f"汇总：中文名 {n_zh}/{len(stands)}"
          f"（{n_zh / len(stands) * 100:.0f}%）"
          f"｜完全查不到 {len(no_route)}｜映射错 {len(wrong)}")
    print("=" * 70)

    ok = not no_route and not wrong and not zh_missing
    print("✓ 全部可查" if ok else "✗ 存在缺口（见上）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

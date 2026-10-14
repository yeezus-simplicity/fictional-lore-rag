"""M52 回归测试：档案卡左侧「一大片空白」。

用户反馈（截图）：
  「你看这个档案这一块 左边文字有一大边空白
    因为右边的六维图会比文字大很多 有没有什么解决方法
    **不要跟我说缩小六维图这种解决方法**」

== 实测（Playwright，视口 1163px）==
    左栏文字 .zh-card-inner  **174px**
    右栏雷达 .sp-radar        **375px**
    → 左栏下方 **201px 空白（占右栏高度的 54%）**
  成因：`.stand-panel { align-items: flex-start }`
    —— 左栏高度只按内容算，多出来的高度自然就成了空白。

== 做法（不缩雷达，用户明确要求）==
  反过来让**左栏有足够的内容**：
  1. 原来的「六维」是挤在**一行**里（`.zc-dims`，6 项 28px）；
     改成 **3×2 的六格明细**（每格「维度 / 等级」）。
     ★ 这是雷达**同一份数据的另一种表示**（图 + 明细），
       而且能表达雷达给不出的信息 ——「无数据」是明确状态，不是 0。
  2. `.stand-panel` 去掉 `align-items: flex-start`（用默认 stretch），
     `.sp-info` 改成纵向 flex，明细块 `flex: 1 1 auto`
     → 明细吃掉剩余高度，左栏与右栏**底部对齐**。
  ★ 用户上一轮已明确排除「缩小六维图」，所以这里一个字都没动雷达尺寸。

实测结果（视口 1163px）：
    Tusk 左栏 375 = 右栏 375，底部差 **0px**
    Star Platinum 左栏 343 = 右栏 343，底部差 **0px**

== 为什么要把这件事写进测试 ==
  这三个 CSS 属性是一组**互相依赖**的约束（少任何一个，空白就回来），
  而且都不会报错 —— 只有肉眼看页面才发现。
  静态断言能在改样式时立刻发现「约束被拆掉」。

★ 为什么「无数据」这条也要测：
  实测**64/187 个替身**有缺失维度，其中 Tusk 这类「形态母体」是六维全缺
  （`NOT_APPLICABLE` = 该源未提供；能力值记在 ACT1~ACT4 上）。
  也就是说左侧六格里出现「无数据」是常见路径，不是边角情况。
  必须确认它是「无数据」而**不是 0**（与项目一贯原则一致：
  缺值绝不填 0 —— 填 0 会被读成「这项能力为零」，含义相反）。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "api" / "static" / "index.html"
PROC = ROOT / "dataset" / "processed"

DIM_KEYS = ["pwr", "spd", "rng", "sta", "prc", "dev"]


def main() -> int:
    fails: list[str] = []
    n = 0

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal n
        n += 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}"
              f"{('   ' + detail) if detail and not ok else ''}")
        if not ok:
            fails.append(name)

    print("=" * 74)
    print("M52：档案卡左右两栏的高度平衡")
    print("=" * 74)

    src = HTML.read_text(encoding="utf-8")
    css = re.sub(r"<script[^>]*>.*?</script>", "", src, flags=re.S)

    def rule(sel: str) -> str:
        m = re.search(re.escape(sel) + r"\s*\{([^}]*)\}", css)
        return m.group(1) if m else ""

    # ---- ① 三条互相依赖的布局约束 ----
    print("\n[1] 布局约束（少任何一条，空白就会回来）")
    panel = rule(".stand-panel")
    check(".stand-panel 不再用 align-items:flex-start",
          panel and "flex-start" not in panel,
          f"实际：{panel.strip()[:70]}")
    check(".stand-panel 是 flex 横向布局",
          "display: flex" in panel)
    info = rule(".sp-info")
    check(".sp-info 是纵向 flex（档案在上、明细在下）",
          "flex-direction: column" in info, f"实际：{info.strip()[:70]}")
    grid = rule(".zc-dimgrid")
    check(".zc-dimgrid 用 flex:1 吃掉剩余高度",
          "flex: 1 1 auto" in grid, f"实际：{grid.strip()[:70]}")
    cells = rule(".dg-cells")
    check(".dg-cells 是 3 列网格",
          "repeat(3, 1fr)" in cells, f"实际：{cells.strip()[:70]}")

    # ---- ② 旧的「六维」一行已移除（否则内容重复） ----
    print("\n[2] 旧的「六维」单行已移除")
    check("模板里不再有 <span class=\"zc-k\">六维</span>",
          'class="zc-k">六维' not in src)
    check("旧的 .zc-dims 样式已清理",
          ".zc-dims {" not in css)

    # ---- ③ 缺失维度必须显示「无数据」而不是 0 / 空白 ----
    print("\n[3] 缺值的显示规则")
    check("明细格按 stats_cn 逐项生成（格子数 = 维度数）",
          bool(re.search(r"Object\.entries\(zc\.stats_cn", src)))
    check("空值渲染成「无数据」",
          bool(re.search(r"v\s*\?\s*esc\(v\)\s*:\s*'无数据'", src)))
    check("空值有弱化样式（zc-na）",
          "zc-na" in src and bool(re.search(r"\.dc-v\.zc-na\s*\{", css)))
    check("★ 没有把空值当成 0 的写法",
          not re.search(r"stats_cn[\s\S]{0,400}?\|\|\s*0", src))

    # ---- ④ 窄屏适配 ----
    print("\n[4] 窄屏")
    check("窄屏下明细改 2 列（避免格子被压得太窄）",
          bool(re.search(r"max-width:\s*560px[\s\S]{0,120}repeat\(2, 1fr\)", css)))
    check("窄屏下档案与雷达上下堆叠（原有行为保留）",
          bool(re.search(r"max-width:\s*860px[\s\S]{0,160}flex-direction:\s*column",
                         css)))

    # ---- ⑤ 数据侧：缺值是常态，且形态母体六维全缺 ----
    print("\n[5] 数据侧（决定这条路径的常见程度）")
    stats = {r["stand_id"]: r for r in json.loads(
        (PROC / "stand_stats.json").read_text(encoding="utf-8"))}
    n_missing = sum(1 for r in stats.values() if (r.get("missing_count") or 0) > 0)
    check(f"有缺失维度的替身 > 0（实测 {n_missing}/{len(stats)}）", n_missing > 0)
    tusk = stats.get("tusk")
    check("Tusk 这类形态母体：六维全缺",
          tusk is not None and all(tusk.get(k) is None for k in DIM_KEYS),
          f"实际 {[(k, tusk.get(k)) if tusk else None for k in DIM_KEYS][:3]}")
    check("Tusk 的类别标为 NOT_APPLICABLE（该源未提供，不是 0）",
          tusk is not None
          and all(tusk.get(f"{k}_cat") == "NOT_APPLICABLE" for k in DIM_KEYS))
    sp = stats.get("star_platinum")
    check("有完整六维的替身也存在（六格会显示等级字母）",
          sp is not None and all(isinstance(sp.get(k), int) for k in DIM_KEYS))

    print()
    if fails:
        print(f"✗ {len(fails)}/{n} 项失败：" + "、".join(fails))
        return 1
    print(f"✓ 全部通过（{n} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

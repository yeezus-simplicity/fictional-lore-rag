"""M76：招式 / 必杀技覆盖（12 → 156 个替身）+ 自然语言入口

== 起点 ==
`data_facts.json` 里 `n_moves = 23`（覆盖 **11 个替身**，占 187 的 6%）——
全库覆盖率最低的一块。

== 根因（两层）==

【1】渲染器的兜底判据太窄
    `render_fetch.js` 提招式有三条路：
      3a `div.techBox`（源站的招式卡片）—— 只有热门角色才有
      3b 能力段的**列表**项，格式 `Name (Alias): desc`
      3c 能力段的**段落**，格式 `Name: desc`
    而 3b 的段名判据是白名单 `/attack|move|abilit|technique|method|form/i`
    → 实测漏掉的段名（**全都是 `== Abilities ==` 下的 h3**）：
        LIFE GIVER（Gold Experience，18 个 list 被整段丢掉）
        SUPERHUMAN CAPABILITIES（Star Platinum）
        SPECIFIC USES / BUBBLE GENERATION / RESTORATION / TIME STOP …
    → 全库只有 **11 / 185** 个替身提得到招式。

【2】还有一类页面：招式名就是 **h3 标题本身**
    Gold Experience 的 `=== Life Giver ===` / `=== Life Shot ===`
    下面装的是「Turning bank notes into butterflies」这类**用例**，
    不是 `Name: desc` → 3a/3b/3c 一条都提不到，整页 moves=0。

== 修法 ==
· 3b 的段名判据改成「**在 Abilities 这个 h2 之下**」+ 反向黑名单
  （Appearance / Personality / History / Gallery … 不是招式）。
  反向比正向稳：能力段名是**开放集合**（每个替身自己起名），
  非能力段是**封闭集合**。
· 新增 3d：**该页一个招式都没提到时**，把能力段下的 h3 标题当招式名。
  ★ 只在 moves 为空时启用 —— Star Platinum 有 4 个真招式，
    不该再补一条 "Superhuman Capabilities"（那是章节名）。

== 另外两个坑 ==
· `--only` **不会覆盖已存在的快照**（`if (!force && fs.existsSync(outFile))`）
  → 我第一轮改完重抓，日志说"成功 4/4"，但文件 mtime 还是凌晨的，
    白跑一趟才发现。**必须加 `--force`**。
· `console.log` 在 `page.evaluate` 里是**浏览器上下文**，看不到 ->
  调试要改「把信息写进返回值」。

跑法：python evaluation/test_m76_moves.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services"))
sys.path.insert(0, str(ROOT / "database"))

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          + (f"   {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    print("=" * 74)
    print("M76：招式 / 必杀技覆盖 + 自然语言入口")
    print("=" * 74)

    # ---- ① 意图识别 ----
    print("\n[1] ★ 招式意图（此前全是 unknown）")
    from executor import detect_intent
    for q in ("Star Platinum 有什么招式", "白金之星有什么招式",
              "黄金体验的必杀技", "Killer Queen 的招式",
              "银色战车的招数"):
        check(f"「{q}」→ moves", detect_intent(q) == "moves",
              f"实得 {detect_intent(q)}")
    # ★ 刻意不抢「能力」问法（那要走语义检索拿原文长段）
    for q in ("疯狂钻石的能力是什么", "黄金体验的能力", "白金之星的破坏力是几级"):
        check(f"「{q}」**不**判成 moves", detect_intent(q) != "moves",
              f"实得 {detect_intent(q)}")

    # ---- ② 覆盖率（需 DB）----
    print("\n[2] ★ 覆盖率（修前 11 个替身 / 23 条）")
    try:
        from load_db import connect
        conn = connect()
        cur = conn.cursor()
        cur.execute("SELECT count(*), count(DISTINCT stand_id) FROM stand_moves")
        n_rows, n_stands = cur.fetchone()
        check(f"招式条数 ≥ 250（实得 {n_rows}）", n_rows >= 250)
        check(f"覆盖替身 ≥ 150（实得 {n_stands}）", n_stands >= 150)
        # 关键替身必须有招式（修前这几条全空）
        for sid, want in (("star_platinum", 4), ("gold_experience", 3),
                          ("killer_queen", 3), ("tusk", 4)):
            cur.execute("SELECT count(*) FROM stand_moves WHERE stand_id=%s",
                        (sid,))
            got = cur.fetchone()[0]
            check(f"{sid} 有 {want} 个招式", got >= want, f"实得 {got}")
        # gold_experience 的招式名必须是 h3 标题（3d 路径的证据）
        cur.execute("SELECT name FROM stand_moves WHERE stand_id="
                    "'gold_experience' ORDER BY move_id")
        names = [r[0] for r in cur.fetchall()]
        check("★ Gold Experience 的招式来自 h3 标题（Life Giver 等）",
              any("Life" in (n or "") or "LIFE" in (n or "") for n in names),
              f"实得 {names}")
        # 描述不能是空的（3d 取的是该 h3 下第一段）
        cur.execute("SELECT count(*) FROM stand_moves "
                    "WHERE text IS NULL OR length(text) < 30")
        n_empty = cur.fetchone()[0]
        check("没有空描述（<30 字符）的招式", n_empty == 0, f"实得 {n_empty}")
        conn.close()
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠ 数据库不可用，跳过覆盖率检查（{type(e).__name__}）")

    # ---- ③ 闸门数字 ----
    print("\n[3] data_facts.json（闸门的单一来源）")
    facts = json.loads((ROOT / "dataset" / "processed" / "data_facts.json")
                       .read_text(encoding="utf-8"))
    check(f"n_moves = {facts.get('n_moves')}（≥250）",
          (facts.get("n_moves") or 0) >= 250)

    print("\n" + "=" * 74)
    if FAILS:
        print(f"✗ {len(FAILS)} 项失败：")
        for f in FAILS:
            print(f"   · {f}")
        return 1
    print("✓ M76 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())

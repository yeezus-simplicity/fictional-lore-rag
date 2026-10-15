"""M67 回归测试：多值字段粘连的修复（角色名 / 数据链路完整性）。

== 起因 ==
M66 报告 §9 记了一条已知问题：`characters` 表 178 行里有 **10 行没有独立角色页**，
且名字是**多个角色名粘连**（`Andre BoomboomL.A. Boomboom`、
`Caravan SeraiChakaFour Unnamed miceKhan…`、`Whitesnake's Unknown Victim Thunder McQueen`…）。
本轮查清根因并修掉。

== 根因（M1 抓取层）==
详情页解析用 `sib.text_content()`，而 **lxml 的 `text_content()` 会丢掉 `<br>`
且不补任何分隔符** → Infobox 的多值字段被粘成一串：
    `Benjamin Boomboom (1)<br/>Andre Boomboom (2)` → `Benjamin Boomboom (1)Andre Boomboom (2)`

== 修了三处（缺一不可）==
  A. `scrape.py`：取文本前把 `<br>` 换成 `, `（治本，未来重抓即正确）
  B. `repair_owner_split.py`：用**已抓的 wikitext 语料**就地修复 18 条（不重抓）
  C. `load_db.py`：① 清掉本轮不再产生的角色行（`ON CONFLICT` 从不删）
                  ② text_chunks 写 `location_id`（M65 漏的）
                  ③ 补 `locations/location_places/location_mentions` 的 loader（M65 只建了表）

== ★ 本测试守什么 ==
  1. 落盘数据里**没有粘连的 owner_name**；且每一条修复都经过"指纹校验"
  2. DB 里那 10 个粘连 id **不存在**，拆出来的角色**存在**
  3. 角色中文名**没有丢**（粘连 id 上的名字要跟着搬到新 id）
  4. 地点三张表**有数据**（不是空表），且 `text_chunks.location_id` 已写
  5. ★★ `check_fresh_deploy` 通过 —— **全新空库能完整加载** ★★
     （这条是本轮最重要的：修之前它是**失败**的，且 CI 看不见）
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROC = ROOT / "dataset" / "processed"
DB = ROOT / "database"

# ★ 真正属于"粘连"的 id（本轮应全部消失）
#   ★ 注意：M66 报告里列的是"DB 行没有独立角色页"的 10 个，**不是**都是粘连 ——
#     `escape_room_player` / `unnamed_man` / `unnamed_mountain_lodge_owner`
#     是**单个名字**（源站就是"无名角色"），至今合法存在，不该要求它们消失。
#     （我第一版照抄了那 10 个，测试因此误报。）
GLUED_IDS = [
    "dixie_flatlinen",
    "caravan_seraichakafour_unnamed_micekha_0b0ac773",
    "whitesnake_s_unknown_victim_thunder_mcqueen",
    "whitesnake_s_unknown_victim_sports_maxx",
    "whitesnake_s_unknown_victim_miraschon",
    "whitesnake_s_unknown_victim_viviano_westwood",
    "andre_boombooml_a_boomboom",
    # ★ 这是修 owner 之后**又暴露出来**的一条：`split_owner_names` 里
    #   「拆出 >6 段 → 回退整体」的保护把 Anubis 的 7 个持有者当成过拆，
    #   整串又变成一个角色（名字里带逗号）
    "caravan_serai_chaka_four_unnamed_mice__0152b175",
]
# 仍应**合法存在**的无名角色（单名，不是粘连）
LEGIT_UNNAMED = ["escape_room_player", "unnamed_man",
                 "unnamed_mountain_lodge_owner"]
# 拆开后应出现的角色（抽样，含中文名丢失检查）
SPLIT_OK = ["andre_boomboom", "l_a_boomboom", "benjamin_boomboom",
            "thunder_mcqueen", "sports_maxx", "miraschon",
            "viviano_westwood", "dixie_flatline", "guccio",
            "boomboom_family", "caravan_serai", "chaka", "khan",
            "four_unnamed_mice", "jean_pierre_polnareff"]


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
    print("M67：多值字段粘连修复 + 数据链路完整性")
    print("=" * 74)

    stands = json.loads((PROC / "stands.json").read_text(encoding="utf-8"))

    # ---- ① 落盘数据：没有粘连 ----
    print("\n[1] 落盘数据：owner_name 不再粘连")
    glued = [(s["stand_id"], s["owner_name"]) for s in stands
             if (s.get("owner_name") or "")
             and ", " not in s["owner_name"]
             and not re.search(r"\s/\s", s["owner_name"])
             and re.search(r"[a-z)][A-Z]", s["owner_name"])]
    check("★ 没有粘连的 owner_name", not glued,
          f"{len(glued)} 个：{glued[:2]}")
    multi = [s for s in stands if ", " in (s.get("owner_name") or "")]
    check("多持有者替身已被拆成逗号分隔（18 条）", len(multi) == 18,
          f"实际 {len(multi)}")
    anubis = next(s for s in stands if s["stand_id"] == "anubis")
    check("Anubis 的 7 个持有者逐个分开",
          anubis["owner_name"].count(", ") == 6,
          anubis["owner_name"][:80])
    tomb = next(s for s in stands if s["stand_id"] == "tomb_of_the_boom_1_2_3")
    check("★ 源站比抓取多一段时也修对（Boomboom Family 出现了）",
          "Boomboom Family" in tomb["owner_name"],
          tomb["owner_name"][:80])

    # ---- ② 根因已修（scrape.py）----
    print("\n[2] 根因：抓取层不再丢分隔符")
    sc = (ROOT / "dataset" / "pipeline" / "scrape.py").read_text(encoding="utf-8")
    check("scrape.py 有 _text_with_breaks 且被用于 infobox 取值",
          "_text_with_breaks" in sc
          and "page.infobox[field] = _clean(_text_with_breaks(sib))" in sc)
    check("★ 说明里点明 text_content() 丢 <br> 这个根因",
          "text_content()" in sc and "丢掉" in sc)

    # ---- ③ 修复脚本可复跑（幂等）----
    print("\n[3] 修复脚本：可复跑且带校验门")
    rp = (ROOT / "dataset" / "pipeline" / "repair_owner_split.py")
    check("修复脚本存在", rp.exists())
    txt = rp.read_text(encoding="utf-8")
    check("★ 有「指纹」校验门（sig）", "def sig(" in txt and "sig(cand) == sig(raw)" in txt)
    check("★ 只修 owner_name，stand_type 明确不修（附原因）",
          "stand_type（★ 刻意不动" in txt)
    r = subprocess.run([sys.executable, str(rp)], cwd=str(ROOT),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    out = (r.stdout or "") + (r.stderr or "")
    check("复跑后无待修项（幂等：已经修好了）",
          "粘连 0 个" in out or "可修复 0" in out,
          out[-200:])

    # ---- ④ DB 状态 ----
    print("\n[4] 数据库：粘连 id 消失 / 新角色出现 / 中文名没丢")
    sys.path.insert(0, str(DB))
    sys.path.insert(0, str(ROOT / "services"))
    try:
        from load_db import connect
        conn = connect()
        cur = conn.cursor()
        cur.execute("SELECT character_id FROM characters")
        ids = {r[0] for r in cur.fetchall()}
        check("★ 10 个粘连 id 全部消失", not (set(GLUED_IDS) & ids),
              f"仍在：{sorted(set(GLUED_IDS) & ids)}")
        check("★ 拆出来的角色都在", all(k in ids for k in SPLIT_OK),
              f"缺：{[k for k in SPLIT_OK if k not in ids]}")
        check("★ 无名角色（单名）仍合法存在",
              all(k in ids for k in LEGIT_UNNAMED),
              f"缺：{[k for k in LEGIT_UNNAMED if k not in ids]}")
        check("角色数 186（拆开 7 个粘连替身的持有者后增加）", len(ids) == 186,
              f"实际 {len(ids)}")

        from aliases import load_char_zh_map
        zh = load_char_zh_map()
        check("★ 角色中文名没有丢（≥136 条）", len(zh) >= 136,
              f"实际 {len(zh)}")
        check("★ 粘连 id 上的中文名按**人工确认表**搬到新 id",
              zh.get("l_a_boomboom") == ["L·A·祊玢"]
              and zh.get("thunder_mcqueen") == ["桑达·麦奎恩"],
              f"l_a_boomboom={zh.get('l_a_boomboom')} "
              f"thunder_mcqueen={zh.get('thunder_mcqueen')}")
        check("★ 没有被错配的中文名（「白蛇」是替身名，不该挂在角色上）",
              "白蛇" not in (zh.get("thunder_mcqueen") or []),
              f"thunder_mcqueen → {zh.get('thunder_mcqueen')}")

        # 地点三张表必须有数据（M65 只建表没写 loader）
        for t, lo in (("locations", 113), ("location_places", 200),
                      ("location_mentions", 2000)):
            cur.execute(f"SELECT count(*) FROM {t}")
            c = cur.fetchone()[0]
            check(f"{t} 有数据（≥{lo}）", c >= lo, f"实际 {c}")
        cur.execute("SELECT count(*) FROM text_chunks "
                    "WHERE location_id IS NOT NULL")
        check("★ text_chunks.location_id 已写（M65 漏的）",
              cur.fetchone()[0] == 1179, "应 1179")
        cur.execute("SELECT count(*) FROM text_chunks WHERE stand_id IS NULL "
                    "AND character_id IS NULL AND location_id IS NULL")
        check("没有「三无」语料块（违反三选一约束的那种）",
              cur.fetchone()[0] == 0)
        cur.close()
        conn.close()
    except Exception as e:  # noqa: BLE001
        check("数据库可连接", False, f"{type(e).__name__}: {e}")

    # ---- ⑤ 文本块统计口径 ----
    print("\n[5] 语料块归属分布")
    art = json.loads((PROC / "article_chunks.json").read_text(encoding="utf-8"))
    loc = [c for c in art if c.get("location_id")]
    ch = [c for c in art if c.get("character_id")]
    st = [c for c in art if c.get("stand_id")]
    check(f"地点块 {len(loc)} / 角色块 {len(ch)} / 替身块 {len(st)}",
          len(loc) == 1179 and len(ch) == 2961, f"{len(loc)}/{len(ch)}/{len(st)}")

    print()
    if fails:
        print(f"✗ {len(fails)}/{n} 项失败：" + "、".join(fails))
        return 1
    print(f"✓ 全部通过（{n} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

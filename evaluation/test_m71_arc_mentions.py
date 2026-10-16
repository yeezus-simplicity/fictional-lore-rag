"""M71：rendered 快照缺口 + 篇章语料证据链

两个用户反馈的后续（M70 只做到「说清口径差异」，这轮做到「真能答」）：

【1】rendered 快照缺口（数据新鲜度）
  `dataset/sources/rendered/` 只有 148 个快照，而替身已有 187 个。
  `stand_appearances` 正是从 rendered/ 解析的 → 40 个替身的
  出场记录与必杀技**恒为空**，包括 `Les Feuilles`
  （勒索之路现象的真正来源，M70 答案里漏掉的就是它）。
  根因：M39–M45 补第 8/9 部替身后从未重抓快照
  —— 与 M68「没进过抓取队列」、M70「抓取名单有盲区」
  是**同一类问题的第三次**（上游数据增长，下游产物没跟着重算）。

【2】篇章语料证据链
  `story_arcs.json` 的章节是**部内相对章号**，而 `stand_appearances`
  第 1–5 部是**绝对章号** → 直接把 "DU 153-162" 拿去匹配会得 0 个替身
  （实测 Killer Queen 明明在 418–427 章出场却一条都匹配不上）。
  → 两侧都换算到绝对章号后再比（偏移 44/113/265/439）。

跑法：python evaluation/test_m71_arc_mentions.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROC = ROOT / "dataset" / "processed"
FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          + (f"   {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    print("=" * 74)
    print("M71：rendered 快照缺口 + 篇章语料证据链")
    print("=" * 74)

    # ---- ① rendered 快照覆盖 ----
    print("\n[1] rendered 快照覆盖（出场记录的原料）")
    snap_dir = ROOT / "dataset" / "sources" / "rendered"
    snaps = {f.stem for f in snap_dir.glob("*.json")}
    check("快照数 ≥180", len(snaps) >= 180, f"实得 {len(snaps)}")
    check("★ Les Feuilles 快照已补（M70 答案里漏的就是它）",
          "les_feuilles" in snaps)

    # ---- ② 出场记录覆盖 ----
    print("\n[2] stand_appearances 覆盖（有库才跑）")
    conn = None
    try:
        sys.path.insert(0, str(ROOT / "database"))
        from load_db import connect
        conn = connect()
        cur = conn.cursor()
    except BaseException as e:                     # noqa: BLE001
        print(f"  SKIP  数据库不可用（{type(e).__name__}）")
        conn = None

    if conn is not None:
        cur.execute("SELECT stand_id FROM stands")
        all_sid = {r[0] for r in cur.fetchall()}
        cur.execute("SELECT DISTINCT stand_id FROM stand_appearances")
        has_app = {r[0] for r in cur.fetchall()}
        miss = all_sid - has_app
        check("★ 有出场记录的替身 ≥180（修前 145）",
              len(has_app) >= 180, f"实得 {len(has_app)}")
        check("★ 无出场记录的替身 ≤10（修前 43）",
              len(miss) <= 10, f"仍缺 {len(miss)}：{sorted(miss)[:6]}")
        cur.execute("SELECT count(*) FROM stand_appearances")
        n_app = cur.fetchone()[0]
        check("★ 出场记录数 ≥4800（修前 4111）", n_app >= 4800, f"实得 {n_app}")
        cur.execute("SELECT count(*) FROM stand_appearances "
                    "WHERE stand_id = 'les_feuilles'")
        check("★ Les Feuilles 有出场记录（修前 0）",
              cur.fetchone()[0] > 0)

        # 剩余缺口必须是**真实情况**而非解析失败：
        #   · Echoes ACT1-3 没有独立 wiki 页面（M1 已记录）
        #   · Remote Romance / House of Holy / Smallfaces 的
        #     "APPEARANCE" 段讲的是**外观长相**，不是话数出场
        legit = {"echoes_act1", "echoes_act2", "echoes_act3",
                 "remote_romance", "house_of_holy", "smallfaces"}
        check("★ 剩余缺口全部是有据可查的真实情况（非解析失败）",
              miss <= legit, f"意外缺失：{sorted(miss - legit)}")

    # ---- ③ arc_mentions.json ----
    print("\n[3] 篇章证据链文件")
    am_f = PROC / "arc_mentions.json"
    check("arc_mentions.json 已生成", am_f.exists())
    if not am_f.exists():
        print("\n" + "=" * 74)
        print(f"✗ {len(FAILS)} 项未通过")
        return 1
    am = json.loads(am_f.read_text(encoding="utf-8"))
    check("篇章数 ≥180", am["n_arcs"] >= 180, f"{am['n_arcs']}")
    check("★ 有替身关联的篇章 ≥150", am["n_with_stands"] >= 150,
          f"{am['n_with_stands']}")

    by_id = {a["arc_id"]: a for a in am["arcs"]}

    # ---- ④ 章号口径换算（本轮核心）----
    print("\n[4] ★ 章号口径：部内相对 → 绝对")
    sh = by_id.get("shakedown_road") or {}
    check("★ 勒索之路 = 第 8 部（JJL 前缀）", sh.get("part") == 8,
          f"实得 {sh.get('part')}")
    check("★ JJL 18-22 是部内相对 → 不换算（第 6–9 部两边都相对）",
          sh.get("chapters_abs") == [18, 22],
          f"实得 {sh.get('chapters_abs')}")
    ao = by_id.get("another_one_bites_the_dust") or {}
    check("★ DU 153-162 换算成绝对 418-427（+265）",
          ao.get("chapters_abs") == [418, 427],
          f"实得 {ao.get('chapters_abs')}")
    check("★ 换算后能圈到 Killer Queen（修前 0 个）",
          any(s.get("stand_id") == "killer_queen" for s in ao.get("stands") or []),
          str([s.get("stand_id") for s in ao.get("stands") or []][:6]))

    # 无前缀的「Chapters N - M」是第 1/2 部，本身就是绝对章号
    sm = by_id.get("the_stone_mask") or {}
    check("★ 无前缀 Chapters 8-11 判为第 1 部且不换算",
          sm.get("part") == 1 and sm.get("chapters_abs") == [8, 11],
          f"part={sm.get('part')} abs={sm.get('chapters_abs')}")

    # ---- ⑤ 跨部污染必须被挡住 ----
    print("\n[5] ★ 跨部污染（只按章号查会误撞）")
    sd = by_id.get("san_diego_beach") or {}
    names_sd = [s.get("stand_id") for s in sd.get("stands") or []]
    check("★ 第7部 ch1-4 只圈到 In a Silent Way（不撞第8部 ch1-4）",
          names_sd == ["in_a_silent_way"], f"实得 {names_sd}")

    # ---- ⑥ 数据质量 ----
    print("\n[6] 数据质量")
    all_stands = [s for a in am["arcs"] for s in (a.get("stands") or [])]
    check("★ 没有 name_en 为空的替身（修前 35 条）",
          all(s.get("name_en") for s in all_stands),
          str([s.get("stand_id") for s in all_stands if not s.get("name_en")][:5]))
    check("★ 形态折叠进父替身（Tusk 不被列成 5 行）",
          sum(1 for s in (sh.get("stands") or [])
              if str(s.get("stand_id", "")).startswith("tusk")) == 1,
          str([s.get("stand_id") for s in sh.get("stands") or []]))

    # ---- ⑦ 服务层 ----
    print("\n[7] 篇章可问答（修前只能得到 location_hint）")
    sys.path.insert(0, str(ROOT / "services"))
    import locations as L

    ans, _, _ = L.answer("篇章 勒索之路 的替身是什么")
    check("★ 「篇章 勒索之路」→ arc_fact（修前 location_hint）",
          ans["type"] == "arc_fact", ans["type"])
    check("★ 答案含 Les Feuilles（M70 漏掉的那个）",
          any(s.get("name") == "Les Feuilles" for s in ans.get("stands") or []),
          str([s.get("name") for s in ans.get("stands") or []]))
    check("★ 每条带话数与原文证据",
          all(s.get("chapter") and s.get("snippet")
              for s in ans.get("stands") or []))

    ans2, _, _ = L.answer("Anubis 是哪一段的故事")
    check("★ 无同名地点的篇章也能答（Anubis → 第3部）",
          ans2["type"] == "arc_fact" and ans2["arc"]["part"] == 3,
          f"{ans2['type']} part={ans2.get('arc', {}).get('part')}")

    ans3, _, _ = L.answer("勒索之路的替身是什么")
    check("★ 没提「篇章」时仍走地点口径（不抢答）",
          ans3["type"] == "location_related", ans3["type"])

    ans4, _, _ = L.answer("篇章 一个不存在的篇章名 xyzzy")
    check("★ 篇章不存在时不硬答（宁拒答）",
          ans4["type"] != "arc_fact", ans4["type"])

    # ---- ⑧ 前端 ----
    print("\n[8] 前端渲染")
    html = (ROOT / "api" / "static" / "index.html").read_text(encoding="utf-8")
    check("★ 前端渲染 arc_fact", "arc_fact" in html)
    check("★ 替身标签带话数（证据可见）", "ch${esc(String(x.chapter))}" in html)

    print("\n" + "=" * 74)
    if FAILS:
        print(f"✗ {len(FAILS)} 项未通过：")
        for f in FAILS:
            print(f"    - {f}")
        return 1
    print("✓ M71 全部检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())

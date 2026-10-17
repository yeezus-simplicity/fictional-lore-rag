"""M77：地点模糊 + 中文要点层 + 替身列表说明 + 亲属关系

== 用户实测反馈（四条）==
  ① 「虹村宅在哪」→ 返回 4 个「虹村」角色卡片（答非所问）
  ② 「使用者 Giorno Giovanna 有哪些替身？」→ 答案只有英文替身名，
     且底部挂着一句与问题无关的「没有替身说明其条目未标（如第 1、2 部的波纹使者）」
  ③ 「普奇的弟弟是谁」→ 只给普奇档案，不答亲属
  ④ 语义答案 98.6% 是英文原文（text_chunks 含中文 155/10866）

== 四条 → 四个根因 ==
【1】地点匹配一直是**精确**的（名字 ⊂ 问句），而角色在 M74 就有了模糊
     → 「虹村宅」少一个「邸」精确落空，角色的「虹村」子串（模糊）命中
     → 路由到 character。**不是数据缺，是地点这一侧太窄。**
     → locations.fuzzy_hits()：片段 ≥3 字且只比地名少 1 个字
       （「虹村」2 字 → 不算，仍走角色候选；这是刻意的）
     → Router 只在问「在哪/什么地方」时才用地点模糊，避免抢走角色问句。

【2】`character_stands` 的说明文案是**固定字符串**：
       「该角色的替身来自…；没有替身说明其条目未标（…）」
     挂在 2 个替身下面 → 读起来像"系统认为他没有替身"。
     → 文案跟着数据变：有替身说「共 N 个」，没有才说「条目未标注」。
     → 同时补 stands_zh（中文名），用户输入中文、答案却是英文名，对不上。

【3】`families` 字段（`Domenico Pucci (Twin Brother)`）**从来没有代码读**，
     问句被当成「普奇 是谁」→ 返回档案。
     → relations.py：解析括号里的关系，按中文称谓筛选。
     ★ 不猜长幼：问「弟弟」而源站只写 Brother / Twin Brother → 照给但明确标注。
     ★ 没有该关系时给**已有亲属全表**，不退化成档案。

【4】语料 100% 英文站，机翻会破坏证据可追溯（也不符合「不造词」）。
     → zh_layer.py：只用**库内已有中文事实**拼成中文句子，
       放在英文原文之前，并标注「描述正文仍是源站英文原文」。

跑法：python evaluation/test_m77_location_zh_relations.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "services"))
sys.path.insert(0, str(ROOT / "api"))

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          + (f"   {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:  # noqa: PLR0915
    print("=" * 74)
    print("M77：地点模糊 + 中文要点 + 替身说明 + 亲属关系")
    print("=" * 74)

    import locations as L
    import characters as C
    import relations as R
    import zh_layer as Z

    # ---- ① 地点模糊 ----
    print("\n[1] ★ 地点模糊（用户：虹村宅在哪 → 返回 4 个角色卡片）")
    for q, want in (("虹村宅在哪", "nijimura_residence"),
                    ("吉良宅在哪里", "kira_residence"),
                    ("虹村宅", "nijimura_residence"),
                    ("杜王町在哪", "morioh")):
        hits = [lid for lid, _ in L.match(q)]
        check(f"「{q}」命中 {want}", want in hits, f"实得 {hits[:4]}")
    check("「虹村」2 字**不算**地点（否则会吞掉 4 个角色候选）",
          L.match("虹村") == [], f"实得 {L.match('虹村')}")
    check("守门：「华盛顿在哪里」仍无地点命中", L.match("华盛顿在哪里") == [])

    # ---- ② 中文要点 ----
    print("\n[2] ★ 中文要点（成句的中文，排在英文原文之前）")
    check("部名映射：第 5 部 → 黄金之风", Z.part_label(5) == "第 5 部《黄金之风》",
          f"实得 {Z.part_label(5)}")
    s = Z.stand_summary({"name_zh": "黄金体验", "name_en": "Gold Experience",
                         "owner": "乔鲁诺·乔巴拿", "part": 5,
                         "stats_cn": {"破坏力": "C", "速度": "A"}},
                        ["LIFE GIVER"])
    check("替身要点含中文名与使用者",
          s and "黄金体验" in s["lines"][0] and "乔鲁诺·乔巴拿" in s["lines"][0])
    check("替身要点含招式名（保留源站原名）",
          any("LIFE GIVER" in ln for ln in s["lines"]))
    check("替身要点标注「不是机器翻译」", "机器翻译" in (s.get("note") or ""))
    # 缺数据一律写「源站未提供」，不填 0
    s2 = Z.stand_summary({"name_zh": "X", "name_en": "X", "owner": None,
                          "part": None, "stats_cn": {}}, None)
    check("无六维时写「源站未提供」而不是 0",
          any("源站未提供" in ln for ln in s2["lines"]))
    check("无部数时写「部数未知」", "部数未知" in s2["lines"][0])
    ch = Z.character_summary({"name_zh": "恩里克·普奇", "name_en": "Enrico Pucci",
                              "part": 6, "status": "Deceased",
                              "stand_names": ["Whitesnake"]})
    check("角色要点把 Deceased 译成「已故」",
          any("已故" in ln for ln in ch["lines"]), f"实得 {ch['lines']}")
    lo = Z.location_summary({"name_zh": "杜王町", "name_en": "Morioh", "part": 4,
                             "types": ["Town"],
                             "place_paths": [["S City", "M Prefecture", "Japan"]]})
    check("地点要点含类型与所属路径",
          any("类型" in ln for ln in lo["lines"])
          and any("S City" in ln for ln in lo["lines"]))

    # ---- ③ 替身列表说明文案 ----
    print("\n[3] ★ 「X 有哪些替身」的说明文案（用户：回答对不上提问）")
    ans, _, _ = C.answer("使用者 Giorno Giovanna 有哪些替身？")
    check("返回 character_stands", ans.get("type") == "character_stands",
          f"实得 {ans.get('type')}")
    check("带中文替身名且与 stands 同序",
          ans.get("stands_zh")
          and len(ans["stands_zh"]) == len(ans.get("stands") or []),
          f"实得 {ans.get('stands_zh')}")
    check("中文名确实是「黄金体验」",
          "黄金体验" in (ans.get("stands_zh") or []),
          f"实得 {ans.get('stands_zh')}")
    note = ans.get("note") or ""
    check("有替身时说明文案**不再**提「没有替身」",
          "没有替身" not in note, f"实得 {note}")
    check("说明文案给出替身数量", "2 个替身" in note, f"实得 {note}")
    # ★ M78：答案必须显示**中英双名** —— 用户输入英文、只看到中文名
    #   会以为系统认错了人（Giorno Giovanna = 乔鲁诺·乔巴拿，实测被当成 bug）
    check("★ 回传英文名（标题显示双名）",
          ans.get("character_en") == "Giorno Giovanna",
          f"实得 {ans.get('character_en')}")
    check("★ 回传命中的写法（用于「你问的是…」回显）",
          bool(ans.get("matched")), f"实得 {ans.get('matched')}")
    check("★ 数量永远给（count）", ans.get("count") == 2, f"实得 {ans.get('count')}")
    # 用户原话：「我问的是几个替身，他直接列出来而非回答我两个」
    cnt, _, _ = C.answer("Giorno Giovanna 有几个替身")
    check("★「有几个替身」被识别成**问数量**（asked_count）",
          cnt.get("asked_count") is True and cnt.get("count") == 2,
          f"实得 asked_count={cnt.get('asked_count')} count={cnt.get('count')}")
    lst, _, _ = C.answer("普奇有几个替身")
    check("★ 普奇 3 个替身（白蛇 / 新月 / 天堂制造）",
          lst.get("count") == 3 and lst.get("asked_count") is True,
          f"实得 count={lst.get('count')}")
    # 「有哪些」也要带数量（两种读法都成立）
    check("★「有哪些替身」同样回 count（标题会显示「共 2 个」）",
          ans.get("count") == 2 and ans.get("asked_count") is False,
          f"实得 {ans.get('count')} / {ans.get('asked_count')}")
    # 没有替身的角色：文案要说明「条目未标注」
    ans2, _, _ = C.answer("乔纳森·乔斯达有哪些替身")
    if ans2.get("type") == "character_stands" and not ans2.get("stands"):
        check("无替身角色说明「条目未标注」",
              "未标注" in (ans2.get("note") or ""), f"实得 {ans2.get('note')}")

    # ---- ④ 亲属关系 ----
    print("\n[4] ★ 亲属关系（用户：普奇的弟弟是谁 → 只给普奇档案）")
    check("detect 认「弟弟」", R.detect("普奇的弟弟是谁") == "弟弟",
          f"实得 {R.detect('普奇的弟弟是谁')}")
    check("detect 认「爸爸」→ 归并为「父亲」",
          R.detect("乔鲁诺的爸爸是谁") == "父亲")
    check("detect 对普通问句返回 None", R.detect("普奇的替身是什么") is None)
    r1, _, _ = C.answer("普奇的弟弟是谁")
    check("「普奇的弟弟是谁」返回 character_relatives",
          r1.get("type") == "character_relatives", f"实得 {r1.get('type')}")
    names = [i["name"] for i in (r1.get("items") or [])]
    check("给出 Domenico Pucci（源站 Twin Brother）",
          any("Domenico" in n for n in names), f"实得 {names}")
    check("★ 源站未标长幼时**明说**，不假装确定",
          "未标注长幼" in (r1.get("note") or ""), f"实得 {r1.get('note')}")
    check("strict=False（因为只标了 Twin Brother）", r1.get("strict") is False)
    r2, _, _ = C.answer("普奇的妹妹是谁")
    check("「普奇的妹妹是谁」给出 Perla Pucci 且 strict",
          r2.get("type") == "character_relatives" and r2.get("strict") is True,
          f"实得 {r2.get('strict')}")
    r3, _, _ = C.answer("乔鲁诺的父亲是谁")
    check("「乔鲁诺的父亲是谁」给出 DIO",
          any("DIO" in i["name"] for i in (r3.get("items") or [])),
          f"实得 {r3.get('items')}")
    # 没有该关系 → 不退化成档案；长幼相反的**单列**，不当成答案
    r4, _, _ = C.answer("普奇的姐姐是谁")
    check("没有「姐姐」时不退回档案，而是说明并列出已知亲属",
          r4.get("type") == "character_relatives" and r4.get("matched") is False,
          f"实得 {r4.get('type')} / matched={r4.get('matched')}")
    check("并说明源站未标注该关系",
          "没有标注" in (r4.get("note") or ""), f"实得 {r4.get('note')}")
    check("★ 把「妹妹」放进 contrast（源站标的是 Younger，不是姐姐）",
          any("Perla" in i["name"] for i in (r4.get("contrast") or [])),
          f"实得 {r4.get('contrast')}")
    check("结果列表里**没有**她（不能把妹妹当姐姐答）",
          not any("Perla" in i["name"] for i in (r4.get("items") or [])),
          f"实得 {r4.get('items')}")

    # ---- ⑤ 端到端 + 守门 ----
    print("\n[5] ★ 端到端（真实服务）与守门")
    from fastapi.testclient import TestClient
    import main as m

    ctx = TestClient(m.app)
    ctx.__enter__()
    for q, want in (("虹村宅在哪", "location"),
                    ("普奇的弟弟是谁", "character")):
        d = ctx.post("/query", params={"q": q}).json()
        check(f"「{q}」路由 → {want}", d.get("route") == want,
              f"实得 {d.get('route')}")
    d = ctx.post("/query", params={"q": "黄金体验的能力是什么"}).json()
    zs = d.get("zh_summary") or {}
    check("语义答案带中文要点",
          bool(zs.get("lines")) and "黄金体验" in zs["lines"][0],
          f"实得 {zs}")
    check("中文要点在响应里可被前端取到（response_model 没吃掉）",
          "zh_summary" in d)
    d2 = ctx.post("/query", params={"q": "使用者 Giorno Giovanna 有哪些替身？"}).json()
    check("character_stands 不重复叠中文要点（本身就是中文答案）",
          d2.get("zh_summary") is None, f"实得 {d2.get('zh_summary')}")
    for q in ("今天天气怎么样", "华盛顿在哪里", "甘道夫的替身是什么"):
        d = ctx.post("/query", params={"q": q}).json()
        check(f"守门：「{q}」仍拒答", d.get("answer_type") == "abstain",
              f"实得 {d.get('answer_type')}")

    print("\n" + "=" * 74)
    if FAILS:
        print(f"✗ {len(FAILS)} 项未通过：{FAILS}")
        return 1
    print("✓ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""M70：两个用户实测反馈 —— ①「Lisa Lisa 检索不到」②「勒索之路的替身答得不对」

用户原话：
  「比如勒索之路我记得不单单是一个地方 他是有替身」
  「我搜索lisalisa 二乔的母亲 并不会返回结果（没有检索到）」

== 这两个反馈各自暴露了什么 ==

【1】Lisa Lisa 检索不到 → **抓取名单有结构性盲区**
  角色页的 target 一直是**从 `stands.json` 的 `owner_name` 反推**的
  （`fetch_articles.build_targets` 第二个循环）—— 即「谁是替身持有者就抓谁」。
  于是：**不是任何替身持有者的角色，从来不会进抓取队列。**
  实测后果：`Lisa Lisa` / `Jonathan Joestar` / `Caesar Anthonio Zeppeli`
  在源站都有独立条目、在语料里被提到 35 次，但 523 个已抓页面里没有她们。
  根因是**第 1、2 部没有替身**（波纹时代）→ 没有任何 owner_name 会提到波纹战士。
  ★ 这个盲区和 M65 地点那个坑是同一形状：「从业务字段反推」不如
    「用分类/模板反查」可靠。源站 `Category:Part N Characters` 才是权威名单。

【2】「勒索之路的替身是什么」→ **同名歧义没处理**
  源站里 `Shakedown Road` **既是地点也是篇章**，是两个独立页面，
  页面顶部各有 `{{Hatnote}}` 互相指路：
      地点页：For the story arc of the same name, see
              [["Shakedown Road" (Story Arc)]]
      篇章页：For the location of the same name, see [[Shakedown Road]]
  我们只建了地点 → 用户问篇章的诉求（这一段里有哪些替身）
  被答成「这个地点条目里写到过谁」。
  ★ 全库 116 个 hatnote 里只有 2 个是 location↔arc（Shakedown Road /
    San Diego Beach）—— 判据刻意做窄，不泛化（详见 build_locations 的注释）。

跑法：python evaluation/test_m70_coverage_and_disambiguation.py
"""
from __future__ import annotations

import ast
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
    print("M70：抓取盲区（非持有者角色）+ 同名歧义（地点 ↔ 篇章）")
    print("=" * 74)

    art = json.loads((ROOT / "dataset" / "sources" / "jojowiki"
                      / "articles.json").read_text(encoding="utf-8"))
    pages = art["pages"] if isinstance(art, dict) and "pages" in art else art
    by_title = {p["title"]: p for p in pages}
    chars = {p["title"] for p in pages if p.get("kind") == "character"}
    arcs = {p["title"] for p in pages if p.get("kind") == "arc"}

    # ---- ① 用户点名的角色必须在库里 ----
    print("\n[1] 用户点名的角色：源站有独立条目 → 必须能检索到")
    for name in ("Lisa Lisa", "Jonathan Joestar",
                 "Caesar Anthonio Zeppeli"):
        check(f"★ {name} 已抓取", name in chars, "不在 character 页里")
        check(f"  {name} 的 entity_id 与标题一致",
              by_title.get(name, {}).get("entity_id")
              == re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_"),
              f"实得 {by_title.get(name, {}).get('entity_id')}")

    # ---- ② 结构性判据：抓取名单不能只依赖 owner_name ----
    print("\n[2] ★ 抓取名单的来源（结构性判据，不依赖网络）")
    fa_src = (ROOT / "dataset" / "sources" / "jojowiki"
              / "fetch_articles.py").read_text(encoding="utf-8")
    check("抓取器有独立的分类名单入口（不是只有 owner_name 反推）",
          "fetch_character_titles" in fa_src
          and "Category:Part" in fa_src)
    check("★ 分类口径锁定在 9 部正传（用户口径）",
          re.search(r"for part in range\(1,\s*10\)", fa_src) is not None)
    check("分类补入的条目标了来源，便于复核",
          'source_of_truth' in fa_src)
    check("★ 非持有者角色确实被补进来了（数量级）",
          len(chars) >= 300, f"character 页 {len(chars)} 个")

    # 与 stands.owner_name 的差集 —— 就是「从前抓不到」的那批
    stands = json.loads((PROC / "stands.json").read_text(encoding="utf-8"))
    items = stands["stands"] if isinstance(stands, dict) else stands
    owners: set[str] = set()
    for s in items:
        for part in (s.get("owner_name") or "").split(","):
            t = part.split("(")[0].strip()
            if t:
                owners.add(t)
    non_owner = chars - owners
    check("★ 存在「不是任何替身持有者但已入库」的角色（盲区已填）",
          len(non_owner) > 100, f"{len(non_owner)} 个")

    # ---- ③ 污染检查：不能把替身页/列表页当角色抓进来 ----
    print("\n[3] 分类名单不干净 —— 污染必须被挡住")
    stand_titles = {s.get("name_en") for s in items}
    polluted = [t for t in chars if t in stand_titles]
    check("★ 没有把替身页同时当角色抓（污染 0）", not polluted, str(polluted[:5]))
    list_pages = [t for t in chars if re.search(r"/\s*Part\s*\d+$", t)]
    check("★ 列表页（Minor Characters/Part N 形状）被排除", not list_pages,
          str(list_pages[:5]))

    # ---- ④ 角色数据落库 ----
    print("\n[4] 角色档案")
    ext = json.loads((PROC / "characters_ext.json").read_text(encoding="utf-8"))
    cs = ext["characters"]
    ids = {c["character_id"] for c in cs}
    check("角色档案数 ≥350", len(cs) >= 350, f"实得 {len(cs)}")
    check("★ Lisa Lisa 在角色档案里", "lisa_lisa" in ids)
    ll = next((c for c in cs if c["character_id"] == "lisa_lisa"), None)
    check("★ Lisa Lisa 有简介原文（不是空壳）",
          bool(ll and ll.get("bio") and len(ll["bio"]) > 40))
    check("★ Lisa Lisa 能判部（第 1/2 部靠 mangadebut）",
          bool(ll and ll.get("part") in (1, 2)), f"part={ll and ll.get('part')}")
    # 驼峰误报修复：作品后缀不应被判成"两角色名粘连"
    check("★ 标题可疑（误报）为 0 —— 作品后缀 (JoJolion) 不算粘连",
          sum(1 for c in cs if c.get("suspect_title")) == 0,
          str([c["title"] for c in cs if c.get("suspect_title")][:4]))

    # ---- ⑤ 中文名（用户打中文也要能搜到） ----
    print("\n[5] 新角色要有中文名（人工核对，不自动音译）")
    pairs = json.loads((ROOT / "dataset" / "sources" / "zh_names"
                        / "zh_char_pairs.json").read_text(encoding="utf-8"))
    check("★ Lisa Lisa 有中文名（莉莎莉莎 / 丽莎丽莎）",
          bool(pairs.get("lisa_lisa")), str(pairs.get("lisa_lisa")))
    check("★ 乔纳森·乔斯达 有中文名", bool(pairs.get("jonathan_joestar")))
    check("★ 西撒·齐贝林 有中文名", bool(pairs.get("caesar_anthonio_zeppeli")))
    # 刻意不给的：非角色页不该有中文名
    check("★ 组织/列表页**没有**被塞中文名",
          not pairs.get("locacaca_organization")
          and not pairs.get("list_of_horses"))
    man = json.loads((ROOT / "dataset" / "sources" / "zh_names"
                      / "zh_char_m70_manual.json").read_text(encoding="utf-8"))
    ent = man.get("entries") or {}
    check("★ 每条中文名都带依据与可信度（可复核）",
          all("note" in v and "confidence" in v for v in ent.values()))
    check("★ 替身页不会被挂上角色中文名（id 校验拦下的）",
          "fu_fighters" not in pairs and "weather_report" not in pairs,
          "Foo Fighters / Weather Report 是替身，不该出现在角色中文名表")

    # ---- ⑥ 同名歧义：篇章 ----
    print("\n[6] ★ 同名歧义（location ↔ story arc）")
    check("篇章页已抓取（≥150）", len(arcs) >= 150, f"{len(arcs)} 个")
    arcs_f = PROC / "story_arcs.json"
    check("story_arcs.json 已生成", arcs_f.exists())
    if not arcs_f.exists():
        print("\n" + "=" * 74)
        print(f"✗ {len(FAILS)} 项未通过")
        return 1
    sa = json.loads(arcs_f.read_text(encoding="utf-8"))
    check("篇章数 ≥180", sa["n_arcs"] >= 180, f"{sa['n_arcs']}")
    check("★ 篇章能判出部号（不为全 None）",
          sum(1 for a in sa["arcs"] if a.get("part")) >= 100,
          str(sum(1 for a in sa["arcs"] if a.get("part"))))
    check("★ 篇章能给出章节区间", sum(1 for a in sa["arcs"] if a.get("chapters")) >= 150,
          str(sum(1 for a in sa["arcs"] if a.get("chapters"))))

    linked = [a for a in sa["arcs"] if a.get("location_id")]
    check("★ 识别出 location↔arc 同名对（实测 2 个）",
          len(linked) == 2, f"{len(linked)} 个：{[a['arc_id'] for a in linked]}")
    sh = next((a for a in linked if a["arc_id"] == "shakedown_road"), None)
    check("★ 勒索之路 对上的是篇章（而非把地点当篇章）",
          bool(sh) and sh["location_id"] == "shakedown_road")
    check("★ 勒索之路篇章 = 第 8 部 JJL 18-22",
          bool(sh) and sh.get("part") == 8
          and any("JJL" in c for c in sh.get("chapters") or []),
          f"part={sh and sh.get('part')} chapters={sh and sh.get('chapters')}")

    # arc_id 必须剥掉引号与 (Story Arc) 后缀 —— 否则用户打「勒索之路」匹配不上
    check("★ arc_id 剥掉了引号与 (Story Arc) 后缀",
          all(not a["arc_id"].count('"')
              and "story_arc" not in a["arc_id"].replace("story_arcs", "")
              for a in sa["arcs"][:60] if a["arc_id"]),
          str([a["arc_id"] for a in sa["arcs"][:5]]))
    check("★ 抓取层与构建层的 arc_id 规则一致（避免两边 id 对不上）",
          "_arc_id" in fa_src
          and "def _arc_id" in (ROOT / "dataset" / "sources" / "jojowiki"
                               / "build_locations.py").read_text(encoding="utf-8"))

    # ---- ⑦ 服务层：答句必须带同名提示 ----
    print("\n[7] 服务层给出同名提示（口径说清，不替用户猜）")
    sys.path.insert(0, str(ROOT / "services"))
    import locations as L
    ans, ev, card = L.answer("勒索之路的替身是什么")
    check("仍走 location_related（未破坏既有路由）",
          ans["type"] == "location_related", ans["type"])
    hint = ans.get("also_story_arc")
    check("★ 答案里带同名篇章提示", bool(hint))
    check("★ 提示里说清了口径差异",
          bool(hint and "地点" in hint.get("note", "")
               and "篇章" in hint.get("note", "")))
    check("★ 提示指向的篇章是第 8 部",
          bool(hint and hint.get("part") == 8), f"part={hint and hint.get('part')}")

    # 非同名地点不该被硬塞提示
    ans2, _, _ = L.answer("安杰罗岩是什么地方")
    check("★ 非同名地点不给多余提示（不硬造）",
          ans2.get("also_story_arc") is None,
          str(ans2.get("also_story_arc"))[:80])

    # ---- ⑧ 前端能展示提示 ----
    print("\n[8] 前端渲染")
    html = (ROOT / "api" / "static" / "index.html").read_text(encoding="utf-8")
    check("★ 前端渲染 also_story_arc", "also_story_arc" in html)
    check("★ 提示块有醒目样式（同名歧义不能藏在 meta 里）",
          "同名歧义" in html)

    print("\n" + "=" * 74)
    if FAILS:
        print(f"✗ {len(FAILS)} 项未通过：")
        for f in FAILS:
            print(f"    - {f}")
        return 1
    print("✓ M70 全部检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())

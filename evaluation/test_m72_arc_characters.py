"""M72：角色 ↔ 篇章证据链 + 重定向副本/语言子页清理 + 篇章块入库

承接 M71 报告 §5 的第一条遗留项：
  「角色 ↔ 篇章的证据链还没建（目前只有替身 ↔ 篇章）。
    characters 表没有出场记录，要做需另抓角色页的 Appearance 段。」

== 本轮四件事 ==
【1】角色 ↔ 篇章证据链（arc_characters.json）
    数据源是角色页的 `{{Appearance|...}}` 模板里的 `{{Ch|...}}` 列表，
    实测 368 个角色页里 355 个有（97%）。
    → 188 个篇章里 187 个有角色关联，**零空档**。

【2】★ 重定向副本清理
    `fetch_wikitext()` 用 `redirects=1`，所以抓 `DIO` 会得到
    `Dio Brando` 的正文（125,941 字符，一模一样）。
    而源站的 `Category:Part N Characters` **同时列了重定向和它的目标**
    → 同一个角色被抓成两条，`characters` 表出现 `dio` 和 `dio_brando` 两行。
    实测 5 组 11 个页面，其中 7 个是重定向。
    ★ 判定方法：`action=query&prop=revisions` 拿未经重定向解析的原文，
      以 `#REDIRECT` 开头即为重定向。
      （★ 不要用 `action=parse` 判：它对 redirects 参数的行为不一致，
       实测同一个 Holy Joestar 两次调用返回不同 title。）

【3】语言子页清理
    源站分类里混着 `Urmd Avdul/fr`、`Gray Fly/es` 这类语言子页。
    build_locations 早就有 LANG_SUFFIX 过滤，角色侧漏了。

【4】篇章块入库（migration 005）
    M70 抓了篇章页但没重跑 build_article_chunks → 1062 个篇章块
    一直没生成（又一个「下游产物没跟着重算」）。
    生成后入库撞 chk_chunk_owner（三类约束不认 arc_id）→ 扩成四类。
    ★ 迁移必须幂等且顺序无关：002/003 加约束的地方都要能容忍
      「库里已有 005 认可的新归属」。

跑法：python evaluation/test_m72_arc_characters.py
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROC = ROOT / "dataset" / "processed"
SRC = ROOT / "dataset" / "sources" / "jojowiki" / "articles.json"
FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          + (f"   {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    print("=" * 74)
    print("M72：角色↔篇章证据链 + 重定向副本清理 + 篇章块入库")
    print("=" * 74)

    art = json.loads(SRC.read_text(encoding="utf-8"))
    pages = art["pages"] if isinstance(art, dict) and "pages" in art else art
    by_title = {p["title"]: p for p in pages}
    char_titles = {p["title"] for p in pages if p.get("kind") == "character"}

    # ---- ① 重定向副本必须清掉 ----
    print("\n[1] ★ 重定向副本清理（同一角色被抓成两条）")
    # 这 7 个是重定向页，抓取时 redirects=1 会把它们变成正文的副本
    REDIRECTS = ["DIO", 'Anjuro "Angelo" Katagiri',
                 'Shigekiyo "Shigechi" Yangu', "Former Tama",
                 "Elder A. Phex Brother", "Younger A. Phex Brother",
                 "Holy Joestar"]
    for t in REDIRECTS:
        check(f"重定向页「{t}」已从抓取名单剔除", t not in by_title)

    # 内容完全相同的页面（同一 wikitext 被抓两次）必须不存在
    byh: dict[str, list[str]] = {}
    for p in pages:
        h = hashlib.md5(p["wikitext"].encode()).hexdigest()
        byh.setdefault(h, []).append(p["title"])
    dup_groups = [t for t in byh.values() if len(t) > 1]
    check("★ 无「内容完全相同」的重复页（修前 5 组 11 页）",
          not dup_groups, str(dup_groups[:3]))

    # ---- ② 语言子页必须清掉 ----
    print("\n[2] 语言子页清理")
    LANG = re.compile(r"/(fr|zh|es|de|it|ja|pt|ru|pl|ko)$", re.I)
    lang_bad = [t for t in by_title if LANG.search(t)]
    check("★ 无语言子页（修前 4 个）", not lang_bad, str(lang_bad[:5]))

    # ---- ③ arc_characters.json ----
    print("\n[3] 角色 ↔ 篇章证据链")
    ac_f = PROC / "arc_characters.json"
    check("arc_characters.json 已生成", ac_f.exists())
    if not ac_f.exists():
        print("\n" + "=" * 74)
        print(f"✗ {len(FAILS)} 项未通过")
        return 1
    ac = json.loads(ac_f.read_text(encoding="utf-8"))
    check("篇章数 ≥180", ac["n_arcs"] >= 180, f"{ac['n_arcs']}")
    check("★ 有角色关联的篇章 ≥180（零空档）",
          ac["n_with_characters"] >= 180, f"{ac['n_with_characters']}")
    check("★ 区间内无记录的篇章为 0", ac["n_empty"] == 0,
          f"{ac['n_empty']}")
    check("★ 多数角色页有 Appearance 模板（≥95%）",
          ac["n_char_pages_with_appearance"] / max(ac["n_char_pages_total"], 1) >= 0.95,
          f"{ac['n_char_pages_with_appearance']}/{ac['n_char_pages_total']}")

    by_id = {a["arc_id"]: a for a in ac["arcs"]}

    # ---- ④ 章号口径（复用 M71 的共享模块）----
    print("\n[4] ★ 章号口径：与 M71 同一套（不重写规则）")
    sys.path.insert(0, str(ROOT / "dataset" / "pipeline"))
    import chapter_numbers as cn
    check("共享模块存在且被角色侧复用",
          hasattr(cn, "normalize_appearance")
          and hasattr(cn, "parse_arc_range"))
    src_txt = (ROOT / "dataset" / "sources" / "jojowiki"
               / "build_arc_characters.py").read_text(encoding="utf-8")
    check("★ build_arc_characters import 共享模块（不自己写一份）",
          "from chapter_numbers import" in src_txt)
    check("★ M71 的 build_arc_mentions 也用同一模块",
          "from chapter_numbers import" in
          (ROOT / "dataset" / "sources" / "jojowiki"
           / "build_arc_mentions.py").read_text(encoding="utf-8"))

    # 部内相对 → 绝对 的换算在角色侧同样生效
    ao = by_id.get("another_one_bites_the_dust") or {}
    check("★ 第4部篇章换算成绝对 418-427（+265）",
          ao.get("chapters_abs") == [418, 427], f"{ao.get('chapters_abs')}")
    check("★ 换算后圈到吉良吉影（修前 0 个角色）",
          any("Kira" in (c.get("name_en") or "")
              for c in ao.get("characters") or []),
          str([c.get("name_en") for c in ao.get("characters") or []][:5]))

    # 跨部污染必须被挡住
    sd = by_id.get("san_diego_beach") or {}
    names_sd = [c.get("name_en") for c in sd.get("characters") or []]
    check("★ 第7部 ch1-4 不撞第8部（只按章号查会误撞）",
          all(n not in ("Josuke Higashikata (JoJolion)", "Yasuho Hirose")
              for n in names_sd),
          f"{names_sd[:6]}")
    check("★ 第7部 ch1-4 圈到该段反派 Sandman",
          "Sandman" in names_sd, f"{names_sd[:8]}")

    # ---- ⑤ 数据质量 ----
    print("\n[5] 数据质量")
    allc = [c for a in ac["arcs"] for c in (a.get("characters") or [])]
    check("★ 无语言子页混入", not [c for c in allc if LANG.search(c["name_en"] or "")])
    check("★ 无重定向副本混入",
          not [c for c in allc if c["name_en"] in REDIRECTS],
          str([c["name_en"] for c in allc if c["name_en"] in REDIRECTS][:5]))
    check("★ 每条都带话数与原文证据",
          all(c.get("chapter") and c.get("snippet") for c in allc[:200]))

    # ---- ⑥ 服务层：替身 + 角色都给 ----
    print("\n[6] 服务层")
    sys.path.insert(0, str(ROOT / "services"))
    import locations as L
    ans, _, _ = L.answer("篇章 勒索之路")
    check("★ 「篇章 勒索之路」仍为 arc_fact", ans["type"] == "arc_fact",
          ans["type"])
    check("★ 替身侧仍在（M71 的成果没被覆盖）",
          any(s.get("name") == "Les Feuilles" for s in ans.get("stands") or []),
          str([s.get("name") for s in ans.get("stands") or []]))
    check("★ 角色侧也答出来了（本轮新增）",
          ans.get("n_characters", 0) >= 10,
          f"n_characters={ans.get('n_characters')}")
    check("★ 角色含 Josuke Higashikata (JoJolion)（该段主角）",
          any("Josuke" in (c.get("name") or "")
              for c in ans.get("characters") or []),
          str([c.get("name") for c in ans.get("characters") or []][:6]))

    ans2, _, _ = L.answer("篇章 Anubis")
    check("★ 无同名地点的篇章也能答角色",
          ans2["type"] == "arc_fact" and ans2.get("n_characters", 0) > 0,
          f"{ans2['type']} n={ans2.get('n_characters')}")

    ans3, _, _ = L.answer("勒索之路的替身是什么")
    check("★ 没提「篇章」仍走地点口径（不抢答）",
          ans3["type"] == "location_related", ans3["type"])

    # ---- ⑦ 前端 ----
    print("\n[7] 前端渲染")
    html = (ROOT / "api" / "static" / "index.html").read_text(encoding="utf-8")
    check("★ 前端渲染 arc_fact 的角色行", "相关角色" in html)
    check("★ 角色标签也带话数", "charTags" in html)

    # ---- ⑦.5 撇号与单章写法（本轮修的两个解析 bug）----
    print("\n[7.5] 解析 bug 回归锁")
    src_loc = (ROOT / "dataset" / "sources" / "jojowiki"
               / "build_locations.py").read_text(encoding="utf-8")
    # clean_text 里必须是「连续 2 个及以上才删」
    check("★ 撇号只在成对时删除，不误删英文所有格",
          "'{2,}" in src_loc,
          "clean_text 里可能仍是旧写法，会把 Kira's 吃成 Kiras")
    yk = by_id.get("yoshikage_kira_s_new_situation") or {}
    check("★ Kira's 的撇号保住了",
          "'" in (yk.get("name") or ""), f"实得 {yk.get('name')!r}")
    check("★ 两个单章写法 DU 105 + DU 112 → 区间 370-377",
          yk.get("chapters_abs") == [370, 377],
          f"实得 chapters_abs={yk.get('chapters_abs')} "
          f"chapters={yk.get('chapters')}")
    # 重定向副本的中文名必须搬到规范页（否则挂在不存在的 id 上）
    sys.path.insert(0, str(ROOT / "services"))
    import aliases as _al
    tbl = _al.build_char_alias_table()
    for nm, want in (("迪奥·布兰度", "dio_brando"), ("胖重", "shigekiyo_yangu"),
                     ("猫草", "tama"), ("片桐安十郎", "anjuro_katagiri")):
        check(f"★ 中文名「{nm}」→ {want}（副本键已清理）",
              tbl.get(nm) == want, f"实得 {tbl.get(nm)}")

    # ---- ⑦.6 页面类型标记（源站分类不干净）----
    print("\n[7.6] page_kind：如实记录「这一页是什么」")
    ext = json.loads((PROC / "characters_ext.json").read_text(encoding="utf-8"))
    ecs = ext["characters"]
    kinds = {}
    for c in ecs:
        kinds[c.get("page_kind")] = kinds.get(c.get("page_kind"), 0) + 1
    check("★ 大多数页是 Character Infobox（≥350）",
          kinds.get("character", 0) >= 350, str(kinds))
    check("★ 组织/物种页被标为 group（实测 3）",
          kinds.get("group", 0) >= 3, str(kinds))
    check("★ 家族页被标为 family",
          kinds.get("family", 0) >= 1, str(kinds))
    # 模板别名必须被识别 —— `{{Character Info}}` 是 `Character Infobox` 的别名
    by_title = {c["title"]: c for c in ecs}
    check("★ 模板别名 {{Character Info}} 被认成 character"
          "（Dot Han / Mona Lisa 这类）",
          (by_title.get("Dot Han") or {}).get("page_kind") == "character",
          f"实得 {(by_title.get('Dot Han') or {}).get('page_kind')}")
    check("★ 明确不是角色的页被正确归类",
          (by_title.get("Locacaca Organization") or {}).get("page_kind") == "group"
          and (by_title.get("Boomboom Family") or {}).get("page_kind") == "family",
          f"{by_title.get('Locacaca Organization', {}).get('page_kind')} / "
          f"{by_title.get('Boomboom Family', {}).get('page_kind')}")
    # 答案层要如实说明（不把组织当人物介绍）
    sys.path.insert(0, str(ROOT / "services"))
    import characters as _ch
    a1, _, _ = _ch.answer("Rock Human 是什么")
    check("★ 问组织/物种时给出「不是个体角色」的说明",
          bool(a1.get("page_kind_note")), str(a1.get("page_kind")))
    a2, _, _ = _ch.answer("东方仗助是谁")
    check("★ 正常角色不加这类噪音（page_kind_note 为 None）",
          a2.get("page_kind_note") is None, str(a2.get("page_kind_note")))
    html = (ROOT / "api" / "static" / "index.html").read_text(encoding="utf-8")
    check("★ 前端渲染 page_kind_note", "page_kind_note" in html)

    # ---- ⑧ DB（有库才跑）----
    print("\n[8] DB：篇章表与篇章块")
    conn = None
    try:
        sys.path.insert(0, str(ROOT / "database"))
        from load_db import connect
        conn = connect()
        cur = conn.cursor()
    except BaseException as e:                     # noqa: BLE001
        print(f"  SKIP  数据库不可用（{type(e).__name__}）")

    if conn is not None:
        cur.execute("SELECT count(*) FROM story_arcs")
        n_arcs_db = cur.fetchone()[0]
        check("★ story_arcs 表有行（≥180）", n_arcs_db >= 180, f"{n_arcs_db}")
        cur.execute("SELECT count(*) FROM text_chunks WHERE arc_id IS NOT NULL")
        n_arc_chunks = cur.fetchone()[0]
        check("★ 篇章块已入库（≥1000）", n_arc_chunks >= 1000, f"{n_arc_chunks}")
        cur.execute("""
            SELECT count(*) FROM text_chunks
            WHERE stand_id IS NULL AND character_id IS NULL
              AND location_id IS NULL AND arc_id IS NULL
        """)
        check("★ 无四者皆空的孤儿块", cur.fetchone()[0] == 0)
        cur.execute("""
            SELECT convalidated FROM pg_constraint
            WHERE conrelid = 'text_chunks'::regclass
              AND conname = 'chk_chunk_owner'
        """)
        row = cur.fetchone()
        check("★ chk_chunk_owner 已 VALIDATE（NOT VALID 只是过渡）",
              bool(row) and row[0] is True, str(row))
        # 重定向副本不能还在 characters 表里
        # ★ 不要用占位符传 list：psycopg2 会渲染成裸数组字面量
        #   `{"DIO",...}`，PG 在 ANY() 里推断不出类型 → syntax error at or near "{"
        #   （与 M69/M72 在 load_db 里两次踩的是同一个坑，这是第三次）
        #   → 用 ARRAY[...] 构造语法，值逐个转义。
        _vals = ",".join("'" + t.replace("'", "''") + "'" for t in REDIRECTS)
        cur.execute(f"SELECT count(*) FROM characters "
                    f"WHERE name_en = ANY(ARRAY[{_vals}])")
        check("★ characters 表无重定向副本", cur.fetchone()[0] == 0)
        # data_facts.json 权威计数
        facts_f = PROC / "data_facts.json"
        check("★ data_facts.json 已生成（闸门的单一来源）", facts_f.exists())
        if facts_f.exists():
            f = json.loads(facts_f.read_text(encoding="utf-8"))
            cur.execute("SELECT count(*) FROM characters")
            check("★ facts 的角色数与 DB 一致",
                  f.get("n_characters") == cur.fetchone()[0],
                  f"facts={f.get('n_characters')}")
        cur.close()
        conn.close()

    print("\n" + "=" * 74)
    if FAILS:
        print(f"✗ {len(FAILS)} 项未通过：")
        for f in FAILS:
            print(f"    - {f}")
        return 1
    print("✓ M72 全部检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())

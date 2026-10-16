"""M68：补抓「从没进过抓取队列」的角色页（抓取层 target 名单粘连的遗留）

背景（M66 报告 §9 / M67 报告 §8 记的遗留项）：
  M67 修好了 `stands.json` 的 owner 粘连，但**抓取层的 target 名单**是
  运行时从 stands.json 生成的 —— 在 M67 之前，名单里一直是那个
  「粘成一串」的名字（wiki 上不存在）→ 永远抓不到 → 那些角色页
  **从来没有被抓过**，永久缺失。

本轮：
  ① 查清 18 个待抓角色里哪些 wiki 真实存在（12 个）、哪些没有独立条目（6 个）
  ② 加**标题修正表** `TITLE_FIXUPS`（`Unnamed Mountain Lodge Owner`
     → `Mountain Lodge Owner`，源站少了 `Unnamed ` 前缀）
     ★ 修正只换**页面标题**，**id 仍按原名算**（DB 的 character_id 用原名）
  ③ 加 `NO_PAGE`：确认无独立条目的 6 个，**如实记录、不去抓、不造数据**
  ④ 修 `build_characters` 的 `suspect_title` 误报（`Thunder McQueen` 的
     `cQ` 驼峰被当成「两角色名粘连」——与 M67「不用驼峰边界判粘连」同教训）

跑法：python evaluation/test_m68_owner_pages.py
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services"))

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          + (f"   {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    print("=" * 74)
    print("M68：补抓角色页（target 名单粘连的遗留）")
    print("=" * 74)

    import importlib.util as u
    sp = u.spec_from_file_location(
        "fa", ROOT / "dataset" / "sources" / "jojowiki" / "fetch_articles.py")
    fa = u.module_from_spec(sp)
    sp.loader.exec_module(fa)

    # ---- ① 两张人工表存在且内容正确 ----
    print("\n[1] 抓取器的两张人工确认表")
    check("TITLE_FIXUPS 存在且非空", bool(fa.TITLE_FIXUPS),
          f"{fa.TITLE_FIXUPS}")
    check("★ 修正表的键值确实不同（否则是无效条目）",
          all(k != v for k, v in fa.TITLE_FIXUPS.items()))
    check("NO_PAGE 存在且非空", bool(fa.NO_PAGE), f"{sorted(fa.NO_PAGE)}")
    check("★ 两张表没有交集（同一个名字不能既修正又跳过）",
          not (set(fa.TITLE_FIXUPS) & set(fa.NO_PAGE)))
    check("★ 修正表的值不在 NO_PAGE 里（否则修了也白修）",
          not (set(fa.TITLE_FIXUPS.values()) & set(fa.NO_PAGE)))

    # ---- ② target 名单干净 ----
    print("\n[2] target 名单：无粘连、无已知缺失")
    targets = fa.build_targets()
    chars = [t for t in targets if t["kind"] == "character"]
    titles = {t["title"] for t in chars}
    # ★ 180 = 探测时的 182 扣掉 NO_PAGE 的 6 个 + 修正表换名的 1 个仍算 1 个
    #   （`Unnamed Mountain Lodge Owner` → `Mountain Lodge Owner`，数量不变）
    check(f"角色 target {len(chars)} 个（≥180）", len(chars) >= 180,
          f"实际 {len(chars)}")
    glued = [t["title"] for t in chars
             if re.search(r"[a-z][A-Z]", t["title"])
             and not re.search(r"(Mc|Mac)[A-Z]", t["title"])]
    check("★ 名单里没有粘连名（排除 Mc/Mac 误报）", not glued, f"{glued}")
    check("★ M66/M67 报告里那 3 个长粘连名已消失",
          not any("BoomboomL.A." in t or "SeraiChaka" in t
                  or "Victim Thunder" in t for t in titles),
          f"{[t for t in titles if 'BoomboomL.A.' in t or 'SeraiChaka' in t]}")
    for n in sorted(fa.NO_PAGE):
        check(f"NO_PAGE 成员不在名单里：{n}", n not in titles)
    check("★ 修正后的页面名在名单里",
          all(v in titles for v in fa.TITLE_FIXUPS.values()),
          f"缺：{[v for v in fa.TITLE_FIXUPS.values() if v not in titles]}")

    # ---- ③ ★ id 与 DB 对齐（标题修正不能顺手改主键）----
    print("\n[3] ★ 标题修正后 id 仍按原名算（与 DB 一致）")
    # ★★ 注意：`load_db` 的**导入本身**就可能触发连接自检并打印排查清单、
    #    甚至抛异常（实测 2026-10-16 Docker Desktop 未启动时就是这样）。
    #    → import 也要包在 try 里，否则连「降级跳过」都走不到。
    db_ids: set[str] = set()
    db_ok = False
    try:
        sys.path.insert(0, str(ROOT / "database"))
        from load_db import connect, _char_id          # noqa: E402
    except BaseException as e:   # noqa: BLE001  # ★ 见下方说明
        print(f"  SKIP  load_db 导入失败（{type(e).__name__}）—— DB 相关断言跳过")
        _char_id = None
    else:
        try:
            cur = connect().cursor()
            cur.execute("SELECT character_id FROM characters")
            db_ids = {r[0] for r in cur.fetchall()}
            cur.close()
            db_ok = True
        # ★★ 必须写 `BaseException`，不是 `Exception` ★★
        #   `load_db.connect()` 连不上时抛的是 **`SystemExit(1)`**
        #   （第 96 行 `raise SystemExit(1) from e`），而 SystemExit
        #   **不继承 Exception**（它继承 BaseException）→
        #   `except Exception` 抓不到 → 整个测试被静默终止，
        #   连「降级跳过」的 SKIP 行都打不出来。
        except BaseException as e:   # noqa: BLE001
            print(f"  SKIP  DB 连接失败（{type(e).__name__}）—— "
                  f"DB 相关断言跳过，其余照跑（Docker Desktop 是否已启动？）")
    if db_ok:
        for old, new in fa.TITLE_FIXUPS.items():
            t = next((x for x in chars if x["title"] == new), None)
            check(f"★ {old!r} → 页面名 {new!r}，id 仍是 {_char_id(old)!r}",
                  bool(t) and t["entity_id"] == _char_id(old)
                  and t["entity_id"] in db_ids,
                  f"target={t} in_db={bool(t) and t['entity_id'] in db_ids}")
            check(f"★ {old!r} 的 target 带了原名标注（便于复核）",
                  bool(t) and t.get("name_in_stands") == old,
                  f"name_in_stands={t.get('name_in_stands') if t else None}")
        # 全部角色 target 的 id 都该在 DB 里
        miss = [t["title"] for t in chars if t["entity_id"] not in db_ids]
        check("★ 所有角色 target 的 entity_id 都在 DB characters 里",
              not miss, f"对不上的（前 5）：{miss[:5]}")
    else:
        # ★ 即使没 DB，也要能验「id 按原名算」这条（它不依赖 DB）
        if _char_id is not None:
            for old, new in fa.TITLE_FIXUPS.items():
                t = next((x for x in chars if x["title"] == new), None)
                check(f"★ {old!r} 的 entity_id 按原名算（{_char_id(old)!r}）",
                      bool(t) and t["entity_id"] == _char_id(old),
                      f"target={t} 期望 id={_char_id(old)!r}")
        else:
            # 连 _char_id 都导入不到时，用**语料页自带的 entity_id** 反验
            # （target 与 articles.json 里的 page 必须一致）
            art_ids = {pp.get("entity_id"): pp.get("title")
                       for pp in json.loads((ROOT / "dataset" / "sources"
                                             / "jojowiki" / "articles.json")
                                            .read_text(encoding="utf-8"))["pages"]}
            t = next((x for x in chars if x["title"] in art_ids.values()), None)
            check("★ 抓取页的 entity_id 与语料页一致",
                  bool(t) and art_ids.get(t["entity_id"]) == t["title"],
                  f"target={t}")

    # ---- ④ 语料里真的有那 12 个新页面 ----
    print("\n[4] 语料：新页面已抓进来")
    art = json.loads((ROOT / "dataset" / "sources" / "jojowiki"
                      / "articles.json").read_text(encoding="utf-8"))
    have = {p["title"] for p in art["pages"] if p.get("kind") == "character"}
    check("语料角色页 ≥180", len(have) >= 180, f"实际 {len(have)}")
    NEW = ["Caravan Serai", "Chaka", "Khan", "Thunder McQueen",
           "Sports Maxx", "Miraschon", "Viviano Westwood",
           "Boomboom Family", "Andre Boomboom", "L.A. Boomboom",
           "Dixie Flatline", "Mountain Lodge Owner"]
    for n in NEW:
        check(f"  已抓到：{n}", n in have)
    lodge = next((p for p in art["pages"] if p["title"] == "Mountain Lodge Owner"),
                 None)
    check("★ 被抓页带 entity_id（= DB 里的原名 id）",
          bool(lodge) and lodge.get("entity_id") == "unnamed_mountain_lodge_owner",
          f"entity_id={lodge.get('entity_id') if lodge else None}")

    # ---- ⑤ 下游：角色档案与语料块都长起来了 ----
    print("\n[5] 下游产物随之增长")
    import characters as CH
    d = CH.load()
    check("角色档案 ≥180", d["n"] >= 180, f"实际 {d['n']}")
    check("★ 简介覆盖 ≥179", d["n_with_bio"] >= 179, f"实际 {d['n_with_bio']}")
    chunks = json.loads((ROOT / "dataset" / "processed"
                         / "article_chunks.json").read_text(encoding="utf-8"))
    n_char_chunks = sum(1 for c in chunks
                        if (c.get("extra") or {}).get("kind") == "character")
    check("角色语料块 ≥3063", n_char_chunks >= 3063,
          f"实际 {n_char_chunks}")
    # 语料块引用的 character_id 必须都在 DB 里（外键）
    if db_ok:
        bad = sorted({c["character_id"] for c in chunks
                      if c.get("character_id") and c["character_id"] not in db_ids})
        check("★ 语料块的 character_id 全部在 DB 里", not bad, f"对不上：{bad[:5]}")
    else:
        # 没 DB 时退一步：至少验**块内自洽**（同一 character_id 的块
        # 都在同一个角色下），这条不依赖 DB 也能抓真问题。
        by_cid: dict[str, set[str]] = {}
        for c in chunks:
            if c.get("character_id"):
                by_cid.setdefault(c["character_id"], set()).add(
                    c.get("stand_name") or "")
        multi = {k: v for k, v in by_cid.items() if len(v) > 1}
        check("★ 语料块内自洽（同一 character_id 只有一个 stand_name）",
              not multi, f"异常的：{list(multi.items())[:3]}")
        print("  SKIP  「character_id 在 DB 里」—— DB 不可用")

    # ---- ⑥ suspect_title 误报已修 ----
    print("\n[6] McQueen 这类正常驼峰名不再被误判")
    src = (ROOT / "dataset" / "sources" / "jojowiki"
           / "build_characters.py").read_text(encoding="utf-8")
    check("★ suspect_title 判据排除了 Mc/Mac 前缀",
          "Mc|Mac" in src or "Mc[A-Z]" in src)
    ext = d["by_id"]
    check("★ Thunder McQueen 的 suspect_title 为 False",
          not (ext.get("thunder_mcqueen") or {}).get("suspect_title"))
    check("★ 全库 suspect_title 为 0",
          sum(1 for r in ext.values() if r.get("suspect_title")) == 0,
          f"实际 {sum(1 for r in ext.values() if r.get('suspect_title'))}")

    # ---- ⑦ 源码级：不能又造出「静默失效」 ----
    print("\n[7] 源码级检查（防回归）")
    fa_src = (ROOT / "dataset" / "sources" / "jojowiki"
              / "fetch_articles.py").read_text(encoding="utf-8")
    # ★ 我第一版写成 `bool(ast.parse(...)) is False or True` —— 恒为真，
    #   等于没检查（正是本项目反复出现的"检查工具写错比代码错更危险"）。
    try:
        ast.parse(fa_src)
        ok_syntax = True
    except SyntaxError:
        ok_syntax = False
    check("fetch_articles.py 语法正确", ok_syntax)
    check("★ 角色 target 的 entity_id 用 `orig`（原名）而非修正后的 `o`",
          '"entity_id": _char_id(orig)' in fa_src
          or "_char_id(orig)" in fa_src,
          "若用 `o`，标题一改主键就变 → 与 DB 脱节")
    check("★ build_characters 用 page 自带的 entity_id",
          'p.get("entity_id")' in src)
    check("★ build_characters 的 name_en 用原名（name_in_stands）",
          "name_in_stands" in src)

    print()
    if FAILS:
        print(f"✗ {len(FAILS)} 项失败：" + "、".join(FAILS[:6]))
        return 1
    print("✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())

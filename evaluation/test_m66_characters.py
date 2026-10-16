"""M66 回归测试：人物背景维度（角色档案 / 简介 / 背景 / 性格 / 部数口径）。

用户最初的诉求：
  「现在要不要补一下人物的背景 这样12部都能加进去了 并且也能做一些更多的内容
    如安杰罗岩 勒索之路 恶魔掌心这些特色地点」
→ M65 做了**地点**；本轮补上**人物背景**这一半：
  「东方仗助是谁」这类问句此前**答不了**（角色页一直在语料里，
   但 characters 表只有 5 个字段、没有简介）。

== 本测试守什么 ==
  1. 数据规模与覆盖：168 个角色 · 简介 168 · 背景 106 · 性格 162
  2. ★ 三段原文都必须**能回查语料块**（chunk_id）—— 证据可追溯
  3. ★ 解析后字段**无残留 wiki 标记**（M65 的"脏值"同类，逐字段自检）
  4. ★ 部数口径：DB 优先；与"首次出场推断"不一致时**必须给出说明**
     （实测 9 个角色如此，如承太郎在第 2 部末客串）
  5. 路由：角色问句走 character；**替身问句与 owner 查询不受影响**；
     无关问句仍拒答（M21 守门）
  6. ★ 前端不用内联 onclick 传名字（名字里有撇号 `D'Arby` 会提前闭合属性）
     —— 改用 data-ask + 事件委托；M65 地点列表那处隐患也一并守
  7. 端到端：角色卡字段齐全、使用者可点、撇号条目可点
"""
from __future__ import annotations

import http.client
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
PROC = ROOT / "dataset" / "processed"
SRC = ROOT / "dataset" / "sources" / "jojowiki"
SERVICES = ROOT / "services"
HTML = ROOT / "api" / "static" / "index.html"
MAINPY = ROOT / "api" / "main.py"
PORT = 8808


def wait_http(port: int, timeout: float = 240) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            c.request("GET", "/health")
            if c.getresponse().status == 200:
                return True
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
        time.sleep(2)
    return False


def query(q: str, port: int) -> dict:
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    try:
        c.request("POST", f"/query?q={quote(q)}&top_k=3",
                  headers={"Content-Type": "application/json"})
        return json.loads(c.getresponse().read())
    finally:
        c.close()


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

    sys.path.insert(0, str(SERVICES))
    import characters as CH                       # noqa: E402
    from conflict_resolver import Router          # noqa: E402

    print("=" * 74)
    print("M66：人物背景（角色档案 / 原文三段 / 部数口径 / 路由 / 前端）")
    print("=" * 74)

    ext = PROC / "characters_ext.json"
    if not ext.exists():
        print("  ⏭ 缺少 characters_ext.json"
              "（先跑 dataset/sources/jojowiki/build_characters.py）—— 已跳过")
        return 0
    raw = json.loads(ext.read_text(encoding="utf-8"))
    chars = raw.get("characters") or []
    data = CH.load()

    # ---- ① 规模与覆盖 ----
    #   ★ M68：数字从 168 → 180（补抓了 M67 修 owner 粘连后暴露出的
    #     12 个「从没进过抓取队列」的角色页）。
    #     ★ 断言写「≥」而不是「==」：这类**规模数字会随补抓增长**，
    #       钉死具体值意味着每补一批数据就要改一次测试（M64 的教训：
    #       断言绑死具体数字 → 新环境/新数据下假失败）。
    print("\n[1] 数据：规模与字段覆盖")
    check("角色 ≥168 个（M68 补抓后 180）", data["n"] >= 168,
          f"实际 {data['n']}")
    check("★ 简介（首段）覆盖 ≥168/168", data["n_with_bio"] >= 168,
          f"实际 {data['n_with_bio']}")
    check("背景节 ≥106（源站只有部分条目有该节）",
          data["n_with_background"] >= 106, f"实际 {data['n_with_background']}")
    check("性格节 ≥162", data["n_with_personality"] >= 162,
          f"实际 {data['n_with_personality']}")
    check("中文名 ≥123（既有别名系统的角色中文名）", data["n_with_zh"] >= 123,
          f"实际 {data['n_with_zh']}")

    # ---- ①′ ★ M68：标题修正后 id 仍与 DB 对齐 ----
    #   抓取层把 `Unnamed Mountain Lodge Owner` 的页面标题换成 wiki 真实名
    #   `Mountain Lodge Owner`（源站少了 `Unnamed ` 前缀），但 **id 必须仍按
    #   原名算** —— DB 的 `characters.character_id` 用的是原名。
    #   ★ 这条断言防的是「改标题顺手改了主键」那类错：
    #     它不报错，只会让中文名/块号/关联**全部静默对不上**。
    print("\n[1′] ★ 标题修正后 id 仍与 DB 对齐（M68）")
    # ★★ M68：必须 `BaseException` ★★ —— `load_db.connect()` 连不上时抛的是
    #   **`SystemExit(1)`**（不继承 Exception）。原先这里连 try 都没有，
    #   DB 一不可用整个测试直接终止，后面 40+ 条断言全看不到。
    #   ★ 实测（2026-10-16 Docker Desktop 未启动）：就是这样死的。
    import sys as _sys
    _sys.path.insert(0, str(ROOT / "database"))
    _db: dict = {}
    try:
        from load_db import connect as _connect   # noqa: E402
        _c = _connect().cursor()
        _c.execute("SELECT character_id, name_en FROM characters")
        _db = {a: b for a, b in _c.fetchall()}
        _c.close()
    except BaseException as e:   # noqa: BLE001
        print(f"  SKIP  DB 不可用（{type(e).__name__}）—— [1′] 段 DB 对比跳过，"
              f"非 DB 断言照跑（Docker Desktop 是否已启动？）")
    ext = data["by_id"]
    lodge = ext.get("unnamed_mountain_lodge_owner")
    if _db:
        check("★ 修正标题的角色：id 用原名（与 DB 一致）",
              bool(lodge) and lodge["character_id"] in _db,
              f"ext={bool(lodge)} in_db={bool(lodge) and lodge['character_id'] in _db}")
        check("★ 修正标题的角色：name_en 用原名（与 DB 一致）",
              bool(lodge) and _db.get(lodge["character_id"]) == lodge["name_en"],
              f"DB={_db.get('unnamed_mountain_lodge_owner')!r} "
              f"ext={lodge['name_en'] if lodge else None!r}")
    else:
        check("★ 修正标题的角色：id 是原名 id（无 DB，退一层验）",
              bool(lodge) and lodge["character_id"] == "unnamed_mountain_lodge_owner",
              f"id={lodge['character_id'] if lodge else None!r}")
    check("★ 修正标题的角色：title 是**真实页面名**（可点回源站）",
          bool(lodge) and lodge["title"] == "Mountain Lodge Owner",
          f"title={lodge['title'] if lodge else None!r}")
    check("★ 修正标题的角色有简介与部信息",
          bool(lodge) and bool(lodge.get("bio")) and lodge.get("part") is not None,
          f"bio={bool(lodge and lodge.get('bio'))} part={lodge and lodge.get('part')}")
    if _db:
        check("★ 所有 characters_ext 的 id 都在 DB 里（外键能对上）",
              all(k in _db for k in ext),
              f"对不上的：{[k for k in ext if k not in _db][:5]}")
    else:
        print("  SKIP  「id 都在 DB 里」—— DB 不可用")
    # 驼峰误报：Thunder McQueen 的 `cQ` 曾被判成「疑两角色名粘连」
    check("★ McQueen 这类正常驼峰名不再被误判为「粘连」",
          not (ext.get("thunder_mcqueen") or {}).get("suspect_title"),
          f"suspect_title={(ext.get('thunder_mcqueen') or {}).get('suspect_title')}")
    check("★ 标题可疑数为 0（M68 修掉 McQueen 误报后）",
          sum(1 for r in ext.values() if r.get("suspect_title")) == 0,
          f"实际 {sum(1 for r in ext.values() if r.get('suspect_title'))}")

    # ---- ② ★ 三段原文要能回查语料块 ----
    print("\n[2] ★ 每段原文都能回查语料块（证据可追溯）")
    miss = [c["title"] for c in chars
            if c.get("bio") and not c.get("bio_chunk_id")]
    check("★ 有简介的角色都能给出 bio_chunk_id", not miss,
          f"缺 {len(miss)} 个：{miss[:3]}")
    #   ★ M70 留了一个容差：源站 `Urmd Avdul` 的 Background 段只有一句话
    #     （"Urmd Avdul is a racer who hails from Egypt…"），
    #     切块时按长度阈值被丢弃 → 拿不到 chunk_id。
    #     这是**源站内容太短**的真实情况，不是解析缺陷 ——
    #     所以断言写成「有背景节的绝大多数能回查」，并把例外显式列出，
    #     而不是要求 100%（要求 100% 就会逼着代码去编造一个块号）。
    _BG_MIN = 150          # 正文字符数低于此值视为「源站段落过短」
    miss2 = [c["title"] for c in chars
             if c.get("background") and not c.get("background_chunk_id")
             and len(c.get("background") or "") >= _BG_MIN]
    _short = [c["title"] for c in chars
              if c.get("background") and not c.get("background_chunk_id")
              and len(c.get("background") or "") < _BG_MIN]
    check("★ 有背景节（且段长足够）的角色都能回查 background_chunk_id",
          not miss2,
          f"缺 {len(miss2)} 个：{miss2[:3]}"
          f"；另有 {len(_short)} 个因**源站段落过短**（<{_BG_MIN} 字）"
          f"无块号，属真实情况：{_short[:3]}")
    a, ev, k = CH.answer("东方仗助是谁")
    check("★ 角色答案带证据（每条含 chunk_id + 原文）",
          bool(ev) and all(e.get("chunk_id") and e.get("content") for e in ev),
          str([(e.get("type"), e.get("chunk_id")) for e in ev])[:100])

    # ---- ③ ★ 脏值自检（M65 同类）----
    print("\n[3] ★ 解析后字段无残留 wiki 标记")
    PARSED = ["name_ja", "name_romaji", "gender", "age", "birthday", "height",
              "weight", "blood", "status", "aliases", "nations",
              "occupations", "families", "affiliations", "seiyuus",
              "voiceactors", "namesakes", "stands",
              "bio", "background", "personality"]
    bad: list[tuple[str, str, str]] = []
    for c in chars:
        for key in PARSED:
            v = c.get(key)
            vals = v if isinstance(v, list) else ([v] if v else [])
            for s in vals:
                if isinstance(s, str) and re.search(
                        r"\{\{|\}\}|\[\[|\]\]|<ref|<br|https?://", s):
                    bad.append((c["title"], key, s[:50]))
                    break
    check("★ 残留 wiki 标记为 0（含 URL / 半截模板）", not bad,
          f"{len(bad)} 处：{bad[:2]}")
    # 「空字段吞下一行」那条也要守（M65 坑 5）
    check("★ 取值正则的空白类不含换行（[^\\S\\n]）",
          "[^\\S\\n]" in (SRC / "build_characters.py").read_text(encoding="utf-8"))
    # 具体案例：DIO 的身高是两段值，不能被吞成一行脏值
    dio = next((c for c in chars if c["title"] == "DIO"), None)
    check("DIO 身高解析出两个值（185 / 195），不是脏串",
          bool(dio) and "185" in (dio.get("height") or "")
          and "195" in (dio.get("height") or ""),
          str(dio.get("height") if dio else None))

    # ---- ④ 部数口径 ----
    print("\n[4] ★ 部数：DB 优先 + 差异说明")
    src = (SRC / "build_characters.py").read_text(encoding="utf-8")
    check("脚本复用地点那边的判部函数（不复制一套）",
          "from build_locations import part_of" in src)
    check("服务层有 DB 优先 + 差异说明的实现",
          "_part_of_char" in (SERVICES / "characters.py").read_text(encoding="utf-8")
          and "主要登场为第" in (SERVICES / "characters.py").read_text(encoding="utf-8"))
    # 承太郎：源站首次出场在第 2 部末，DB 是第 3 部
    jt = next((c for c in chars if c["title"] == "Jotaro Kujo"), None)
    check("承太郎的推断部数与 DB 不一致（说明这套兜底是必要的）",
          bool(jt) and jt.get("part") == 2, f"推断 {jt.get('part') if jt else None}")
    _, _, kc = CH.answer("空条承太郎是谁")     # 不带 DB map → 用推断值
    check("不带 DB 时退化为推断值（第 2 部）", kc["part"] == 2,
          f"实际 {kc['part']}")
    _, _, kc2 = CH.answer("空条承太郎是谁", part_map={"jotaro_kujo": 3})
    check("★ 带 DB map 时用 DB 值，并给出差异说明",
          kc2["part"] == 3 and kc2.get("part_note"),
          f"part={kc2['part']} note={kc2.get('part_note')}")

    # ---- ⑤ 路由 ----
    print("\n[5] 路由：角色 / 替身 / 守门")
    import locations as L                        # noqa: E402
    r = Router(known_stands={"Star Platinum", "Crazy Diamond", "Tusk",
                             "Soft & Wet", "November Rain"},
               known_entities={"Star Platinum", "Crazy Diamond", "Tusk",
                               "Soft & Wet", "November Rain", "Jotaro Kujo"},
               known_locations=set(L.all_names()),
               known_characters=set(CH.all_names()))
    for q, want in [("东方仗助是谁", "character"),
                    ("空条承太郎是谁", "character"),
                    ("谁是东方仗助", "character"),
                    ("第 4 部有哪些角色", "character"),
                    ("有哪些角色", "character")]:
        d = r.route(q)
        check(f"「{q}」→ {want}", d.route == want, f"实际 {d.route}")
    # ★ 回归：这些**不能**被角色路由抢走
    for q, want in [("Jotaro Kujo的替身是什么", "structured"),
                    ("这个替身的使用者是谁", "structured"),
                    ("Star Platinum 的使用者是谁", "structured"),
                    ("November Rain 的使用者是谁", "structured"),
                    ("Star Platinum 的破坏力是几级", "structured")]:
        d = r.route(q)
        check(f"回归：「{q}」仍是 {want}", d.route == want, f"实际 {d.route}")
    # ★ 守门
    for q in ["今天天气怎么样", "华盛顿在哪里", "甘道夫的替身是什么",
              "第 10 部有哪些角色"]:
        d = r.route(q)
        check(f"守门：「{q}」仍拒答", d.route == "abstain", f"实际 {d.route}")

    # ---- ⑥ 接线 ----
    print("\n[6] 接线：解析脚本 / 服务层 / API / 前端")
    check("有角色解析脚本", (SRC / "build_characters.py").exists())
    check("有角色服务模块", (SERVICES / "characters.py").exists())
    mainpy = MAINPY.read_text(encoding="utf-8")
    check("API 有 character 分支", 'route == "character"' in mainpy)
    check("API 响应带 character_card", '"character_card": character_card' in mainpy)
    check("Router 收 known_characters",
          "known_characters: Optional[set[str]] = None" in
          (SERVICES / "conflict_resolver.py").read_text(encoding="utf-8"))
    html = HTML.read_text(encoding="utf-8")
    check("前端有角色卡渲染", "function renderCharacterPanel" in html)
    check("前端渲染四种角色答案类型",
          all(f"t==='{x}'" in html for x in
              ("character_fact", "character_list", "character_stands",
               "character_hint")))
    check("★ 使用者可点（跳到角色卡）",
          'data-ask="${esc(zc.owner' in html and "cc-link" in html)
    # ★ 防回归：不允许再用内联 onclick 传名字（撇号会提前闭合属性）
    #   ★ 先去掉注释行 —— 解释这个坑的注释里就含这种写法（M65 被自己的注释绊过）
    code = "\n".join(ln for ln in html.splitlines()
                     if not ln.lstrip().startswith("//"))
    check("★ 前端没有「内联 onclick 传名字」的写法（全走 data-ask）",
          not re.search(r"onclick=['\"]ask\(\$\{JSON", code)
          and "data-ask=" in code)

    # ---- ⑦ 端到端 ----
    print(f"\n[7] 端到端（127.0.0.1:{PORT}）")
    log = (ROOT / ".tmp_preview" / "_m66_server.log").open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "api" / "main.py"),
         "--port", str(PORT), "--no-vector"],
        cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL)
    try:
        if not wait_http(PORT):
            check("服务就绪", False, "超时")
            return 1
        # ★★ M68：探测 DB 是否真的连上（无 DB 时优雅 SKIP，不判失败）★★
        #   服务在无 DB 时会降级（`/health` 报 `database=disconnected`）。
        #   ★ 惯例同 test_gui_e2e / test_m40_part_list：
        #     「⏭ 已跳过（环境不满足）」。`--require-db` / `CI=true` 时严格。
        try:
            import urllib.request as _u
            _h = json.loads(_u.urlopen(
                f"http://127.0.0.1:{PORT}/health", timeout=15).read())
            _DB_OK = (_h.get("database") == "connected")
        except Exception:                          # noqa: BLE001
            _DB_OK = False
        if not _DB_OK:
            print("=" * 66)
            print("⏭ 已跳过（环境不满足）：数据库没连上")
            print("    启动方式：cd docker && docker compose up -d postgres")
            if ("--require-db" in sys.argv) or (os.environ.get("CI") == "true"):
                print("    （--require-db / CI=true：缺库视为失败）")
                return 1
            print("    （未开 --require-db，按「跳过」处理，不计失败）")
            print("=" * 66)
            return 0
        d = query("东方仗助是谁", PORT)
        cc = d.get("character_card") or {}
        check("「东方仗助是谁」route=character",
              d.get("route") == "character", f"实际 {d.get('route')}")
        check("答案类型 = character_fact",
              d.get("answer_type") == "character_fact",
              f"实际 {d.get('answer_type')}")
        check("卡片给出中文名/日文名/第 4 部",
              cc.get("name_zh") == "东方仗助"
              and cc.get("name_ja") == "東方 仗助"
              and cc.get("part") == 4, str(cc)[:110])
        check("卡片给出替身与事实字段（性别/年龄/身高）",
              cc.get("stand_names") == ["Crazy Diamond"]
              and cc.get("gender") == "Male" and cc.get("height") == "180",
              str({k: cc.get(k) for k in ("stand_names", "gender", "height")}))
        check("★ 卡片带简介原文 + 语料块 id",
              bool(cc.get("bio")) and isinstance(cc.get("bio_chunk_id"), int),
              f"chunk={cc.get('bio_chunk_id')}")
        check("简介是英文原文（未机翻）",
              bool(re.search(r"[A-Za-z]{4,}\s+[A-Za-z]{4,}", cc.get("bio") or "")))
        check("★ missing 如实标注源站未提供的字段",
              isinstance(cc.get("missing"), list) and "background" in cc["missing"],
              str(cc.get("missing")))
        check("证据含 character_corpus（带块 id）",
              any(e.get("type") == "character_corpus" and e.get("chunk_id")
                  for e in (d.get("evidence") or [])),
              str([(e.get("type"), e.get("chunk_id"))
                   for e in (d.get("evidence") or [])])[:110])

        d2 = query("第 4 部有哪些角色", PORT)
        a2 = d2.get("answer") or {}
        # ★ M70：数字 25 → 49（补抓了 196 个「不是替身持有者」的角色后，
        #   第 4 部角色从 25 涨到 49）。写死具体数字会在数据增长时误报，
        #   改为「列表非空 + 覆盖合理区间」，既守住回归又不绑死规模。
        _n4 = a2.get("count") or 0
        check("「第 4 部有哪些角色」返回列表（M70 后 49 个）",
              d2.get("answer_type") == "character_list" and _n4 >= 40,
              f"type={d2.get('answer_type')} count={_n4}")

        d3 = query("迪亚哥·布兰度有什么替身", PORT)
        a3 = d3.get("answer") or {}
        check("「X 有什么替身」返回 character_stands",
              d3.get("answer_type") == "character_stands"
              and "Scary Monsters" in (a3.get("stands") or []),
              f"type={d3.get('answer_type')} stands={a3.get('stands')}")

        # ★ 回归：替身问句 + owner 查询 + 守门
        d4 = query("Star Platinum 的破坏力是几级？", PORT)
        a4 = d4.get("answer")
        a4 = a4[0] if isinstance(a4, list) and a4 else a4
        check("回归：替身六维仍是 fact",
              isinstance(a4, dict) and a4.get("type") == "fact", str(a4)[:90])
        d5 = query("Soft & Wet 的使用者是谁", PORT)
        a5 = d5.get("answer")
        check("回归：「Soft & Wet 的使用者是谁」仍答 owner",
              d5.get("route") == "structured"
              and isinstance(a5, dict) and a5.get("type") == "owner",
              f"route={d5.get('route')} type={a5.get('type') if isinstance(a5, dict) else a5}")
        check("守门：端到端「华盛顿在哪里」仍拒答",
              query("华盛顿在哪里", PORT).get("answer_type") == "abstain")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except Exception:  # noqa: BLE001
            proc.kill()
        log.close()

    print()
    if fails:
        print(f"✗ {len(fails)}/{n} 项失败：" + "、".join(fails))
        return 1
    print(f"✓ 全部通过（{n} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

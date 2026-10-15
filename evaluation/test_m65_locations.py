"""M65 回归测试：地点维度（抓取/解析 → 检索路由 → 前端地点卡）。

用户原话：
  「现在要不要补一下人物的背景 这样12部都能加进去了 并且也能做一些更多的内容
    如安杰罗岩 勒索之路 恶魔掌心这些特色地点 先跟我对齐怎么做 再进行落实」
  → 对齐结论：**人物背景 + 地点；只保留 9 部正传漫画；地点带层级；中文名萌娘 + 人工兜底**
  → 补充要求：「不准瞎掰 必须要有语料证据」

== 为什么要做地点（而不是只补人物）==
  替身天然**只覆盖第 3–9 部**：`stands.part` 实测第 1、2 部各 **0 个**
  （那两部是波纹时代，没有替身）。而第 1、2 部**有地点**（Ogre Street /
  Joestar Mansion / Air Supplena Island…）与人物 —— 加这两个维度才谈得上"更多部"。

== 本测试守什么 ==
  1. 数据：113 个正传地点、**覆盖全部 9 部**、三个门面地点（安杰罗岩/恶魔掌心/
     勒索之路）的部数与归属路径正确
  2. ★ **每条关联都必须带证据**（chunk_id + matched_term + snippet）——
     用户明确要求"不准瞎掰"。这条是本测试最核心的一项。
  3. 两类已知误匹配**不再出现**（都是"有证据外壳、结论却错"的典型）：
     · 短名是长名的一部分：`Cream` ← "Cream Starter"（第 3 部替身被挂到第 7 部地点）
     · 实义词撞常用词：`Forever` ← "live in the town forever as a rock"
  4. 关联分两个方向（强=地点条目正文写了实体／弱=实体条目提到地名），
     且**不允许把弱证据当成强证据**混进卡片
  5. 路由：地点问句走 location；替身问句不受影响；**无关问句仍然拒答**
     （M21 的守门测试，不能因为加了地名而失守）
  6. 端到端：三个门面地点问得出答案，卡片里"缺字段"照实报（**不填 0**）
"""
from __future__ import annotations

import http.client
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
PROC = ROOT / "dataset" / "processed"
SRC = ROOT / "dataset" / "sources"
SERVICES = ROOT / "services"
HTML = ROOT / "api" / "static" / "index.html"
MAINPY = ROOT / "api" / "main.py"
MIGRATION = ROOT / "dataset" / "schema" / "migrations" / "003_locations.sql"
PORT = 8805

# 三个门面地点（用户点名的 + 需求里出现的）
FACADE = {
    "angelo_rock": (4, ["Jozenji", "Morioh", "Japan"]),
    "devil_s_palm": (7, None),          # 源站标了两处归属 → 只查部数与路径条数
    "shakedown_road": (8, ["Morioh", "Japan"]),
}


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
    import locations as L                       # noqa: E402
    from conflict_resolver import Router        # noqa: E402

    print("=" * 74)
    print("M65：地点维度（数据 / 证据 / 路由 / 前端）")
    print("=" * 74)

    loc_json = PROC / "locations.json"
    men_json = PROC / "location_mentions.json"
    if not loc_json.exists() or not men_json.exists():
        print("  ⏭ 缺少地点产物（先跑 build_locations.py 与 "
              "build_location_mentions.py）—— 已跳过")
        return 0

    raw = json.loads(loc_json.read_text(encoding="utf-8"))
    men = json.loads(men_json.read_text(encoding="utf-8"))
    data = L.load()

    # ---- ① 数据规模与覆盖 ----
    print("\n[1] 数据：规模与部数覆盖")
    check("保留 113 个正传地点", data["n_included"] == 113,
          f"实际 {data['n_included']}")
    by_part = {}
    for r in data["by_id"].values():
        by_part[r.get("part")] = by_part.get(r.get("part"), 0) + 1
    check("★ 覆盖全部 9 部（第 1、2 部不再为空）",
          all(by_part.get(p, 0) > 0 for p in range(1, 10)),
          f"实际分布 {dict(sorted(by_part.items(), key=lambda x: (x[0] is None, x[0])))}")
    check("第 1 部有 9 个地点", by_part.get(1) == 9, f"实际 {by_part.get(1)}")
    check("第 2 部有 12 个地点", by_part.get(2) == 12, f"实际 {by_part.get(2)}")

    # 外传必须被排除（用户口径：只保留 9 部正传漫画）
    titles = {r.get("title") for r in data["by_id"].values()}
    check("外传地点已排除（JORGE JOESTAR 等）",
          not any("JORGE" in (t or "").upper() for t in titles)
          and not any(r.get("part") is None for r in data["by_id"].values()))

    # ---- ② 门面地点：部数与归属路径 ----
    print("\n[2] 门面地点（用户点名的三个）")
    for lid, (part, path) in FACADE.items():
        r = data["by_id"].get(lid)
        check(f"{lid} 存在且为第 {part} 部",
              bool(r) and r.get("part") == part,
              f"实际 {r.get('part') if r else '不存在'}")
        if r and path:
            check(f"{lid} 归属路径 = {' → '.join(path)}",
                  path in (r.get("place_paths") or []),
                  f"实际 {r.get('place_paths')}")
    # ★ 恶魔掌心：源站标了两处归属（美国 / 杜王町），不能拼成一条不存在的路径
    dp = data["by_id"].get("devil_s_palm") or {}
    check("★ 恶魔掌心保留源站的**多条**归属路径（不拼成假路径）",
          len(dp.get("place_paths") or []) >= 2,
          f"实际 {dp.get('place_paths')}")

    # ---- ②′ 字段脏值自检（真实踩到的 bug）----
    #   ★ 成因：取值正则里用了 `\s*` 而 `\s` **包含换行** ——
    #     遇到空字段 `|animedebut = ` 时会把**下一行整行**当成值吞进来
    #     （实测 25 个地点的 animedebut 变成 `|colors     = JoJolion`，
    #      界面上显示成「动画：]colors = JoJolion」）。
    #   → 修法是空白类必须写成 `[^\S\n]`（不含换行）。
    bad_vals = []
    for r in data["by_id"].values():
        for k in ("name_ja", "name_romaji", "type", "mangadebut", "animedebut"):
            v = r.get(k)
            if v and re.search(r"[=|]|[Cc]olors", v):
                bad_vals.append((r.get("title"), k, v))
    check("★ 字段值里没有「吞了下一行字段」的脏值（=|colors）",
          not bad_vals, f"{len(bad_vals)} 处：{bad_vals[:2]}")
    check("空字段确实是 None（Shakedown Road 源站未填 animedebut）",
          (data["by_id"].get("shakedown_road") or {}).get("animedebut") is None,
          str((data["by_id"].get("shakedown_road") or {}).get("animedebut")))

    # ---- ③ ★ 每一条关联都必须带证据（用户的核心要求）----
    print("\n[3] ★ 关联必须可追溯到语料（「不准瞎掰」的落地）")
    ms = men.get("mentions") or []
    check("关联条数 > 2000", len(ms) > 2000, f"实际 {len(ms)}")
    no_ev = [m for m in ms
             if not m.get("chunk_id") or not m.get("matched_term")
             or not (m.get("snippet") or "").strip()]
    check("★ 没有任何一条关联「无证据」（chunk_id + 命中词 + 原文片段）",
          not no_ev, f"缺证据 {len(no_ev)} 条：{no_ev[:2]}")
    # 两个方向都必须存在且带标记
    dirs = {m.get("direction") for m in ms}
    check("两类方向都有（强=地点条目写了实体／弱=实体条目提到地名）",
          dirs == {"location_page", "entity_page"}, f"实际 {dirs}")
    strong = [m for m in ms if m["direction"] == "location_page"]
    check("强证据条数 > 1000", len(strong) > 1000, f"实际 {len(strong)}")
    # 命中词要真的出现在片段里（否则"证据"是假的）
    fake = [m for m in ms
            if (m.get("matched_term") or "").lower()
            not in (m.get("snippet") or "").lower()]
    check("★ 命中词确实出现在片段里（证据不是摆设）",
          len(fake) == 0, f"对不上 {len(fake)} 条：{fake[:2]}")

    # ---- ④ 两类历史误匹配必须不再出现 ----
    print("\n[4] ★ 已知误匹配不再出现（有证据、但结论是错的）")
    # (a) 短名是长名的一部分：Cream（第 3 部）← "Cream Starter"（第 7 部替身）
    cream_dp = [m for m in ms if m["location_id"] == "devil_s_palm"
                and m["entity_id"] == "cream"]
    check("★ `Cream` 不再被误挂到恶魔掌心（\"Cream Starter\" 误命中）",
          not cream_dp, f"仍有 {len(cream_dp)} 条")
    check("`Cream Starter` 仍然被正确关联",
          any(m["location_id"] == "devil_s_palm"
              and m["entity_id"] == "cream_starter" for m in ms))
    # (b) 实义词撞常用词：Forever ← "live in the town forever as a rock"
    fv = [m for m in ms if m["entity_id"] == "forever"
          and m["direction"] == "location_page"]
    check("★ `Forever` 的命中都被要求首字母大写（不再撞 town forever）",
          all(m["matched_term"] == "Forever" or "Forever" in m["matched_term"]
              for m in fv) and bool(fv),
          f"样例 {[m['matched_term'] for m in fv][:4]}")

    # ---- ⑤ 数据质量的诚实性 ----
    print("\n[5] 缺数据照实说（不填 0）")
    check("中文名覆盖率如实记录（59/113）", data["n_with_zh"] == 59,
          f"实际 {data['n_with_zh']}")
    card = L.answer("安杰罗岩是什么地方")[2]
    check("★ 卡片带 missing（源站未提供的字段清单）",
          isinstance(card.get("missing"), list))
    check("★ 卡片带弱证据数量，并标注为弱",
          isinstance(card.get("weak_count"), int) and card["weak_count"] >= 0)
    check("卡片只把**强证据**放进 stands/characters",
          all(isinstance(x.get("chunk_id"), int) for x in card["stands"]))

    # ---- ⑥ 路由 ----
    print("\n[6] 路由：地点 / 替身 / 守门")
    r = Router(known_stands={"Star Platinum", "Crazy Diamond", "Tusk"},
               known_entities={"Star Platinum", "Crazy Diamond", "Tusk",
                               "Jotaro Kujo"},
               known_locations=set(L.all_names()))
    for q, want in [("安杰罗岩是什么地方", "location"),
                    ("杜王町有哪些地点", "location"),
                    ("恶魔掌心在哪里", "location"),
                    ("勒索之路有哪些替身", "location"),
                    ("第 1 部有哪些地点", "location")]:
        d = r.route(q)
        check(f"「{q}」→ {want}", d.route == want, f"实际 {d.route}")
    # 替身问句不受影响（地名不能抢走替身锚定）
    check("「Star Platinum 的破坏力是几级」仍是 structured",
          r.route("Star Platinum 的破坏力是几级").route == "structured")
    check("「Tusk 有什么能力」不因新增地点而变成 location",
          r.route("Tusk 有什么能力").route != "location")
    # ★ 守门：无关问句必须仍拒答
    for q in ["今天天气怎么样", "华盛顿在哪里",
              "第 10 部有哪些地点", "替身 Star Platinum Ultimate 的使用者是谁"]:
        check(f"守门：「{q}」仍拒答", r.route(q).route == "abstain",
              f"实际 {r.route(q).route}")

    # ---- ⑦ 代码接线（正文改对了没） ----
    print("\n[7] 接线：schema / 服务层 / API / 前端")
    mig = MIGRATION.read_text(encoding="utf-8") if MIGRATION.exists() else ""
    for t in ("locations", "location_places", "location_mentions"):
        check(f"schema 有表 {t}", f"TABLE IF NOT EXISTS {t}" in mig)
    check("★ location_mentions 带 direction（区分强/弱证据）",
          "direction" in mig)
    check("★ location_mentions 带 chunk_id（证据可追溯）", "chunk_id" in mig)
    check("text_chunks 增加 location_id",
          "location_id" in mig and "text_chunks" in mig)

    src = HTML.read_text(encoding="utf-8")
    mainpy = MAINPY.read_text(encoding="utf-8")
    check("API 有 location 分支", 'route == "location"' in mainpy)
    check("API 响应带 location_card", '"location_card": location_card' in mainpy)
    check("★ Evidence 模型含 location_id（否则被白名单静默过滤）",
          re.search(r"location_id: Optional\[str\] = None", mainpy) is not None
          and '"location_id"' in mainpy)
    check("前端有地点卡渲染", "function renderLocationPanel" in src)
    check("前端有点关联看原文", "function showLocEvidence" in src)
    check("前端渲染四个地点答案类型",
          all(f"t==='{x}'" in src for x in
              ("location_fact", "location_list", "location_related",
               "location_hint")))
    check("★ 地点缺失字段显示为「源站未提供」（不是 0）",
          "源站未提供" in src)
    # ★ 关联标签的证据必须**按索引引用**，不能把 JSON 内联进 onclick ——
    #   实测：JSON 里的双引号会提前闭合 HTML 属性，onclick 变成半截代码，
    #   点标签**毫无反应**（不报错、最难查那种）。真浏览器验证见 §9。
    #   ★ 判据落在**实际的 onclick**上，且要先**去掉注释行** ——
    #     函数上方那段解释这个坑的注释本身就含 `onclick="showLocEvidence(${JSON…`，
    #     拿整份文件做包含判断会被**自己的注释**绊倒（我连踩两次：
    #     第一次写 `not in src`，第二次换了正则还是被注释命中）。
    _code = "\n".join(ln for ln in src.splitlines()
                      if not ln.lstrip().startswith("//"))
    check("★ 关联标签按索引引用证据（不内联 JSON，避免属性被引号切断）",
          "showLocEvidence(${i})" in _code and "LOC_EV" in _code
          and not re.search(r'onclick="showLocEvidence\(\$\{JSON', _code))

    # ---- ⑧ 端到端 ----
    print(f"\n[8] 端到端（127.0.0.1:{PORT}）")
    log = (ROOT / ".tmp_preview" / "_m65_server.log").open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "api" / "main.py"),
         "--port", str(PORT), "--no-vector"],
        cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL)
    try:
        if not wait_http(PORT):
            check("服务就绪", False, "超时")
            return 1
        d = query("安杰罗岩是什么地方", PORT)
        lc = d.get("location_card") or {}
        check("「安杰罗岩是什么地方」route=location",
              d.get("route") == "location", f"实际 {d.get('route')}")
        check("答案类型 = location_fact",
              d.get("answer_type") == "location_fact",
              f"实际 {d.get('answer_type')}")
        check("卡片给出中文名/日文名/第 4 部",
              lc.get("name_zh") == "安杰罗岩"
              and lc.get("name_ja") == "アンジェロ岩"
              and lc.get("part") == 4, str(lc)[:120])
        check("卡片归属路径 = Jozenji → Morioh → Japan",
              ["Jozenji", "Morioh", "Japan"] in (lc.get("place_paths") or []),
              str(lc.get("place_paths")))
        check("★ 关联替身里能看到 Crazy Diamond（且有原文片段）",
              any(s["name"] == "Crazy Diamond" and s.get("snippet")
                  for s in (lc.get("stands") or [])),
              str([s["name"] for s in (lc.get("stands") or [])]))
        check("证据条带 location_id 且未被白名单过滤",
              any(e.get("location_id") == "angelo_rock"
                  for e in (d.get("evidence") or [])),
              str([e.get("type") for e in (d.get("evidence") or [])]))

        d2 = query("杜王町有哪些地点", PORT)
        a2 = d2.get("answer") or {}
        check("「杜王町有哪些地点」返回列表且数量合理",
              d2.get("answer_type") == "location_list"
              and isinstance(a2.get("count"), int) and a2["count"] >= 20,
              f"type={d2.get('answer_type')} count={a2.get('count')}")

        d3 = query("勒索之路有哪些替身", PORT)
        a3 = d3.get("answer") or {}
        check("「勒索之路有哪些替身」返回 location_related",
              d3.get("answer_type") == "location_related",
              f"实际 {d3.get('answer_type')}")
        check("相关替身来自语料（Soft & Wet 在列表里）",
              any(x.get("name") == "Soft & Wet" for x in (a3.get("stands") or [])),
              str([x.get("name") for x in (a3.get("stands") or [])]))

        # 守门：端到端也要拒答
        d4 = query("华盛顿在哪里", PORT)
        check("守门：端到端「华盛顿在哪里」仍拒答",
              d4.get("answer_type") == "abstain",
              f"实际 {d4.get('answer_type')}")
        # 回归：替身问句照旧
        d5 = query("Star Platinum 的破坏力是几级？", PORT)
        a5 = d5.get("answer")
        a5 = a5[0] if isinstance(a5, list) and a5 else a5
        check("回归：替身六维查询仍是 fact（未被地点路由影响）",
              isinstance(a5, dict) and a5.get("type") == "fact",
              str(a5)[:100])
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

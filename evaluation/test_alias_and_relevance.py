"""
中文名与领域相关性回归测试（M14）。

★★ 触发这个模块的用户 bug：
    Q「黄金体验的能力」
    A → 一张「Stardust Crusaders 33 个替身 / Diamond is Unbreakable 29 个…」
         的篇章统计表 —— **看起来合理但完全答非所问**。

  根因链（三层，每层单独修都不够）：
    ① `name2id` 只收英文名 → 认不出中文实体「黄金体验」
    ② `detect_intent` 的兜底是 `part_count` → 落进篇章统计
    ③ 路由器无「不相关」判据 → 「今天天气怎么样」也会去检索

用法：
    python evaluation/test_alias_and_relevance.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for sub in ("services", "retrieval", "database"):
    sys.path.insert(0, str(ROOT / sub))

from aliases import (  # noqa: E402
    build_alias_table, expand_variants, match_stand, simplify_zh,
)
from conflict_resolver import Router  # noqa: E402
from executor import detect_intent  # noqa: E402

PROC = ROOT / "dataset" / "processed"


def main() -> int:
    print("=" * 70)
    print("中文名 & 领域相关性回归测试（M14）")
    print("=" * 70)

    alias2id, id2alias = build_alias_table()
    stands = json.loads((PROC / "stands.json").read_text(encoding="utf-8"))

    # ---------------------------------------------------------
    print("\n[1] 别名表基础不变量")
    miss = [s["name_en"] for s in stands
            if s.get("name_en") and s["name_en"] not in alias2id]
    print(f"  {'✓' if not miss else '✗'} 英文名全部在表里"
          f"（{len(stands) - len(miss)}/{len(stands)}）")
    if miss:
        print(f"      ★ 缺 {miss[:5]}")
        return 1

    n_zh = sum(1 for v in id2alias.values()
               if any(re.search(r"[\u4e00-\u9fff]", x) for x in v))
    print(f"  ℹ 带中文名的替身 {n_zh} / {len(stands)}"
          f"（其余 {len(stands) - n_zh} 个需用英文名）")

    # 单字中文名必须被排除（否则会抢任何含该字的问句）
    single = [n for n in alias2id if len(n) == 1
              and re.search(r"[\u4e00-\u9fff]", n)]
    print(f"  {'✓' if not single else '✗'} 无单字中文别名"
          f"{'' if not single else f' —— 有 {single[:5]}'}")
    if single:
        print("      ★ 单字名会抢匹配（如「力」匹配「能力」）")
        return 1

    # ---------------------------------------------------------
    print("\n[2] 简繁通配（用码点断言，避开字体规范化）")
    trad = "黄金体験"    # 黄金体験
    simp = "黄金体验"    # 黄金体验
    got = simplify_zh(trad)
    ok = got == simp
    print(f"  {'✓' if ok else '✗'} 黄金体験 → {got}")
    if not ok:
        print(f"      ★ 码点：得到 {[hex(ord(c)) for c in got]}")
        print(f"        期望 {[hex(ord(c)) for c in simp]}")
        return 1

    # ---------------------------------------------------------
    print("\n[3] 实体匹配（★ 用户的原问题）")
    cases = [
        ("黄金体验的能力", "gold_experience"),
        ("黄金体验的破坏力是几级", "gold_experience"),
        ("Gold Experience 的能力", "gold_experience"),
        ("ゴールド・エクスペリエンス 的能力", "gold_experience"),
        ("女教皇的破坏力", "high_priestess"),
        ("Star Platinum 的破坏力", "star_platinum"),
    ]
    bad = 0
    for q, exp in cases:
        got = match_stand(q, alias2id)
        ok = got == exp
        if not ok:
            bad += 1
        print(f"  {'OK ' if ok else 'XX '}{q:32s} → {got}")
    if bad:
        print(f"  ★ {bad}/{len(cases)} 失败")
        return 1

    # ---------------------------------------------------------
    print("\n[4] 意图识别：兜底不能是「篇章统计」")
    #★ 这条是用户 bug 的第②层：
    #   原来 detect_intent 最后一行是 return "part_count"
    #   → 任何识别失败的问题都返回篇章统计表
    intent_cases = [
        ("黄金体验的能力", "unknown"),
        ("黄金体验的破坏力是几级", "fact"),
        ("每部有多少替身", "part_count"),
        ("各部的替身数量", "part_count"),
        ("Star Platinum 的破坏力是几级", "fact"),
        ("Star Platinum 的使用者是谁", "owner"),
        ("Star Platinum 有哪些形态", "forms"),
        ("破坏力最高的是哪个", "extreme"),
    ]
    bad2 = 0
    for q, exp in intent_cases:
        got = detect_intent(q)
        ok = got == exp
        if not ok:
            bad2 += 1
        print(f"  {'OK ' if ok else 'XX '}{q:28s} → {got:12s}(期望 {exp})")
    if bad2:
        print(f"  ★ {bad2}/{len(intent_cases)} 失败")
        return 1

    # ---------------------------------------------------------
    print("\n[5] 领域相关性：无关问句必须拒答")
    known = {s["name_en"] for s in stands if s.get("name_en")}
    # 把中文别名也纳入（与 api/main.py 的做法一致）
    for names in id2alias.values():
        for n in names:
            if n and re.search(r"[\u4e00-\u9fff]", n):
                known.add(n)
    router = Router(known_stands=known, known_entities=known)

    route_cases = [
        # —— 完全不相关 ——
        ("今天天气怎么样", "abstain"),
        ("请给我讲个笑话", "abstain"),
        ("明天会下雨吗", "abstain"),
        ("帮我写一段Python 代码", "abstain"),
        # —— 本库领域内，不能误拒 ——
        ("黄金体验的能力", "structured|semantic|entity_semantic"),
        ("Star Platinum 的破坏力是几级？", "structured"),
        # ★ M16：这两句现在走 entity_semantic —— 因为句中已锚定替身实体
        #   （Anubis / Tusk），按该替身检索比全库 BM25 更准。
        #   实测：新逻辑下答案分别是 Anubis / Tusk ACT1 的原文，
        #   比旧的全库检索更贴题（旧的靠 BM25 恰好排第一才答对）。
        ("Anubis 的外观形态方面有哪些描述？", "semantic|entity_semantic"),
        ("所有替身中破坏力最高的是哪个？", "structured"),
        ("Star Platinum 有哪些形态？", "structured"),
        ("每部有多少替身？", "structured"),
        ("Star Platinum 的使用者是谁？", "structured"),
        ("Tusk 的形态与外观有哪些描述？", "semantic|entity_semantic"),
    ]
    bad3 = 0
    for q, exp in route_cases:
        d = router.route(q)
        ok = d.route in exp.split("|")
        if not ok:
            bad3 += 1
        print(f"  {'OK ' if ok else 'XX '}{q:30s} → {d.route:10s}")
    if bad3:
        print(f"  ★ {bad3}/{len(route_cases)} 失败")
        return 1

    # ---------------------------------------------------------
    # ★★★ M16 回归：冷门替身中文名必须锚定到正确实体 ★★★
    #
    #   背景：M16 把中文名补到 154/154 后，立刻暴露一个旧 bug ——
    #   「骇游天外的能力是什么」返回 Strength（Forever 的替身）。
    #   根因：语义问题直接丢给全库 BM25，而语料（jojowiki 正文）里
    #         只有 name_en、没有中文 → 冷门中文名匹配不上 → 退化到任意结果。
    #         （「黄金体验」以前是对的，只是因为它 BM25 排名恰好最高。）
    #
    #   这组断言锁死「中文名 → 正确 stand_id」的映射，
    #   防止以后再加中文名时静默退化。
    print("\n[5b] M16 冷门替身中文名 → 实体锚定")
    m16_cases = [
        ("骇游天外的能力是什么", "sky_high"),
        ("小面孔的能力", "smallfaces"),
        ("紫烟破音有什么效果", "purple_haze_distortion"),
        ("牵线木偶师怎么运作", "fun_fun_fun"),
        ("巴斯特女神的能力", "bastet"),
        ("神圣之屋的能力", "house_of_holy"),
        ("遥远浪漫的能力", "remote_romance"),
        ("永恒的守望塔是什么", "all_along_watchtower"),
        # 异译别名也要能命中（同一替身的两种社区叫法）
        ("小脸的能力", "smallfaces"),
        ("荷莉之屋的能力", "house_of_holy"),
    ]
    bad3b = 0
    for q, exp in m16_cases:
        got = match_stand(q, alias2id)
        ok = got == exp
        if not ok:
            bad3b += 1
        print(f"  {'OK ' if ok else 'XX '}{q:24s} → {str(got):28s}"
              f"{'' if ok else f'期望 {exp}'}")
    # 覆盖完整度：**每个替身**都要有中文名
    # ★ 原来硬编码 `== 154`（M16 时的目标）。M21 补上母体名
    #   （echoes / tusk）后变成 156 → 这条一直红着。
    #   → 改成与「替身总数」比较，这样以后补名字不会再误报，
    #     而真出现漏网替身时仍会报警。
    n_zh = sum(1 for v in id2alias.values()
               if any(re.search(r"[\u4e00-\u9fff]", x) for x in v))
    # ★ id2alias 是 {stand_id: [别名列表]}，所以替身总数 = len(id2alias)
    n_total = len(id2alias)
    ok_zh = (n_zh == n_total)
    print(f"  {'OK ' if ok_zh else 'XX '}带中文名的替身{n_zh} / {n_total}"
          f"{'' if ok_zh else '（应全覆盖）'}")
    if not ok_zh:
        bad3b += 1
    if bad3b:
        print(f"  ★ {bad3b} 项失败")
        return 1
    print("  ✓ 冷门中文名不再检索到别的替身")

    # ---------------------------------------------------------
    print("\n[6] Evidence 类型约束（★ 用真实 HTTP 服务验证）")
    #★ 本项目已 3 次在 Evidence 白名单模型上栽：
    #   M13 加字段没声明 → 响应里看不到
    #   M14  note 塞 dict → ValidationError → 500
    #   M14b _valid_evidence 只查 type 存在 → 类型不符的一路漏到 Pydantic
    #
    #★ 这里用**子进程起真实 uvicorn + HTTP 请求**验证：
    #   用 importlib 加载 api/main.py 会因forward ref 解析不了而误报
    #   （模块名变成 apimod，M10 就踩过；不是产品 bug）。
    import json as _json
    import socket
    import subprocess
    import time
    import urllib.error
    import urllib.parse
    import urllib.request

    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        port = sk.getsockname()[1]
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "api" / "main.py"),
         "--port", str(port), "--no-vector"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(60):
            time.sleep(1)
            try:
                urllib.request.urlopen(base + "/health", timeout=3)
                break
            except Exception:
                pass
        else:
            print("  ✗ 服务启动超时")
            return 1

        def post(q, **kw):
            url = base + "/query?" + urllib.parse.urlencode({"q": q, **kw})
            r = urllib.request.Request(url, method="POST")
            return _json.loads(urllib.request.urlopen(r, timeout=60).read())

        # 6a. note 类型不符时不能 500
        try:
            d = post("今天天气怎么样")
            if d["route"] != "abstain":
                print(f"  ✗ 无关问句未拒答：route={d['route']}")
                return 1
            print("  ✓ 无关问句 → abstain（未500）")
        except urllib.error.HTTPError as e:
            print(f"  ✗ HTTP {e.code} —— 又炸在 Evidence 模型上")
            return 1

        # 6b. 正常问句的 evidence 必须带 M13 的合并字段
        d = post("Anubis 的外观形态方面有哪些描述？", top_k=2)
        ev = d.get("evidence") or []
        if not ev:
            print("  ✗ 无证据")
            return 1
        need = ("n_base_blocks", "base_chunk_ids", "merged", "n_chars")
        missing = [k for k in need if k not in ev[0]]
        if missing:
            print(f"  ✗ Evidence 缺字段 {missing}（M13 加的，被白名单过滤了）")
            return 1
        print(f"  ✓ Evidence 含合并字段 {list(need)}")

        # 6c. 健康检查报告索引配置
        h = _json.loads(urllib.request.urlopen(base + "/health", timeout=15).read())
        print(f"  ✓ /health 正常（status={h['status']}）")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()

    # ---------------------------------------------------------
    print("\n" + "=" * 70)
    print("★ 全部通过")
    print("=" * 70)
    print("""
修复前：
  「黄金体验的能力」→ 篇章统计表（Stardust Crusaders 33 个…）
  「今天天气怎么样」→ Heaven's Door 的介绍
修复后：
  「黄金体验的能力」→ Gold Experience 的原文描述
  「今天天气怎么样」→ 明确拒答（本库只收录 JoJo 替身数据）

★ 三层根因，缺一层都不会好：
  ① name2id 只收英文名  → 补 aliases.py（中文/日文别名 + 简繁通配）
  ② detect_intent 兜底是 part_count → 改成 unknown + 显式篇章关键词
  ③ 路由器无「不相关」判据→ 加 DOMAIN_HINT 前置检查
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())

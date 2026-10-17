"""M79：句式体检（答非所问 / 没识别出问什么）

== 起因 ==
用户连续报「回答对不上提问」「他无法识别做出正确答案」。
于是做了一件以前没做过的事：**用 20 个真实问句做端到端体检**，
每条预先写好「应该是什么答案类型」，跑完看哪些不符。
结果 **20 条里 5 条答歪**，其中一条极严重。

== 5 条根因（不是同一种病）==

【1】★★「承太郎的替身是什么」→ 返回 **Sun** 的介绍
  问句主体是**角色简称**，但 STAND_OF 那条路靠 `extract_stand_name()`，
  它只在**替身名表**里找 → 「承太郎」永远抽不到 → executor 放弃
  → 回退语义检索 → BM25 随便给了一个高频替身（Sun）。
  ★ 这是最严重的一种答非所问：**答案存在、机制存在，只是没接上**。
  修：Router 里 STAND_OF 分支判断「主体不是替身名」时，
      先用 `characters.match()` 试一次 → 命中角色就改走**角色维度**
      （顺带得到中文名、数量、多候选，比原来那条路信息更全）。

【2】「黄金体验有几招」→ 返回一段人物介绍
  ★ **同一个词，Router 认了、执行器没认**：
      Router 的 MOVE_ASK 加了「几招」→ 判成 structured；
      但 executor 的 INTENT_MOVES 没加 → intent=unknown → 降级语义检索。
  → 「路由对了、答案还是错的」。两处正则必须同步。
  → 单数出场（「在哪一话出场」「首次登场是第几话」）同理：
      INTENT_APPEARANCE 只认复数（「哪几话」「哪些话」）。

【3】「谁打败了普奇」→ 返回普奇的档案
  库里**没有**战况/死因/宿敌数据，但问句带着实体名，
  于是任何分支都会拿它去查角色，然后返回一段不相干的介绍。
  ★ **拿不相干的内容充数，比明确拒答更糟**。
  修：NO_DATA_ASK 直接 abstain，并把「本库有什么 / 没有什么」讲清楚。

【4】「乔斯达的替身」→ 返回一段无关原文
  歧义姓氏（12 个乔斯达）走进了语义检索。
  → 与【1】同一条修复：主体命中角色 → 走角色维度 → 返回候选列表。

【5】「空条承太郎第一次出场是第几话」→ 掉进语义检索
  角色卡里本来就有 mangadebut（344/368 个角色有），只是没被路由认出来。
  修：CHAR_DEBUT + 角色命中 → 走角色维度。

★ 这轮的通用教训：**中文里同一个问法有多种说法**
  （几招 / 有哪些招 / 招数；哪一话 / 哪几话 / 哪些章节），
  正则只列了其中一种，其余全部掉进语义检索 —— 而语义检索**永远能返回点什么**，
  所以看起来「有答案」，实际是答非所问。

跑法：python evaluation/test_m79_intent_audit.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "services"))
sys.path.insert(0, str(ROOT / "api"))

FAILS: list[str] = []

# (问句, 期望 answer_type 集合, 这条在测什么)
CASES = [
    # —— 数量 ——
    ("黄金体验有几招", {"moves"}, "数量-招式（M79【2】）"),
    ("第4部有多少个替身", {"count", "fact", "part_list", "composite"}, "数量-按部"),
    ("Giorno Giovanna 有几个替身", {"character_stands"}, "数量-角色替身（M78）"),
    # —— 反查 ——
    ("谁使用黄金体验", {"owner"}, "反查-使用者"),
    ("承太郎的替身是什么", {"stand_of"},
     "★★ 简称反查（M79【1】：曾返回 Sun）"),
    ("乔斯达的替身", {"stand_of"},
     "★★ 歧义姓氏（M79【4】：曾返回无关原文）"),
    # —— 属性 / 列表 ——
    ("黄金体验的破坏力是几级", {"fact"}, "属性"),
    ("第4部有哪些替身", {"part_list", "fact", "composite"}, "列表-按部"),
    ("杜王町有哪些角色", {"location_related", "fact"}, "列表-地点关联"),
    ("杀手皇后有哪几个形态", {"forms", "fact"}, "形态"),
    # —— 时间 ——
    ("白金之星在哪一话出场", {"appearance"},
     "★ 单数出场（M79【2】：曾返回人物介绍）"),
    ("空条承太郎第一次出场是第几话", {"character_fact"},
     "★ 角色首登场（M79【5】：曾掉进语义检索）"),
    # —— 归属 ——
    ("迪奥是哪一部的", {"character_fact", "character_candidates"}, "角色-分部"),
    ("乔鲁诺·乔巴拿属于哪个组织", {"character_fact", "snippet"}, "角色-组织"),
    # —— 关系（M77）——
    ("普奇的弟弟是谁", {"character_relatives"}, "亲属关系（M77）"),
    # —— 本库没有的数据 ——
    ("谁打败了普奇", {"abstain"},
     "★★ 库里没有的战况（M79【3】：曾返回普奇档案）"),
    ("承太郎是怎么死的", {"abstain"}, "★ 死因（本库没有）"),
    # —— 守门 ——
    ("今天天气怎么样", {"abstain"}, "守门-无关问句"),
    ("特朗普的替身是什么", {"abstain"}, "守门-不存在的人"),
    ("Ora Ora 是什么", {"abstain", "snippet", "fact"}, "招式名不在结构化维度里"),

    # ===== ★ M80 第二弹：关系 / 结局 / 比较 / 类型 / 第 N 形态 =====
    #   （19 句体检，不匹配 0 项）
    ("谁和空条承太郎认识", {"abstain"}, "关系-泛（库里没有）"),
    ("空条承太郎和乔瑟夫是什么关系", {"abstain"}, "关系-两实体（库里没有）"),
    ("空条承太郎的结局是什么", {"abstain"}, "结局（库里没有）"),
    ("普奇最后怎么样了", {"abstain"}, "结局-口语（库里没有）"),
    ("白金之星和黄金体验谁更强", {"compare"}, "比较-两替身（六维逐项）"),
    ("白金之星是近战型吗", {"stand_type"}, "布尔-替身类型"),
    ("白金之星是自动型吗", {"stand_type"}, "布尔-否定也要给类型"),
    ("哪些替身是自动型", {"stand_type_list"}, "筛选-按类型"),
    ("杀手皇后第二形态是什么", {"forms"}, "第 N 形态（只有基础形态要说清）"),
    ("一共有多少个替身", {"count", "fact"}, "计数-总量"),
    ("第5部有几个角色", {"count", "fact", "part_list"}, "计数-按部角色"),
    ("杜王町有哪些地点", {"location_list", "location_related", "fact"},
     "列表-地点层级"),

    # ===== ★ M81 第三弹：多轮追问 + 时间线 =====
    #   （14 句体检，不匹配 0 项）
    ("白金之星一共出场多少话", {"appearance"}, "时间线-出场话数"),
    ("白金之星动画出场多少集", {"appearance"}, "时间线-动画集数"),
    ("白金之星在漫画里出场吗", {"appearance_bool", "appearance"},
     "时间线-是否出场（布尔）"),
    ("白金之星第一次出场是哪一话", {"appearance"}, "时间线-首次"),
    ("白金之星最后一次出场是哪一话", {"appearance"}, "时间线-最后"),
]

# ---- M81：多轮追问（同一 session_id 连问两句）----
TURNS = [
    ("白金之星有什么招式", "那它的射程呢", {"fact"}, "追问-替身属性"),
    ("白金之星有什么招式", "那它有几个形态", {"forms"}, "追问-形态"),
    ("白金之星有什么招式", "那它一共出场多少话", {"appearance"},
     "追问-出场话数"),
    ("乔鲁诺·乔巴拿是谁", "那他的替身是什么", {"stand_of", "character_stands"},
     "追问-角色替身"),
    ("白金之星是什么类型的替身", "它的使用者是谁", {"owner"}, "追问-使用者"),
    ("黄金体验的射程是多少", "和疯狂钻石比呢", {"compare"},
     "追问-省略主语的比较"),
    ("白金之星的招式有哪些", "它第一次出场是哪一话", {"appearance"},
     "追问-出场"),
    ("白金之星是谁的替身", "它的破坏力呢", {"fact"}, "追问-属性"),
]


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          + (f"   {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:  # noqa: PLR0915
    print("=" * 74)
    print("M79：句式体检（答非所问 / 没识别出问什么）")
    print("=" * 74)

    # ---- ① 意图识别的**纯函数**层面（快，先跑）----
    print("\n[1] ★ 意图正则（同一个词必须两处都认）")
    from executor import detect_intent
    for q, want in (("黄金体验有几招", "moves"),
                    ("黄金体验有多少招", "moves"),
                    ("白金之星在哪一话出场", "appearance"),
                    ("白金之星首次登场是第几话", "appearance"),
                    # 「能力是什么」刻意**不**收 moves（要的是能力描述长段）
                    ("黄金体验的能力是什么", "unknown"),
                    # 「招式是什么意思」执行器会判 moves，但 Router 要求**锚定替身**
                    # → 不满足就走语义/abstain（这层防线在 Router，不在 detect_intent）
                    ("招式是什么意思", "moves")):
        got = detect_intent(q)
        check(f"detect_intent「{q}」→ {want}", got == want, f"实得 {got}")

    from conflict_resolver import Router
    ks = {"gold experience", "黄金体验", "star platinum", "白金之星",
          "jotaro kujo", "空条承太郎"}
    r = Router(known_stands=ks, known_entities=set(ks),
               known_locations=set(), known_characters={"承太郎", "空条承太郎"})
    # ★ 承太郎的替身**仍然是 structured/stand_of** —— M79 第一版改路由时
    #   把它变成 character 路由，5 个测试文件当场变红（类型契约被打破）。
    for q, want in (("黄金体验有几招", "structured"),
                    ("承太郎的替身是什么", "structured"),
                    ("谁打败了乔鲁诺", "abstain"),
                    ("今天天气怎么样", "abstain")):
        got = r.route(q).route
        check(f"Router「{q}」→ {want}", got == want, f"实得 {got}")

    # ---- ② 端到端（真实服务）----
    print("\n[2] ★ 端到端 20 句")
    from fastapi.testclient import TestClient
    import main as m

    ctx = TestClient(m.app)
    ctx.__enter__()
    for q, exp, tag in CASES:
        d = ctx.post("/query", params={"q": q}).json()
        t = d.get("answer_type")
        ok = t in exp
        extra = ""
        if not ok:
            extra = f"期望 {'/'.join(sorted(exp))}"
        check(f"{tag}｜{q}", ok, f"实得 {t}；{extra}")
        # 顺带核对内容（最容易答歪的三个）
        a = d.get("answer") or {}
        if ok and isinstance(a, dict) and q == "承太郎的替身是什么":
            names = [x.get("zh") or x.get("name") for x in (a.get("stands") or [])]
            check("  └ 内容对：白金之星（不是 Sun）", "白金之星" in names,
                  f"实得 {names}")
        if ok and isinstance(a, dict) and q == "乔斯达的替身":
            check("  └ 歧义姓氏明确要求全名，不随机答一个",
                  bool(a.get("ambiguous")) and not a.get("stands"),
                  f"实得 {a}")
        # ---- M80 的内容核对（新答案不能只有类型、没有内容）----
        if ok and isinstance(a, dict) and q == "白金之星和黄金体验谁更强":
            check("  └ 比较结论用**替身名**（不是内部 id）",
                  "star_platinum" not in (a.get("verdict") or "")
                  and "Star Platinum" in (a.get("verdict") or ""),
                  f"实得 {a.get('verdict')}")
            check("  └ 缺维度不按 0 计（共同维度数写进答案）",
                  isinstance(a.get("n_common"), int)
                  and a.get("n_excluded") == 6 - a.get("n_common"),
                  f"实得 n_common={a.get('n_common')} "
                  f"n_excluded={a.get('n_excluded')}")
        if ok and isinstance(a, dict) and q == "白金之星是近战型吗":
            check("  └ 布尔判断给出**源站类型原文**（不只说是）",
                  a.get("match") is True and "近距离型" in (a.get("type_cn") or ""),
                  f"实得 {a}")
        if ok and isinstance(a, dict) and q == "白金之星是自动型吗":
            check("  └ 否定也要给出源站类型（不空手说不是）",
                  a.get("match") is False and bool(a.get("type_cn")),
                  f"实得 {a}")
        if ok and isinstance(a, dict) and q == "哪些替身是自动型":
            check("  └ 筛选给出数量与名单", a.get("count", 0) > 5,
                  f"实得 {a.get('count')}")
        if ok and isinstance(a, dict) and q == "杀手皇后第二形态是什么":
            check("  └ 只有基础形态时**明说**没有第 2 形态",
                  "没有第 2 形态" in (a.get("answer") or "")
                  or "只列了 1 个形态" in (a.get("answer") or ""),
                  f"实得 {a.get('answer')}")

    print("\n[3] ★ 多轮追问（M81：省略主语 + 时间线）")
    for i, (first, follow, exp, tag) in enumerate(TURNS):
        sid = f"m79-turn-{i}"
        d1 = ctx.post("/query", params={"q": first, "session_id": sid}).json()
        d2 = ctx.post("/query", params={"q": follow, "session_id": sid}).json()
        t2 = d2.get("answer_type")
        check(f"{tag}｜{first} → {follow}", t2 in exp, f"实得 {t2}")
        if t2 in exp and isinstance(d2.get("answer"), dict):
            a2 = d2["answer"]
            if follow == "和疯狂钻石比呢":
                check("  └ coref 补上省略的主语（两个替身都在答案里）",
                      "Gold Experience" in str(a2.get("answer"))
                      and "Crazy Diamond" in str(a2.get("answer")),
                      f"实得 {str(a2.get('answer'))[:80]}")
            if follow == "那它一共出场多少话":
                check("  └ 出场话数是真的（不是空壳）",
                      bool(a2.get("n_manga") or a2.get("n_anime")),
                      f"实得 n_manga={a2.get('n_manga')} "
                      f"n_anime={a2.get('n_anime')}")

    print("\n" + "=" * 74)
    if FAILS:
        print(f"✗ {len(FAILS)} 项未通过：{FAILS}")
        return 1
    print("✓ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
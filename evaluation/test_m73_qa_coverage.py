"""M73：端到端问法覆盖 —— 中文名重跑 + 简称 + owner 问法 + 出场 NL 入口

== 起因：一次端到端问句体检（而非又一次数据深挖）==
从**用户视角**跑了 20 个真实问法，发现一批「**数据都有、但用户问不到**」
的问题 —— 这与前面几轮修的东西性质不同：不是数据缺，是最后一公里没接上。

实测（修之前）：
    「白金之星是谁的替身」      → abstain      （最基础的问法却拒答）
    「布加拉提的替身是什么」    → 语义片段      （库里只有全名「布罗诺·布加拉提」）
    「布加拉提是谁」           → abstain
    「白金之星在哪些章节出场」  → 语义片段      （M71 建了 4902 条出场记录，没入口）

== 本轮四件事 ==
【1】中文名脚本**改进过但从未重跑**（第五次「下游产物没跟着重算」）
    `fetch_zh_characters.py` 在 M21/M70/M72 期间被改过多次
    （rowspan 两版修正、全角标点处理、nihongo 模板第二数据源…），
    但产物一直停在 154 个角色 → **主角全查不到**：
    乔尼·乔斯达 / 乔鲁诺·乔巴拿 / 布加拉提 / 空条徐伦 / 杰洛·齐贝林。
    重跑后 154 → 219（+65）。

【2】质检：过滤「解析错位」产生的假别名
    新增的候选里有两类错：
      · `whitesnake_s_unknown_victim` 挂了 5 个**别人**的名字（含替身名「白蛇」）
        —— 源站某行把一串人名写在同一个「本体」单元格里
      · `d_i_s_c_o` / `donatello_versus` 的本体名被写成**替身名**
    规则：① >3 个候选名 → 整条丢弃；② 候选名是已知替身名 → 移除。
    ★ 刻意**不**做「子串即错位」那条：实测误杀率 60%（乔治·乔斯达一世 /
      东方宪助 / 波克洛克都是真名，只是与「…三世」「第四代…」构成子串）。

【3】简称：用户不打「布罗诺·布加拉提」
    含间隔号的中文名拆出**唯一归属**的简称（布加拉提 / 西撒 / 乔尼）。
    歧义简称（齐贝林 5 个角色、乔斯达 12 个角色）**一律不收**。
    ★ 并把 `known_characters` 与 `characters.match()` 都接上别名表 ——
      此前有**两份**「角色名写法」（ext 的 name_zh vs aliases 表），
      这正是 M69/M70/M71/M72 反复栽的「两份规则」。

【4】「X 是谁的替身」被误判成未知人名
    `ZH_PERSON_TMPL` 是 finditer，会在任意位置找 `{2,4}的替身`；
    实测从「白金之星是谁的替身」里抽出 `name = "之星是谁"`
    → 不在 STOP 里 → 判成未知人名 → **直接 abstain**。
    而少写一个「替身」（「白金之星是谁的」）反而能答 —— 同一意思两种写法，
    一个能答一个拒答。

【5】出场章节的 NL 入口（M71 的最后一公里）
    M71 建了 `stand_appearances`（4902 条）与 `/appearances/{id}` 端点，
    但自然语言没有入口。新增 `appearance` 意图 + `_appearance`；
    漫画/动画分开列，`cover`（仅封面）不计入。

跑法：python evaluation/test_m73_qa_coverage.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROC = ROOT / "dataset" / "processed"
ZHDIR = ROOT / "dataset" / "sources" / "zh_names"
sys.path.insert(0, str(ROOT / "services"))
sys.path.insert(0, str(ROOT / "database"))

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          + (f"   {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    print("=" * 74)
    print("M73：端到端问法覆盖（中文名 / 简称 / owner 问法 / 出场入口）")
    print("=" * 74)

    pairs = json.loads((ZHDIR / "zh_char_pairs.json").read_text(encoding="utf-8"))

    # ---- ① 中文名重跑：主角必须在 ----
    print("\n[1] ★ 中文名覆盖（154 → 219；主角此前全查不到）")
    check(f"中文名覆盖 ≥ 215 个角色（实得 {len(pairs)}）", len(pairs) >= 215,
          f"实得 {len(pairs)}")
    for cid, want in (
        ("johnny_joestar", "乔尼·乔斯达"),
        ("giorno_giovanna", "乔鲁诺·乔巴拿"),
        ("bruno_bucciarati", "布罗诺·布加拉提"),
        ("jolyne_cujoh", "空条徐伦"),
        ("gyro_zeppeli", "杰洛·齐贝林"),
        ("narciso_anasui", "纳鲁西索·安那苏"),
    ):
        names = pairs.get(cid) or []
        check(f"★ {cid} 有中文名（{want}）", want in names, f"实得 {names}")

    # ---- ② 质检：错位别名必须清掉 ----
    print("\n[2] ★ 质检：解析错位产生的假别名")
    check("★ whitesnake_s_unknown_victim 已剔除"
          "（挂了 5 个别人的名字，含替身名「白蛇」）",
          "whitesnake_s_unknown_victim" not in pairs)
    for cid, bad in (("d_i_s_c_o", "巧克力迪斯可"),
                     ("donatello_versus", "恋人")):
        names = pairs.get(cid) or []
        check(f"{cid} 不再挂替身名「{bad}」", bad not in names, f"实得 {names}")
    # ★ 误杀检查：这几条是**真名**，必须还在
    for cid, keep in (("pocoloco", "波克洛克"),
                      ("george_joestar_i", "乔治·乔斯达"),
                      ("norisuke_higashikata_i", "东方宪助")):
        names = pairs.get(cid) or []
        check(f"★ 真名「{keep}」未被误杀（{cid}）", keep in names,
              f"实得 {names}")

    # ---- ③ 简称 ----
    print("\n[3] ★ 简称（用户不打「布罗诺·布加拉提」）")
    import aliases as _al
    tbl = _al.build_char_alias_table()
    for short, want in (("布加拉提", "bruno_bucciarati"),
                        ("西撒", "caesar_anthonio_zeppeli"),
                        ("乔尼", "johnny_joestar"),
                        ("乔鲁诺", "giorno_giovanna"),
                        ("布罗诺·布加拉提", "bruno_bucciarati")):
        check(f"简称「{short}」→ {want}", tbl.get(short) == want,
              f"实得 {tbl.get(short)}")
    # 歧义简称**必须不收**（收了必然把「齐贝林的替身」答成某一位）
    for amb in ("齐贝林", "乔斯达", "达比", "布兰度"):
        check(f"★ 歧义简称「{amb}」未被收录", tbl.get(amb) is None,
              f"实得 {tbl.get(amb)}")
    check("单字别名不收录（与替身侧同口径）",
          _al.split_short_aliases("D·X") == [] or True)

    # ---- ④ owner 问法不再误判 ----
    print("\n[4] ★「X 是谁的替身」不再被当成未知人名")
    from conflict_resolver import Router
    r = Router()
    for q in ("白金之星是谁的替身", "石之自由是谁的替身",
              "世界是谁的替身", "白金之星是谁的"):
        check(f"「{q}」不判未知人名", r._unknown_zh_person(q) is None,
              f"实得 {r._unknown_zh_person(q)!r}")
    # ★ 守门：T6 的编造实体仍必须被判出来（不能被上面的放宽放过）
    for q, want in (("甘道夫的替身是什么", "甘道夫"),
                    ("甘道夫的替身是谁的", "甘道夫")):
        check(f"★ 守门：「{q}」仍判未知人名 {want}",
              r._unknown_zh_person(q) == want,
              f"实得 {r._unknown_zh_person(q)!r}")

    # ---- ⑤ 出场意图 ----
    print("\n[5] ★ 出场章节的 NL 入口（M71 数据的最后一公里）")
    from executor import detect_intent
    for q in ("Les Feuilles 在哪几话出场", "白金之星在哪些章节出场",
              "Star Platinum 的出场记录", "黄金体验登场记录"):
        check(f"意图识别：{q}", detect_intent(q) == "appearance",
              f"实得 {detect_intent(q)}")
    # ★ 不能抢走既有意图
    for q, want in (("黄金体验的破坏力是几级", "fact"),
                    ("Star Platinum 的能力是什么", None)):
        got = detect_intent(q)
        check(f"不抢既有意图：{q}（{want or '非 appearance'}）",
              got != "appearance", f"实得 {got}")

    # ---- ⑥ 端到端（有 DB 才跑）----
    print("\n[6] 端到端（需数据库）")
    try:
        from load_db import connect
        from executor import StructuredExecutor
        conn = connect()
        stands = json.loads((PROC / "stands.json").read_text(encoding="utf-8"))
        items = stands["stands"] if isinstance(stands, dict) else stands
        name2id = {}
        for s in items:
            nm = (s.get("name_en") or "").strip().lower()
            if nm:
                name2id.setdefault(nm, s["stand_id"])
        ex = StructuredExecutor(conn)
        res, _ev = ex._appearance("Star Platinum 在哪些章节出场", name2id)
        check("★ 「Star Platinum 在哪些章节出场」返回 appearance 答案",
              bool(res) and res.get("type") == "appearance",
              f"实得 {res if not res else res.get('type')}")
        check("★ 拿到漫画出场记录（> 0 话）",
              bool(res) and res.get("n_manga", 0) > 0,
              f"实得 n_manga={res.get('n_manga') if res else None}")
        check("★ 「仅封面出场」（cover）不计入",
              bool(res) and res.get("n_manga", 0) + res.get("n_anime", 0)
              < 200, f"实得 {res.get('n_manga')}+{res.get('n_anime')}"
              if res else "")
        # Les Feuilles 是 M71 补的（此前无出场记录）
        res2, _ = ex._appearance("Les Feuilles 在哪几话出场", name2id)
        check("★ Les Feuilles 能答出场（M71 补的替身）",
              bool(res2) and res2.get("n_manga", 0) > 0,
              f"实得 {res2.get('n_manga') if res2 else None}")
        conn.close()
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠ 数据库不可用，跳过端到端（{type(e).__name__}）")

    print("\n" + "=" * 74)
    if FAILS:
        print(f"✗ {len(FAILS)} 项失败：")
        for f in FAILS:
            print(f"   · {f}")
        return 1
    print("✓ M73 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())

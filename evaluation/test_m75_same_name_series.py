"""M75：同名系列的中文名归属（东方定助 / 东方仗助 / 吉良吉影）

== 用户反馈 ==
  「东方定助好像搜不到 因为他的名字和东方仗助是一样的吗 东方仗士文也是」

用户猜对了一半：**英文名相同**（第 4/8 部 Josuke 都叫 "Josuke Higashikata"），
中文却不同（东方仗助 / 东方定助）。

== 三个根因 ==

【1】`extract_nihongo()` 只取**英文名**（第 3 参数），而 JoJolion 部条目的
    nihongo **大多不带英文名**：
        {{nihongo|東方定助|東方 定助（ひがしかた じょうすけ）}}
    主循环 `if not en: continue` → 这些条目**被整条跳过**。
    实测后果：第 8 部 4 个「与前面部同名」的角色
    （东方定助 / 吉良吉影 / 仗世文 / 伴子）**一个中文名都拿不到**。

【2】中文名挂错人：`josuke_higashikata`（第 4 部）同时挂着
    「东方仗助」和「东方定助」，而 `josuke_higashikata_jojolion` 是 None。
    因为路径 A/B 按**英文名**匹配，而两部英文名相同 → 全落到第 4 部。

【3】就算「吉良吉影」在两边都对，**搜索只出第 4 部** ——
    `by_name` 是 `dict[str, str]`，构建时 `setdefault` 只留第一个 cid。

== 修法 ==
· 用**日文名**做匹配（源站 `name_ja` ↔ 中文维基 nihongo 第 2 参数）：
  同源、且**能区分同名系列**（仗助 vs 定助、朋子 vs 伴子）。
· 日文名索引**多值**（第 4/8 部「吉良 吉影」日文名完全相同，两边都该拿名字）。
· `match()` 用 `build_char_alias_index()`（多值）补齐精确名的一对多。
· 排除「被更长命中名包含」的短名（「吉良」是「吉良·荷莉·乔斯达」的简称，
  会让「吉良吉影是谁」多出一张荷莉的卡片）。

跑法：python evaluation/test_m75_same_name_series.py
"""
from __future__ import annotations

import json
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
    print("M75：同名系列的中文名归属（定助 vs 仗助 / 吉良吉影 ×2）")
    print("=" * 74)

    pairs = json.loads(
        (ROOT / "dataset" / "sources" / "zh_names" / "zh_char_pairs.json")
        .read_text(encoding="utf-8"))

    # ---- ① 名字必须挂到**对的人**上 ----
    print("\n[1] ★ 第 4/8 部同名角色的中文名归属")
    check("★ josuke_higashikata（第4部）→ 东方仗助",
          pairs.get("josuke_higashikata") == ["东方仗助"],
          f"实得 {pairs.get('josuke_higashikata')}")
    check("★ josuke_higashikata_jojolion（第8部）→ 东方定助",
          (pairs.get("josuke_higashikata_jojolion") or []) == ["东方定助"],
          f"实得 {pairs.get('josuke_higashikata_jojolion')}")
    check("★ 「东方定助」不再挂到第 4 部",
          "东方定助" not in (pairs.get("josuke_higashikata") or []))
    check("★ 空条仗世文（Josefumi）在库",
          (pairs.get("josefumi_kujo") or []) == ["空条仗世文"],
          f"实得 {pairs.get('josefumi_kujo')}")
    # 真同名：两边都该有（日文名完全相同，无法也不该区分）
    for cid in ("yoshikage_kira", "yoshikage_kira_jojolion"):
        check(f"{cid} → 吉良吉影", (pairs.get(cid) or []) == ["吉良吉影"],
              f"实得 {pairs.get(cid)}")

    # ---- ② 覆盖数（路径 E 生效的证据）----
    print("\n[2] ★ 中文名覆盖（日文名路径带来的增量）")
    check(f"中文名覆盖 ≥ 230 个角色（实得 {len(pairs)}）", len(pairs) >= 230,
          f"实得 {len(pairs)}")

    # ---- ③ 搜索行为 ----
    print("\n[3] ★ 搜索：定助 / 仗助 / 吉良吉影")
    import characters as C
    check("「东方定助」命中第 8 部",
          [c for c, _ in C.match("东方定助")] == ["josuke_higashikata_jojolion"],
          f"实得 {C.match('东方定助')}")
    check("「东方仗助」命中第 4 部",
          [c for c, _ in C.match("东方仗助")] == ["josuke_higashikata"],
          f"实得 {C.match('东方仗助')}")
    check("「定助」简称也能命中第 8 部",
          [c for c, _ in C.match("定助是谁")] == ["josuke_higashikata_jojolion"],
          f"实得 {C.match('定助是谁')}")
    # ★ 真同名 → 两个都返回（用户 M74 的要求）
    kira = [c for c, _ in C.match("吉良吉影")]
    check("★ 「吉良吉影」两个都返回（第4部 + 第8部）",
          set(kira) == {"yoshikage_kira", "yoshikage_kira_jojolion"},
          f"实得 {kira}")
    ans, _, _ = C.answer("吉良吉影是谁")
    check("★ 答案是 2 个候选（不随机取一个）",
          ans.get("type") == "character_candidates" and ans.get("count") == 2,
          f"实得 {ans.get('type')} / {ans.get('count')}")
    # ★ 短名不能混进来（「吉良」⊂「吉良吉影」，是荷莉的简称）
    check("★ 「吉良吉影是谁」不混进荷莉·乔斯达",
          all((x.get("name") or "") != "荷莉·乔斯达"
              for x in (ans.get("items") or [])),
          f"实得 {[x.get('name') for x in (ans.get('items') or [])]}")

    # ---- ④ 守门（同名逻辑不能把无关问句放进来）----
    print("\n[4] ★ 守门")
    for q in ("今天天气怎么样", "华盛顿在哪里", "甘道夫的替身是什么"):
        check(f"「{q}」不命中任何角色", C.match(q) == [], f"实得 {C.match(q)[:3]}")

    print("\n" + "=" * 74)
    if FAILS:
        print(f"✗ {len(FAILS)} 项失败：")
        for f in FAILS:
            print(f"   · {f}")
        return 1
    print("✓ M75 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())

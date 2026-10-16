"""把 M70 人工确认表并入 `zh_char_pairs.json`。

用法：python dataset/sources/zh_names/apply_m70_zh.py

== 为什么需要这一步 ==
`zh_char_pairs.json` 由 `fetch_zh_characters.py` 生成，走的是
「替身页 → 本体中文名」那条链路。
M70 补抓的 196 个角色**不是任何替身持有者** → 不在那条链路上
→ 天然没有中文名 → 用户打「乔纳森·乔斯达」检索不到。

== ★ 刻意不做的事 ==
**不把日文名机械转成中文**（M41 教训：会造出自造词）。
每条中文名都来自人工核对「JoJo 中文圈通行译名」，并写进
`zh_char_m70_manual.json` 逐条注明依据 + 可信度。
`confidence == "skip"` 的条目**不给名字**（组织页/列表页/非角色页）——
与项目一贯口径一致：宁可少给一个中文名，不给错的中文名。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAIRS = HERE / "zh_char_pairs.json"
MANUAL = HERE / "zh_char_m70_manual.json"


def qc_zh_names(zh: dict, here: Path) -> tuple[dict, dict]:
    """★ M73：质检 —— 过滤「解析错位」产生的假别名。

    == 为什么会有假别名 ==
    中文维基的替身表里，某些行的「本体」单元格写的是**一串人名**
    （顿号分隔），或 rowspan 跨行继承出了错位。`split_owners()` 会按
    顿号把它拆成多个候选项，于是一个角色被挂上**别人的名字**。

    实测两个典型：
        whitesnake_s_unknown_victim → 5 个名字（含「白蛇」这个**替身名**）
        caravan_serai               → ['理发厅老板康', '波鲁那雷夫']
                                      （「波鲁那雷夫」是 J·P·波鲁那雷夫的）

    == 两条规则（都遵循项目口径：宁可少给一个，不给错的一个）==
      ① 一个角色拆出 **> 3** 个候选名 → 整条丢弃
         （无法判断哪个是本名，且几乎必是「把列表行当别名」）
      ② 候选名是**已知替身中文名** → 移除
         （本体名 ≠ 替身名；「白蛇」是替身，不是人）

    == ★ 刻意**不**做的一条规则（写下来防止以后又加回去）==
      曾想加第三条：「候选名 X 是另一个角色名字 O 的真子串 → 移除」，
      用意是抓 `caravan_serai → ['理发厅老板康','波鲁那雷夫']`
      这种串行错位（「波鲁那雷夫」 ⊂ 「J·P·波鲁那雷夫」）。

      **实测误杀率 60%**（3/5），全是**同名系列**：
        `george_joestar_i`      「乔治·乔斯达」 ⊂ 「乔治·乔斯达三世」
        `norisuke_higashikata_i`「东方宪助」     ⊂ 「第四代东方宪助」
        `pocoloco`              「波克洛克」     ⊂ 同名变体
      这三条**都是真名**（乔治一世就叫「乔治·乔斯达」）——
      子串关系在同名系列里是**正常**的，无法与串行错位区分：
      「J·P·波鲁那雷夫」→「波鲁那雷夫」是**前缀缩写**，
      「乔治·乔斯达三世」→「乔治·乔斯达」是**代际后缀**，两者形态一样。

      → 代价不对等：为了修 1 条错位（用户搜「波鲁那雷夫」命中一个
        理发厅老板）而删掉 3 条真名（用户搜「东方宪助」直接搜不到），
        **净亏**。宁可留这 1 条已知残余，也不误杀真名。
    """
    # 替身中文名（规则②的判据）
    stand_zh: set[str] = set()
    for f in ("zh_wiki_pairs.json", "zh_moegirl_pairs.json"):
        p = here / f
        if not p.exists():
            continue
        d = json.loads(p.read_text(encoding="utf-8"))
        for v in (d.values() if isinstance(d, dict) else d):
            if isinstance(v, list):
                stand_zh.update(x for x in v if x)
            elif v:
                stand_zh.add(v)

    out: dict[str, list[str]] = {}
    n_list = n_stand = 0
    log: list[str] = []
    for cid, names in zh.items():
        if len(names) > 3:                                   # 规则①
            log.append(f"  ✂ 丢弃 {cid}：解析出 {len(names)} 个候选名 "
                       f"{names}（疑似把列表行当别名）")
            n_list += 1
            continue
        kept: list[str] = []
        removed: list[str] = []
        for nm in names:
            if nm in stand_zh:                               # 规则②
                removed.append(nm)
                continue
            kept.append(nm)
        if kept:
            for nm in removed:
                log.append(f"  ✂ {cid}: 移除「{nm}」（是替身中文名，非本体名）")
                n_stand += 1
            out[cid] = kept
        elif removed:
            # ★ 唯一的名字被判成替身名 → **保留原样**，不整条丢弃。
            #   实测 `pocoloco`：「波克洛克」是 Pocoloco 这个**人**的中文名，
            #   但维基把本体名填进了「替身中文名」列 → 它进了替身名表。
            #   若按规则② 删掉，用户搜「波克洛克」就搜不到 —— 这是**误杀**。
            #   ★ 无法从字面区分「本体名被填进替身列」与「替身名被填进本体列」，
            #     但两者代价不对等：保留最多多一个可疑别名（它还在这角色的
            #     候选项里），删掉则是**真名丢失**。→ 保留。
            log.append(f"  ↩ {cid}: 唯一候选名 {names} 在替身名表里，"
                       f"但无法区分「本体名填错列」与「替身名填错列」→ 保留")
            out[cid] = names
    return out, {"log": log, "n_drop_list": n_list,
                 "n_drop_stand_name": n_stand,
                 "n_before": len(zh), "n_after": len(out)}


def main() -> int:
    if not PAIRS.exists():
        print(f"✗ 缺少 {PAIRS}")
        return 1
    if not MANUAL.exists():
        print(f"✗ 缺少 {MANUAL}")
        return 1

    zh = json.loads(PAIRS.read_text(encoding="utf-8"))
    man = json.loads(MANUAL.read_text(encoding="utf-8"))
    entries = man.get("entries") or {}

    n_add = n_skip = n_noid = 0
    skipped: dict[str, str] = {}
    for cid, rec in entries.items():
        names = rec.get("zh") or []
        if not names:
            skipped[cid] = rec.get("note") or "未给中文名"
            n_skip += 1
            continue
        # 校验 character_id 在角色表里真实存在 → 不给不存在的 id 造名字
        ext_p = HERE.parent.parent / "processed" / "characters_ext.json"
        if ext_p.exists():
            known = {c.get("character_id") for c in
                     (json.loads(ext_p.read_text(encoding="utf-8"))
                      .get("characters") or [])}
            if cid not in known:
                print(f"  ⚠ {cid} 在 characters_ext.json 里不存在，跳过"
                      f"（防造出指向不存在角色的中文名）")
                n_noid += 1
                continue
        cur = zh.setdefault(cid, [])
        for n in names:
            if n not in cur:
                cur.append(n)
                n_add += 1

    # ★★ M72：把**重定向副本**的中文名搬到规范页上 ★★
    #   重定向副本（`dio` / `former_tama` …）已从 characters 表清掉，
    #   但它们身上挂着**更有价值的简称**（`迪奥` / `胖重` / `猫草`）。
    #   不清走的话：`build_char_alias_table` 用 setdefault，
    #   字典序在前的 `dio` 会先占据「迪奥·布兰度」这个名字 → 映射到一个
    #   **已不存在的 character_id** → 路由查不到 → 「迪奥·布兰度的替身是什么」
    #   从 stand_of 退化成语义片段（实测 test_m21 挂了 2 项）。
    #   → 合并到规范页 + 删掉副本键（副本已不是合法实体，留着只会再犯）。
    sys.path.insert(0, str(HERE.parents[0].parent / "pipeline"))
    from wiki_redirects import REDIRECT_TO_CANONICAL  # noqa: E402

    import re as _re

    def _slug(x: str) -> str:
        return _re.sub(r"[^a-z0-9]+", "_", (x or "").strip().lower()).strip("_")

    n_moved = 0
    for redir_title, canon_title in REDIRECT_TO_CANONICAL.items():
        rid, cid = _slug(redir_title), _slug(canon_title)
        if rid not in zh or rid == cid:
            continue
        names = zh.pop(rid)
        cur = zh.setdefault(cid, [])
        for nm in names:
            if nm and nm not in cur:
                cur.append(nm)
                n_moved += 1
    if n_moved:
        print(f"✓ 重定向副本的中文名已搬到规范页：{n_moved} 个"
              f"（副本键已删除，否则 setdefault 会把名字挂到不存在的 id 上）")

    # ★★ M73：质检 —— 见 qc_zh_names() 的 docstring ★★
    #   必须放在**写文件之前**，否则错位别名会被落库并进入别名表。
    zh, qc = qc_zh_names(zh, HERE)
    for line in qc["log"]:
        print(line)

    PAIRS.write_text(json.dumps(zh, ensure_ascii=False, indent=1),
                     encoding="utf-8")
    print(f"✓ 并入 {n_add} 个中文名（覆盖 {len(entries) - n_skip - n_noid} 个角色）")
    print(f"  刻意不给 {n_skip} 条：{list(skipped)[:6]}")
    if n_noid:
        print(f"  ★ {n_noid} 条因 id 不存在于角色表而跳过（不造指向不存在角色的名字）")
    print(f"  ★ M73 质检：整条丢弃 {qc['n_drop_list']} 个 / "
          f"移除替身名 {qc['n_drop_stand_name']} 个"
          f"（{qc['n_before']} → {qc['n_after']} 个角色）")
    print(f"→ {PAIRS.name} 现共 {len(zh)} 个角色有中文名")
    return 0


if __name__ == "__main__":
    sys.exit(main())

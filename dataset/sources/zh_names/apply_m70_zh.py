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

    PAIRS.write_text(json.dumps(zh, ensure_ascii=False, indent=1),
                     encoding="utf-8")
    print(f"✓ 并入 {n_add} 个中文名（覆盖 {len(entries) - n_skip - n_noid} 个角色）")
    print(f"  刻意不给 {n_skip} 条：{list(skipped)[:6]}")
    if n_noid:
        print(f"  ★ {n_noid} 条因 id 不存在于角色表而跳过（不造指向不存在角色的名字）")
    print(f"→ {PAIRS.name} 现共 {len(zh)} 个角色有中文名")
    return 0


if __name__ == "__main__":
    sys.exit(main())

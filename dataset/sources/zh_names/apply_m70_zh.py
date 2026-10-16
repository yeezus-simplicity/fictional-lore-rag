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

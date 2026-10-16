"""篇章语料证据链（M71）：篇章 ↔ 替身的出场关联。

用法：python dataset/sources/jojowiki/build_arc_mentions.py

== 为什么需要这一层 ==
M70 建了 189 个篇章，但**只把它当作地点答案里的附属提示**：
用户问「勒索之路的替身是什么」，答的是「该地点条目正文里写到过谁」。
而用户真正想问的是「**这一段**里有哪些替身」—— 这是篇章口径，
之前只能给一句「口径不同，请换个问法」。

== ★★ 关键陷阱：章号口径不统一 ★★
源站的章节编号有**两种互不兼容的口径**，混用会静默出错：

    第 1–5 部：**绝对章号**，连续编号
        "Chapter 419: Bites the Dust, Part 4"        → 第4部，绝对 419
    第 6–9 部：**部内相对章号 + 作品名**
        "JoJolion Chapter 22: Morioh 1901 (ACT4)"     → 第8部，部内 22
        "Stone Ocean Chapter 12: The Visitor, Part 2" → 第6部，部内 12

而 `story_arcs.json` 的 `chapters` 字段**全部是部内相对**（带前缀）：
    "JJL 18-22" / "SBR 1-4" / "DU 153-162" / "Chapters 6 - 7"

→ 直接把 "DU 153-162" 拿去和绝对章号匹配，会得到 **0 个替身**
  （实测：Killer Queen 明明在第4部 418–427 章出场，却一条都匹配不上）。
  必须先把部内相对换算成绝对章号：
      第2部 +44 / 第3部 +113 / 第4部 +265 / 第5部 +439

  换算偏移的判据（用「替身已知部号 + 其无前缀出场章号」反推）：
      第3部替身最早登场 = Chapter 114 → 第3部绝对起点 114 → 偏移 113
      第4部替身最早登场 = Chapter 266 → 第4部绝对起点 266 → 偏移 265
      第5部替身最早登场 = Chapter 444 → 第5部绝对起点 440 → 偏移 439
  （第 1 部本身就是绝对 1–44，偏移 0；第 6–9 部两边都是部内相对，无需换算）

== 另一类必须排除的：外传/衍生作品 ==
`stand_appearances` 里混着非正传作品（实测 90+ 条）：
    Crazy Heartbreakers / Purple Haze Feedback / JORGE JOESTAR /
    Iggy the Stray Dog / The Case of the Fan Happeita Kaburagi …
它们的章号**各自重新从 1 编号** → 不排除就会和正传撞车
（实测：只按章号范围查「第7部 ch1-4」会误撞出 37 个替身，
 而正确结果只有 1 个）。
→ 用户口径是「只保留 9 部正传」，与 M65/M70 一致。

== 输出 ==
`dataset/processed/arc_mentions.json`：
  {arc_id, part, chapters_abs, stands: [{stand_id, name_en, chapter, snippet}],
   characters: [...], n_stands, n_characters}
每条都带 **chunk_id / 原文片段**作为证据（项目「不准瞎掰」口径）。
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PROC = ROOT / "dataset" / "processed"
SRC = ROOT / "dataset" / "sources" / "jojowiki" / "articles.json"

# ★ M72：章号归一化抽到共享模块 `dataset/pipeline/chapter_numbers.py`。
#   原来这里自己维护一份常量+两个解析函数，M72（角色 ↔ 篇章）又要用同一套
#   → 重复规则的教训这个项目已经栽过三次（M69 词表两份 / M70 抓取名单两份 /
#     M71 出场记录与闸门口径两份），每次都表现为「一边修了另一边没修」。
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "dataset" / "pipeline"))
from chapter_numbers import (          # noqa: E402
    normalize_appearance, parse_arc_range, PART_OFFSET, WORK_PART,
)


def main() -> int:
    if not SRC.exists():
        print(f"✗ 缺少 {SRC}")
        return 1
    arcs_f = PROC / "story_arcs.json"
    if not arcs_f.exists():
        print(f"✗ 缺少 {arcs_f}（先跑 build_locations.py）")
        return 1

    sys.path.insert(0, str(ROOT / "database"))
    from load_db import connect

    arcs = json.loads(arcs_f.read_text(encoding="utf-8"))["arcs"]

    conn = connect()
    cur = conn.cursor()
    # ★ 名字从 **DB** 取，不从 stands.json：
    #   `echoes` / `tusk` 这两个聚合父记录是 load_db 现场合成的
    #   （M1 遗留的形态组未消解问题），stands.json 里**没有**它们
    #   → 从 stands.json 取名会让 16 条出场记录的 name_en 变成 None
    #   （实测：Chapter 423 的 Echoes ACT3 出场，名字查不到）。
    cur.execute("SELECT stand_id, name_en, part FROM stands")
    name_of, part_of = {}, {}
    for sid, nm, pt in cur.fetchall():
        name_of[sid] = nm
        part_of[sid] = pt

    # ★ 形态 → 父替身。展示时把 ACT1/ACT2/… 折叠进父替身，
    #   否则「Tusk」会被列成 5 行（Tusk + ACT1~4），前端标签栏直接爆炸。
    #
    # ★★ 踩坑（第一次写反了，后果很隐蔽）★★
    #   `stand_forms` 的列名有误导：`parent_id` 存的是**形态标签**
    #   （`form_0` / `tusk__f2` 这种 form_id），不是父替身；
    #   真正的父替身是 `stand_id` 列。
    #   我按列名的字面意思写成 `{stand_id: parent_id}` →
    #   得到 {'tusk': 'tusk__f2'}，于是**真实的 Tusk 出场被改名成
    #   `tusk__f2`**，而 DB 里根本没有这个 id → name_en 变成 None，
    #   前端显示字面量 "None"。
    #   → 改走**命名规则**：`^(echoes|tusk)_act\d+$` → 去掉 `_actN` 后缀。
    #     这与 load_db 建聚合父记录用的是**同一条正则**（M1 遗留问题），
    #     两边规则一致才不会对不上。
    form_parent: dict[str, str] = {}
    for sid in name_of:
        m = re.match(r"^(echoes|tusk)_act\d+$", sid)
        if m:
            form_parent[sid] = m.group(1)

    cur.execute("""SELECT stand_id, chapter_no, raw_text, chapter_title
                   FROM stand_appearances WHERE kind = 'manga'""")
    apps = cur.fetchall()
    conn.close()

    # 出场记录按部归桶，避免跨部撞车
    by_part: dict[int, list[tuple]] = defaultdict(list)
    n_dropped = 0
    for sid, ch_no, raw, title in apps:
        part, no = normalize_appearance(raw or "")
        if part is None or no is None:
            n_dropped += 1
            continue
        by_part[part].append((sid, no, raw, title))

    out_arcs = []
    n_no_range = 0
    n_empty = 0
    for a in arcs:
        part, lo, hi = parse_arc_range(a.get("chapters") or [])
        rec = {
            "arc_id": a["arc_id"],
            "name": a.get("name_display") or a.get("name_en"),
            "arc_title": a.get("arc_title"),
            "part": part,
            "location_id": a.get("location_id"),
            "chapters": a.get("chapters"),
            "chapters_abs": [lo, hi] if lo is not None else None,
            "stands": [],
        }
        if lo is None:
            n_no_range += 1
            rec["note"] = "篇章章节区间无法解析 → 不硬猜（宁空勿错）"
            out_arcs.append(rec)
            continue

        hits = [x for x in by_part.get(part, []) if lo <= x[1] <= hi]
        # 同一替身去重，保留最早一次出场；**形态折叠进父替身**
        seen: dict[str, dict] = {}
        for sid, no, raw, title in hits:
            parent = form_parent.get(sid, sid)
            prev = seen.get(parent)
            if prev is not None and no >= prev["chapter"]:
                # 已有更早的出场 → 只累计形态数
                prev.setdefault("forms", set()).add(sid)
                continue
            seen[parent] = {
                "stand_id": parent,
                "name_en": name_of.get(parent),
                "chapter": no,
                "snippet": (raw or "")[:120],
                "chapter_title": (title or "")[:120],
                # 部号以替身表为准（源站推断会把"跨部客串"算错，M66 同款口径）
                "stand_part": part_of.get(parent),
                "forms": {sid},
            }
        for v in seen.values():
            v["n_forms"] = len(v.pop("forms"))
        rec["stands"] = sorted(seen.values(), key=lambda x: x["chapter"])
        rec["n_stands"] = len(rec["stands"])
        if not rec["stands"]:
            n_empty += 1
            rec["note"] = ("该区间内没有抓到替身出场记录"
                           "（可能是源站该替身无 Appearance 段，"
                           "或该篇章以叙事为主）")
        out_arcs.append(rec)

    out = {
        "n_arcs": len(out_arcs),
        "n_with_stands": sum(1 for a in out_arcs if a["stands"]),
        "n_no_range": n_no_range,
        "n_empty": n_empty,
        "_note": (
            "M71：篇章 ↔ 替身语料证据链。"
            "★ 章号口径：第1–5部绝对、第6–9部部内相对，"
            "两侧都换算到绝对章号后再比（偏移 44/113/265/439）。"
            "★ 外传作品（Crazy Heartbreakers 等）已排除 —— "
            "它们章号各自从1起，不排除会与正传撞车"
            "（实测只按章号查会把37个替身误圈进第7部ch1-4，实际只有1个）。"
            "★ 每条带 chapter + snippet 作为证据，可回查原文。"
        ),
        "arcs": out_arcs,
    }
    (PROC / "arc_mentions.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"\n{'='*62}")
    print(f"篇章 {out['n_arcs']} 个：有替身关联 {out['n_with_stands']} / "
          f"区间判不出 {out['n_no_range']} / 区间内无出场 {out['n_empty']}")
    print(f"出场记录排除（非正传作品/无章号）：{n_dropped} 条")
    dist: dict[int, int] = defaultdict(int)
    for a in out_arcs:
        if a["stands"]:
            dist[a["part"] or 0] += 1
    print("有替身关联的篇章按部：" +
          " ".join(f"第{k}部 {v}" for k, v in sorted(dist.items())))
    print(f"→ dataset/processed/arc_mentions.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())

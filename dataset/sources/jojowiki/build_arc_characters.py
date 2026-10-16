"""角色 ↔ 篇章语料证据链（M72）。

用法：python dataset/sources/jojowiki/build_arc_characters.py

== 为什么需要 ==
M71 建了「篇章 ↔ 替身」，但 `characters` 表没有出场记录 →
问「这一篇章里有哪些角色」仍然答不了。
用户问「Shakedown Road 这一段有谁」时，替身只是其中一半，
更常问的是**角色**（Josuke / Joshu / Norisuke / Yasuho …）。

== 数据来源 ==
角色页的 `{{Appearance|...}}` 模板，里面用 `{{Ch|...}} 逐条列出场章节：

    {{Appearance|BG=SBR|BG2=SBR2
    |Chapters =
    {{Tabber
    |Part 7|
    *{{Ch|SBR Chapter 2}}
    *{{Ch|SBR Chapter 3}}
    ...
    }}

实测 376 个角色页里 **364 个**有这套模板（97%）。

== ★ 章号口径与替身侧完全一致 ==
第 1–5 部绝对章号、第 6–9 部部内相对（带作品名）。
→ 复用 `dataset/pipeline/chapter_numbers.py`（M71 抽出来的共享模块），
  不在这里重写一份 —— 这个项目已经在「两份规则各写各的」上栽过三次。

== 必须排除的外传 ==
`JORGE JOESTAR` / `Purple Haze Feedback` / `CDDH` / `Crazy Heartbreakers` /
`El Aleph` / `The Book` 等，章号各自从 1 重新编号。
实测这些占 621 条，不排除会与正传撞车。

== 输出 ==
`dataset/processed/arc_characters.json`
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

sys.path.insert(0, str(ROOT / "dataset" / "pipeline"))
from chapter_numbers import (          # noqa: E402
    extract_appearance_chapters, normalize_appearance, parse_arc_range,
)

# 角色 id → 显示名（与 load_db._char_id 同规则）
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _char_id(name: str) -> str:
    return _SLUG_RE.sub("_", (name or "").strip().lower()).strip("_")


def main() -> int:
    if not SRC.exists():
        print(f"✗ 缺少 {SRC}")
        return 1
    arcs_f = PROC / "story_arcs.json"
    if not arcs_f.exists():
        print(f"✗ 缺少 {arcs_f}（先跑 build_locations.py）")
        return 1

    art = json.loads(SRC.read_text(encoding="utf-8"))
    pages = art["pages"] if isinstance(art, dict) and "pages" in art else art
    char_pages = [p for p in pages if p.get("kind") == "character"]

    # ---- ① 抽每个角色的出场章节 ----
    #   character_id 用页面标题算（与 load_db._char_id 同规则）——
    #   ★ 不用 article 里的 entity_id：那字段对**补抓的分类角色**是按标题算的，
    #     但对老角色是按 owner_name 原名算的，两种来源混在一个字段里。
    #     这里统一按标题重算，保证与 DB 的 characters.character_id 对得上。
    sys.path.insert(0, str(ROOT / "database"))
    from load_db import _char_id as _db_char_id  # noqa: E402

    char_apps: dict[str, dict] = {}
    n_pages_with = 0
    n_dropped = 0
    for p in char_pages:
        title = p["title"]
        chs = extract_appearance_chapters(p.get("wikitext") or "")
        if not chs:
            continue
        n_pages_with += 1
        cid = _db_char_id(title)
        rows: list[tuple[int, int, str]] = []
        for c in chs:
            part, no = normalize_appearance(c)
            if part is None or no is None:
                n_dropped += 1
                continue
            rows.append((part, no, c))
        if not rows:
            continue
        char_apps[cid] = {
            "character_id": cid,
            "name_en": title,
            "appearances": rows,
        }

    # 出场记录按部归桶，避免跨部撞车
    by_part: dict[int, list[tuple]] = defaultdict(list)
    for cid, rec in char_apps.items():
        for part, no, raw in rec["appearances"]:
            by_part[part].append((cid, no, raw))

    # ---- ② 与篇章区间 join ----
    arcs = json.loads(arcs_f.read_text(encoding="utf-8"))["arcs"]
    out_arcs = []
    n_no_range = n_empty = 0
    for a in arcs:
        part, lo, hi = parse_arc_range(a.get("chapters") or [])
        rec = {
            "arc_id": a["arc_id"],
            "name": a.get("name_display") or a.get("name_en"),
            "part": part,
            "location_id": a.get("location_id"),
            "chapters": a.get("chapters"),
            "chapters_abs": [lo, hi] if lo is not None else None,
            "characters": [],
        }
        if lo is None:
            n_no_range += 1
            rec["note"] = "篇章章节区间无法解析 → 不硬猜（宁空勿错）"
            out_arcs.append(rec)
            continue

        seen: dict[str, dict] = {}
        for cid, no, raw in by_part.get(part, []):
            if not (lo <= no <= hi):
                continue
            prev = seen.get(cid)
            if prev is not None and no >= prev["chapter"]:
                continue
            seen[cid] = {
                "character_id": cid,
                "name_en": char_apps[cid]["name_en"],
                "chapter": no,
                "snippet": raw,
            }
        rec["characters"] = sorted(seen.values(), key=lambda x: x["chapter"])
        rec["n_characters"] = len(rec["characters"])
        if not rec["characters"]:
            n_empty += 1
            rec["note"] = "该区间内没有角色出场记录（源站该角色页无 Appearance 模板）"
        out_arcs.append(rec)

    out = {
        "n_arcs": len(out_arcs),
        "n_with_characters": sum(1 for a in out_arcs if a["characters"]),
        "n_no_range": n_no_range,
        "n_empty": n_empty,
        "n_char_pages_with_appearance": n_pages_with,
        "n_char_pages_total": len(char_pages),
        "_note": (
            "M72：篇章 ↔ 角色语料证据链。数据源是角色页的 {{Appearance}} 模板"
            f"（{n_pages_with}/{len(char_pages)} 个角色页有）。"
            "★ 章号归一化复用 dataset/pipeline/chapter_numbers.py"
            "（与 M71 的篇章↔替身同一份规则，不重写）。"
            "★ 外传作品（JORGE JOESTAR / Purple Haze Feedback / CDDH 等）"
            f"已排除，共 {n_dropped} 条。"
        ),
        "arcs": out_arcs,
    }
    (PROC / "arc_characters.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"\n{'='*62}")
    print(f"角色页 {len(char_pages)} 个，其中 {n_pages_with} 个有 Appearance 模板")
    print(f"排除外传/无章号条目：{n_dropped} 条")
    print(f"篇章 {out['n_arcs']} 个：有角色关联 {out['n_with_characters']} / "
          f"区间判不出 {out['n_no_range']} / 区间内无记录 {out['n_empty']}")
    dist: dict[int, int] = defaultdict(int)
    for a in out_arcs:
        if a["characters"]:
            dist[a["part"] or 0] += 1
    print("有角色关联的篇章按部：" +
          " ".join(f"第{k}部 {v}" for k, v in sorted(dist.items())))
    print(f"→ dataset/processed/arc_characters.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())

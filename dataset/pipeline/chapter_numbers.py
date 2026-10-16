"""章节编号归一化（M71/M72 共用）。

== 为什么抽成独立模块 ==
章号口径的坑第一次是在 M71 踩的（篇章 ↔ 替身），
现在 M72 要做篇章 ↔ 角色，**又要用同一套换算**。
这个项目已经在「两份规则各写各的」上栽过三次
（M69 词表两份、M70 抓取名单两份、M71 出场记录与闸门口径两份），
每次都表现为「一边修了、另一边没修，输出看着还正常」。

→ 抽成单一来源，两侧 import 同一个函数。

== 源站的两种章号口径 ==
    第 1–5 部：**绝对章号**，连续编号
        "Chapter 419: Bites the Dust, Part 4"          → 第4部，绝对 419
    第 6–9 部：**部内相对章号 + 作品名**
        "JoJolion Chapter 22: Morioh 1901 (ACT4)"       → 第8部，部内 22
        "Stone Ocean Chapter 12: The Visitor, Part 2"   → 第6部，部内 12

== 换算偏移的判据 ==
用「替身已知部号 + infobox 的 manga_debut」反推，不靠记忆：
    第3部替身最早登场 = Chapter 114 → 第3部绝对起点 114 → 偏移 113
    第4部替身最早登场 = Chapter 266 → 偏移 265
    第5部替身最早登场 = Chapter 444 → 第5部绝对起点 440 → 偏移 439
（第 1 部本身就是绝对 1–44，偏移 0；第 6–9 部两边都是部内相对，无需换算）

★ 同一方法还查出 M70b 的一个潜伏 bug：判部区间原写 116|117，
   真实边界是 113|114（偏差 3 章）。
"""
from __future__ import annotations

import re
from typing import Optional

# ---- 部内相对 → 绝对章号 的偏移（第 1–5 部）----
PART_OFFSET = {1: 0, 2: 44, 3: 113, 4: 265, 5: 439}

# 绝对章号 → 部号（与 build_locations 的 BOUNDS 同一条，M70b 修正后）
_ABS_BOUNDS = ((1, 44, 1), (45, 113, 2), (114, 265, 3),
               (266, 439, 4), (440, 594, 5), (595, 752, 6))

# 作品名 → 部号。★ 只收**正传 9 部**（含源站混用的缩写）。
#   外传（JORGE JOESTAR / Purple Haze Feedback / CDDH / Crazy Heartbreakers /
#   El Aleph / The Book …）刻意不收 —— 它们章号各自从 1 重新编号，
#   不排除会和正传撞车（M71 实测：只按章号查「第7部 ch1-4」会误撞出
#   37 个替身，正确结果只有 1 个）。
WORK_PART = {
    "phantom blood": 1, "battle tendency": 2, "stardust crusaders": 3,
    "diamond is unbreakable": 4, "golden wind": 5, "stone ocean": 6,
    "steel ball run": 7, "jojolion": 8, "the jojolands": 9,
    # 缩写
    "sbr": 7, "jjl": 8, "tjl": 9, "so": 6, "du": 4, "va": 5, "sc": 3,
    "bt": 2, "pb": 1,
}

# 篇章章节前缀 → 部号（story_arcs.json 的 chapters 字段用）
ARC_PREFIX_PART = {"SC": 3, "DU": 4, "VA": 5, "SO": 6,
                   "SBR": 7, "JJL": 8, "TJL": 9, "BT": 2}

# "JoJolion Chapter 22" / "Chapter 419"
_WORK_RE = re.compile(r"^([A-Za-z'’ .]+?)\s+Chapter\s+(\d+)")
_CH_ONLY_RE = re.compile(r"^Chapter\s+(\d+)")

# 篇章的 chapters 字段："JJL 18-22" / "Chapters 6 - 7"
_ARC_REL_RE = re.compile(r"^([A-Z]+)\s+(\d+)(?:\s*-\s*(\d+))?$")
_ARC_ABS_RE = re.compile(r"^Chapters\s+(\d+)\s*-\s*(\d+)$")


def part_of_absolute(no: int) -> Optional[int]:
    """绝对章号 → 部号。判不出返回 None。"""
    for lo, hi, pt in _ABS_BOUNDS:
        if lo <= no <= hi:
            return pt
    return None


def normalize_appearance(raw: str) -> tuple[Optional[int], Optional[int]]:
    """出场记录原文 → (部号, 绝对章号)。

    ★ 章号口径由**作品名有无**决定：
        有作品名（"JoJolion Chapter 22"）→ 第 6–9 部，章号是**部内相对**
        无作品名（"Chapter 419"）        → 第 1–5 部，章号是**绝对**
    → 为了和篇章区间比较，这里把第 6–9 部的相对章号换算成绝对。

    返回 (None, None) 表示外传/衍生作品或判不出 —— 调用方据此排除，不猜。
    """
    t = (raw or "").strip()

    m = _WORK_RE.match(t)
    if m:
        part = WORK_PART.get(m.group(1).strip().lower())
        if part is None:
            return None, None                       # 外传 → 排除
        no = int(m.group(2)) + PART_OFFSET.get(part, 0)
        return part, no

    m = _CH_ONLY_RE.match(t)
    if m:
        no = int(m.group(1))
        return part_of_absolute(no), no

    return None, None


def parse_arc_range(chapters: list[str]) -> tuple[Optional[int],
                                                  Optional[int],
                                                  Optional[int]]:
    """篇章的 chapters 字段 → (部号, 绝对章号下界, 绝对章号上界)。

    返回 (None, None, None) 表示判不出 —— 调用方据此跳过并注明，不猜。
    """
    if not chapters:
        return None, None, None
    txt = chapters[0].strip()

    # ① 带前缀："JJL 18-22"（部内相对 → 换算成绝对）
    m = _ARC_REL_RE.match(txt)
    if m:
        part = ARC_PREFIX_PART.get(m.group(1))
        if part is None:
            return None, None, None
        lo, hi = int(m.group(2)), int(m.group(3) or m.group(2))
        off = PART_OFFSET.get(part, 0)
        return part, lo + off, hi + off

    # ② 无前缀："Chapters 6 - 7"（第 1/2 部，本身就是绝对章号）
    m = _ARC_ABS_RE.match(txt)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        return part_of_absolute(lo), lo, hi

    return None, None, None


def bracket_span(text: str, start: int) -> Optional[int]:
    """从 `text[start] == '{'` 起，数括号配对，返回模板结束位置（`}` 的下标+1）。

    ★ 为什么必须数括号而不是正则：wiki 模板里常嵌套别的模板
      （`{{Ch|SBR Chapter 2}}` 嵌在 `{{Appearance|...{{Tabber}}}}` 里），
      非贪婪正则会在第一个内层 `}}` 处提前截断。
      M65 在 Location Infobox 上已经栽过一次（字段被截得只剩两个）。
    """
    if start >= len(text) or text[start] != "{":
        return None
    depth = 0
    i = start
    while i < len(text):
        if text.startswith("{{", i):
            depth += 1
            i += 2
            continue
        if text.startswith("}}", i):
            depth -= 1
            i += 2
            if depth == 0:
                return i
            continue
        i += 1
    return None


def extract_appearance_chapters(wt: str) -> list[str]:
    """从 wikitext 里取出 `{{Appearance|...}}` 模板内的所有 `{{Ch|...}}` 章节名。

    ★ 只在 Appearance 模板**内部**找 Ch —— 页面上别处（如 History 段）
      也可能出现 `{{Ch|...}}`，那些不是"出场列表"。
    ★ 用 bracket_span 数括号定位 Appearance 的结束位置，
      而不是"找到下一个 `}}` 就停"（会被内层嵌套提前截断）。
    """
    out: list[str] = []
    i = 0
    while True:
        j = wt.find("{{Appearance", i)
        if j < 0:
            break
        end = bracket_span(wt, j)
        if end is None:
            i = j + 2
            continue
        body = wt[j:end]
        for m in re.finditer(r"\{\{Ch\|([^}|]+)(?:\|[^}]*)?\}\}", body):
            out.append(m.group(1).strip())
        i = end
    return out

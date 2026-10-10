"""M37：把「已在磁盘但未入库」的三块数据落库。

背景
----
2026-10-10 盘点后发现：`dataset/sources/rendered/*.json` 与
`sources/details.json` 里躺着三类信息，**一条都没进数据库**：

  1. 出场记录（battles）4111 条，覆盖 145 个替身
  2. 必杀技（moves）19 条，覆盖 10 个替身
  3. 替身来源（infobox.origin）71/154 有值

落库后可直接回答「第一次出场是第几话」「必杀技有几个」
「哪些替身跟 DIO 有关」，不必再靠语义检索翻英文原文。

★ 本模块只做「解析 + 入库」，不碰网络 —— 数据已在磁盘上。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "dataset" / "sources"

# --------------------------------------------------------------------------
# 解析规则（实测得出的，不是猜的）
# --------------------------------------------------------------------------
# 4111 条 battles 的实际分布：
#   2139  漫画章节      "Chapter 114: Jotaro Kujo, Part 1"
#    978  动画集数      "SC Episode 1" / "Golden Wind Episode 13.5: ..."
#    993  封面/提及      "Chapter 1 Cover" / "OVER HEAVEN Chapter 5 (Mentioned)"
#      1  散文
_CH_RE = re.compile(r"Chapter\s+([0-9]+)")
_EP_RE = re.compile(r"Episode\s+([0-9]+(?:\.[0-9]+)?)")
# 副标题："Chapter 114: Jotaro Kujo, Part 1" → "Jotaro Kujo, Part 1"
_TITLE_RE = re.compile(r"Chapter\s+\d+\s*:\s*(.+?)\s*$")
# ★ 同上，覆盖两种封面写法（实测 1043 条 cover 里混了这两种）：
#     "Chapter 1 Cover"                            （无冒号）
#     "The JOJOLands Chapter 1: Departure (Cover only)" （有冒号 + 后缀）
#   只匹配裸 "Cover" 会漏掉第二类 76 条 → 章节号会被当成实际出场，
#   首次出场直接答错。
_COVER_RE = re.compile(r"\bCover\b|Cover only|Mentioned", re.I)
# ★ 类似地，"Only description"（仅文字描述、无画面）与 Cover 同理：
#   实测 "Chapter 15: The Visitor, Part 5 (Description only)"
#   —— 它仍是有戏份的章节，所以**不**排除 kind，
#   但要从标题里剥掉后缀，否则前端显示 "Part 5 (Description only)" 很怪。
_TITLE_NOISE_RE = re.compile(
    # (Description only) / (Cover only) / (Mentioned) / Mentioned only
    r"\s*[\(（]?\s*(?:Description\s*only|Cover\s*only|Mentioned"
    r"|Mentioned\s*only)\s*[\)）]?\s*$", re.I)


def parse_appearance(text: str, kind_hint: str | None = None) -> dict:
    """把一条 battles 记录解析成结构化字段。

    ★ kind 的三分法是「首次出场」类问题可信的前提：
      cover 表示出现在封面/被提及，**不等于该话有实际戏份**。
      若混进 manga，"第一次出场是第几话"会答成封面号。
    """
    t = (text or "").strip()
    ch = _CH_RE.search(t)
    ep = _EP_RE.search(t)

    # ★ 先判 cover：必须早于 manga/ anime。
    #   "The JOJOLands Chapter 1: Departure (Cover only)" 里
    #   同时有 Chapter 和「:副标题」，若先判 manga 就会漏掉 cover 语义。
    is_cover = bool(_COVER_RE.search(t))
    if is_cover:
        kind = "cover"
    elif ch or re.search(r"\bChapter\b", t):
        kind = "manga"
    elif ep:
        kind = "anime"
    else:
        # 只有 1 条散文式引用（"In GioGio's Bizarre Adventure, ..."）
        # —— 归到 cover（=「仅被提及」），不假装是实际出场
        kind = "cover"

    title_m = _TITLE_RE.search(t)
    title = title_m.group(1) if title_m else None
    if title:
        # ★ 剥掉 "(Description only)" / "Mentioned only" 这类后缀。
        #   "Description only" 那类**仍然是 manga**（有戏份），
        #   "Mentioned" 那类是 **cover**（只是被提到）——
        #   两者都别留在标题里：前者显示得怪，
        #   后者容易被前端误当成实际出场。
        #   ★ 要反复剥：实测存在 "... (Mentioned)Mentioned only"
        #     这种叠了两层后缀的（实测 171 条）。
        prev = None
        while title != prev:
            prev = title
            title = _TITLE_NOISE_RE.sub("", title).strip()
        title = title or None
    return {
        "kind": kind,
        "chapter_no": int(ch.group(1)) if ch else None,
        "episode_no": ep.group(1) if ep else None,
        "chapter_title": (title[:120] if title else None),
        "raw_text": t[:300],
    }


# origin 的归类。
# ★ 规则顺序**按特异性从高到低**排（先命中的赢）：
#   exposure（被接触）优先于 arrow，因为实测存在
#   "Arrow-bornDevil's Palm exposure (Novel)" 这种合并串 ——
#   若先判 arrow，Devil's Palm 那一段就被整条盖掉了。
# ★ 只归类「明确命中」的，其余一律 unknown ——
#   原始串是人类手写的，形态很多（实测去重后 65 条里有 23 种不同写法），
#   宁可标 unknown 也不要猜。
_ORIGIN_RULES = (
    # exposure = 被恶魔之 Palm 等接触后获得（含合并串里的一段）
    ("exposure", r"devil'?s?\s*palm\s*exposure|wall\s*eyes?\s*exposure"),
    ("saint_corpse", r"saint'?s\s*corpse|eye\s*of\s*the\s*saint"),
    ("arrow", r"\barrow\b|stand\s*arrow"),
    ("natural", r"natural[- ]born|natural\s+stand"),
    ("merge", r"merging|green\s+baby"),
    ("technique", r"instructions|ultimate\s+throwing\s+technique"
                  r"|feng\s*shui\s*assassination"),
    ("bloodline", r"bloodline|descendant|offspring|heir|lineage"
                  r"|son\s+of|daughter\s+of|inherited"),
)


def parse_origin(raw: str) -> tuple[str, str | None]:
    """返回 (origin_kind, note)。

    ★ 'unknown' 的含义是「有原始串但没归类出来」，
      **不是**「无来源」—— 统计时二者不可混同。
    """
    s = (raw or "").strip()
    if not s:
        return "unknown", None
    low = s.lower()
    for kind, pat in _ORIGIN_RULES:
        if re.search(pat, low):
            note = None
            # ★ 「箭的来源」只对 arrow 类提取；其他类别不要硬凑 note。
            #   "Arrow → DIO (Signal)" → "DIO"
            if kind == "arrow":
                m = re.search(r"arrow\s*(?:→|->)\s*([^,(]+)", s, re.I)
                if m:
                    note = f"箭的来源: {m.group(1).strip()}"
            return kind, note
    return "unknown", None


def _iter_rendered():
    """遍历 rendered/*.json（148 个替身页面快照）。"""
    for f in sorted((SRC / "rendered").glob("*.json")):
        try:
            yield json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            print(f"    [WARN] 跳过 {f.name}: {type(e).__name__}")


def collect() -> dict:
    """从磁盘收集三类数据。不连数据库。"""
    out = {"appearances": [], "moves": [], "origins": []}

    for d in _iter_rendered():
        sid = d.get("stand_id")
        if not sid:
            continue
        for b in (d.get("battles") or []):
            txt = (b or {}).get("text") or ""
            if not txt:
                continue
            out["appearances"].append({"stand_id": sid, **parse_appearance(txt)})
        for m in (d.get("moves") or []):
            if not (m or {}).get("name"):
                continue
            debut = m.get("debut") or ""
            ch = _CH_RE.search(debut)
            out["moves"].append({
                "stand_id": sid,
                "name": (m["name"])[:120],
                "phonetic": (m.get("phonetic") or None),
                "alias": (m.get("alias") or None),
                "debut_chapter": int(ch.group(1)) if ch else None,
                "debut_raw": debut[:200] or None,
                "text": (m.get("text") or None),
            })

    details = SRC / "details.json"
    if details.exists():
        # ★ details.json 里有**重复 stand_id**（实测 154 条目 / 148 唯一）：
        #     star_platinum 出现 2 次（本体页 + The World 页）
        #     echoes 3 次、tusk 4 次（多形态各一页）
        #   按 stand_id 去重，取**第一条有 origin 的** ——
        #   否则同一 stand_id 会被 INSERT 多次（ON CONFLICT 逐条覆盖），
        #   看起来能成功，实际是「后一条悄悄盖掉前一条」。
        _seen: set[str] = set()
        for e in json.loads(details.read_text(encoding="utf-8")):
            sid = e.get("stand_id")
            raw = (e.get("infobox") or {}).get("origin")
            if not sid or not raw or sid in _seen:
                continue
            _seen.add(sid)
            kind, note = parse_origin(raw)
            out["origins"].append({
                "stand_id": sid, "origin_raw": raw[:200],
                "origin_kind": kind, "origin_note": note,
            })
    return out


def summarize(data: dict) -> None:
    from collections import Counter
    apps = data["appearances"]
    print(f"\n  出场记录 {len(apps)} 条")
    for k, n in Counter(a["kind"] for a in apps).most_common():
        print(f"    {k:8s} {n}")
    no_ch = sum(1 for a in apps if a["chapter_no"] is None)
    print(f"    章节号解析不出: {no_ch}（按「无数据」处理，不当 0）")
    print(f"\n  必杀技 {len(data['moves'])} 条，"
          f"覆盖 {len({m['stand_id'] for m in data['moves']})} 个替身")
    print(f"\n  替身来源 {len(data['origins'])} 条")
    for k, n in Counter(o["origin_kind"]
                        for o in data["origins"]).most_common():
        print(f"    {k:10s} {n}")


def main() -> int:
    print("=" * 66)
    print("收集出场记录 / 必杀技 / 替身来源")
    print("=" * 66)
    data = collect()
    summarize(data)

    print("\n" + "=" * 66)
    print("样例")
    print("=" * 66)
    print("  出场（前3条 manga）:")
    for a in [x for x in data["appearances"]
              if x["kind"] == "manga"][:3]:
        print(f"    ch{a['chapter_no']:<5} {str(a['chapter_title'])[:44]}")
    print("  覆盖（同替身，cover 混在 manga 里会答错首次出场）:")
    sp = [x for x in data["appearances"] if x["stand_id"] == "star_platinum"]
    for a in sorted(sp, key=lambda x: (x["chapter_no"] or 10**6))[:4]:
        print(f"    {a['kind']:6s} ch={str(a['chapter_no']):6s} "
              f"{a['raw_text'][:40]}")
    print("  origin 样例:")
    for o in data["origins"][:5]:
        print(f"    {o['origin_kind']:10s} {o['origin_raw'][:44]}")
        if o["origin_note"]:
            print(f"               └ {o['origin_note']}")
    print("\n★ 数据已在内存，落库请跑：load_db.py --all")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
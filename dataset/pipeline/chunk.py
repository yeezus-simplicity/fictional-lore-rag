"""
切块层：把渲染抓取的正文切成检索单元。

对应数据规范 §4：
  - 不用固定长度切分，能力描述天然有结构
  - 招式级切块：每个招式独立成块，保证检索粒度对齐用户意图
  - **结构化数据不进向量索引**（stat_row走 SQL）

输入：dataset/sources/rendered/<stand_id>.json
输出：dataset/processed/text_chunks.json

四种检索单元：
  ability_overview  能力概述
  move招式
  battle_record     战斗表现
  lore              命名出处与设定
  section           通用小节（兜底）
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

RENDER_DIR = Path(__file__).resolve().parents[1] / "sources" / "rendered"
STANDS_JSON = Path(__file__).resolve().parents[1] / "processed" / "stands.json"
OUT_PATH = Path(__file__).resolve().parents[1] / "processed" / "text_chunks.json"

# 噪声小节：目录、导航、引用、图库——对检索无价值
NOISE_SECTIONS = re.compile(
    r"^(CONTENTS|SITE NAVIGATION|REFERENCES?|GALLERY|NAME VARIANTS|"
    r"EXTERNAL LINKS|SEE ALSO|NOTES?|FOOTNOTES?|BIBLIOGRAPHY)$",
    re.I,
)
# 噪声列表项
NOISE_ITEMS = re.compile(
    r"^\d+(\.\d+)*\s|Manga▾|Parts▸|^Retrieved from|^\[|\bcookie\b|"
    r"Categories:|^Jump to|^edit$|^Privacy policy",
    re.I,
)
# 战斗记录的章节条目（保留，这是有价值的检索单元）
CHAPTER_ITEM = re.compile(
    r"^(?:[A-Za-z ]+)?\s*Chapter\s\d+\s*:|^Chapter\s\d+\s*:|Episode\s\d+", re.I
)
# 招式条目：形如 "Name (Alias) : description"
MOVE_ITEM = re.compile(r"^([A-Z][A-Za-z0-9 '\-]{1,40}?)\s*(?:\(([^)]{2,40})\))?\s*[:：]")
# ★ 招式误判防线（实测踩坑）
#   "Diamond is Unbreakable Episode 13: ..." 会被误当招式名，
#   因为剧集名以大写开头且含冒号
MOVE_FALSE_POSITIVE = re.compile(
    r"Episode\s|Chapter\s|Part\s\d|Part\s[A-Z]|Volume\s|Gallery|Cover",
    re.I,
)
# 块长度下限：低于此长度的块检索价值低（章节标题行、碎片）
MIN_CHUNK_LEN = 80


@dataclass
class Chunk:
    chunk_id: Optional[int] = None
    stand_id: str = ""
    stand_name: str = ""
    part: Optional[int] = None
    chunk_type: str = ""          # ability_overview / move / battle_record / lore / section
    content: str = ""
    content_len: int = 0
    section: str = ""             # 所属小节标题
    entity: str = ""              # 招式名等实体
    phonetic: Optional[str] = None# 招式读音（片假名/罗马音）
    alias: Optional[str] = None# 别名 / 释义
    debut: Optional[str] = None   # 招式首发章节
    source_url: str = ""
    data_source: str = "jojowiki_rendered"
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# ------------------------------------------------------------------
# 清洗
# ------------------------------------------------------------------

def clean_text(s: str) -> str:
    """去除引用标记 [12] / [a] 与多余空白。"""
    t = s or ""
    t = re.sub(r"\[\d+\]", "", t)
    t = re.sub(r"\[[a-z]\]", "", t, flags=re.I)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def is_noise_section(heading: str) -> bool:
    return bool(NOISE_SECTIONS.match(heading.strip()))


def is_noise_item(text: str) -> bool:
    if not text.strip():
        return True
    if CHAPTER_ITEM.match(text):
        return False          # 章节条目是有价值的
    return bool(NOISE_ITEMS.match(text))


def dedup_key(chunk_type: str, content: str) -> str:
    """去重键。

    ★ 实测踩坑：battle_record 被抓两遍
      —— 页面的 sections 列表里有章节项，page['battles'] 也有。
      两者内容相同但 source 不同（CHAPTERS/EPISODES vs CHAPTERS），
      按 (类型+内容) 去重即可消除。
    """
    return f"{chunk_type}::{content.strip()}"


# ------------------------------------------------------------------
# 切块
# ------------------------------------------------------------------

def chunk_page(page: dict, stand_meta: dict) -> list[Chunk]:
    """把一个替身的渲染结果切成检索单元。"""
    sid = page["stand_id"]
    name = page.get("name_raw", sid)
    part = stand_meta.get("part")
    url = page.get("url", "")

    chunks: list[Chunk] = []
    seen: set[str] = set()      # 去重键集合

    def add(ctype: str, content: str, section: str,
            entity: str = "", phonetic: Optional[str] = None,
            alias: Optional[str] = None, debut: Optional[str] = None) -> None:
        c = clean_text(content)
        # ★ 提高长度下限：实测 66% 的块短于 80 字符，多为章节标题行与碎片，
        #   检索价值低且稀释召回
        if len(c) < MIN_CHUNK_LEN:
            return
        key = dedup_key(ctype, c)
        if key in seen:
            return
        seen.add(key)
        chunks.append(Chunk(
            stand_id=sid, stand_name=name, part=part,
            chunk_type=ctype, content=c, content_len=len(c),
            section=section, entity=entity,
            phonetic=phonetic, alias=alias, debut=debut,
            source_url=url,
        ))

    # --- 1. 概述 ---
    for p in page.get("overview", []):
        add("ability_overview", p, "(overview)")

    # --- 2. 小节（排除噪声）---
    for sec in page.get("sections", []):
        heading = (sec.get("heading") or "").strip()
        if not heading or is_noise_section(heading):
            continue
        for p in sec.get("paras", []):
            # 小节里含引用关键词的归为 lore
            if re.search(r"(named after|tarot|card|deity|mytholog|namesake|"
                         r"based on|reference to)", p, re.I):
                add("lore", p, heading)
            else:
                add("section", p, heading)
        for lst in sec.get("lists", []):
            for item in lst:
                if is_noise_item(item):
                    continue
                if CHAPTER_ITEM.match(item):
                    add("battle_record", item, heading)

    # --- 3. 招式（★ 从渲染层的 techBox 抽取，字段最完整）---
    # 优先级最高：渲染层已解析出name / phonetic / alias / debut
    for m in page.get("moves", []):
        name = (m.get("name") or "").strip()
        if not name:
            continue
        text = m.get("text") or ""
        if len(clean_text(text)) < MIN_CHUNK_LEN:
            text = text or name
        add("move", text, m.get("section") or "TECHNIQUES",
            entity=name,
            phonetic=m.get("phonetic"),
            alias=m.get("alias"),
            debut=m.get("debut"))

    # --- 4. 战斗记录（页面级抽取，与sections 列表按类型+内容去重）---
    for b in page.get("battles", []):
        add("battle_record", b.get("text", ""), "CHAPTERS")

    # --- 5. 命名出处（页面级）---
    for l in page.get("lore", []):
        add("lore", l.get("text", ""), "(lore)")

    return chunks


# ------------------------------------------------------------------
# 主流程
# ------------------------------------------------------------------

def build_all(limit: Optional[int] = None) -> list[Chunk]:
    if not RENDER_DIR.exists():
        raise FileNotFoundError(f"渲染结果目录不存在：{RENDER_DIR}")

    stand_meta: dict[str, dict] = {}
    if STANDS_JSON.exists():
        for s in json.loads(STANDS_JSON.read_text(encoding="utf-8")):
            stand_meta[s["stand_id"]] = s

    files = sorted(RENDER_DIR.glob("*.json"))
    if limit:
        files = files[:limit]

    chunks: list[Chunk] = []
    for f in files:
        try:
            page = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        meta = stand_meta.get(page.get("stand_id", ""), {})
        chunks.extend(chunk_page(page, meta))

    # 分配 chunk_id
    for i, c in enumerate(chunks, 1):
        c.chunk_id = i
    return chunks


def report(chunks: list[Chunk]) -> dict:
    from collections import Counter
    type_cnt = Counter(c.chunk_type for c in chunks)
    part_cnt = Counter(c.part for c in chunks)
    lens = [c.content_len for c in chunks]
    lens_sorted = sorted(lens)

    def pct(p: float) -> int:
        if not lens_sorted:
            return 0
        idx = min(int(len(lens_sorted) * p), len(lens_sorted) - 1)
        return lens_sorted[idx]

    return {
        "n_chunks": len(chunks),
        "n_stands_covered": len({c.stand_id for c in chunks}),
        "total_chars": sum(lens),
        "type_distribution": dict(type_cnt.most_common()),
        "part_distribution": {str(k): v for k, v in sorted(
            part_cnt.items(), key=lambda x: (x[0] is None, x[0]))},
        "len_stats": {
            "min": lens_sorted[0] if lens_sorted else 0,
            "p50": pct(0.50),
            "p85": pct(0.85),
            "p95": pct(0.95),
            "max": lens_sorted[-1] if lens_sorted else 0,
        },
    }


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    print("=" * 68)
    print("切块层")
    print("=" * 68)
    chunks = build_all(args.limit)
    rep = report(chunks)

    print(f"  文本块总数{rep['n_chunks']}")
    print(f"  覆盖替身     {rep['n_stands_covered']}")
    print(f"  总字符       {rep['total_chars']:,}")
    print(f"\n  类型分布（对应数据规范 §4.2）：")
    for t, n in rep["type_distribution"].items():
        print(f"    {t:20s} {n:5d}")
    print(f"\n  所属部分布：{rep['part_distribution']}")
    ls = rep["len_stats"]
    print(f"\n  块长度分布：min={ls['min']} p50={ls['p50']} "
          f"p85={ls['p85']} p95={ls['p95']} max={ls['max']}")

    if not args.dry_run:
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(
            json.dumps([c.to_dict() for c in chunks], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        rp = OUT_PATH.parent / "chunk_report.json"
        rp.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n  输出：{OUT_PATH.name}  ({OUT_PATH.stat().st_size/1024:.1f} KB)")
        print(f"        {rp.name}")
    return 0


if __name__ == "__main__":
    sys_exit = __import__("sys").exit
    sys_exit(main())

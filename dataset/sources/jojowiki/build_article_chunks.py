"""把 jojowiki 文章（替身页 + 角色页 + **地点页**）清洗、切块，并入语料。

== 为什么走"生成新文件"而不是直接改 text_chunks.json ==
1. **可逆**：实验失败能一键回退，不动现有 2407 块。
2. **可对比**：baseline（text_chunks.json）与 augmented（text_chunks_aug.json）
   能跑同一条评测，直接看出增量带来的变化。
3. **绕开 schema 约束**：`text_chunks.stand_id` 是 NOT NULL + 外键到 stands；
   而角色页的实体是 character_id、地点页是 location_id，硬塞进 stand_id 会违反约束。
   ★ 检索层（lexical/semantic）是从 **JSON 文件**读语料的，
     所以只要产出文件就能参与检索，无需动数据库。

== ★ M65：地点页也切进来 ==
   只切 `locations.json` 里 `included == true` 的 **113 个正传地点**
   （外传/小说/游戏等 46 个已按用户口径排除，不参与检索）。
   地点块的部号取自 locations.json（判据见 build_locations.py）。

用法：
    python dataset/sources/jojowiki/build_article_chunks.py
输出：
    dataset/processed/article_chunks.json      # 仅新增的文章块（含地点块）
    dataset/processed/text_chunks_aug.json     # 2407 原始 + 新增
"""
from __future__ import annotations

import json
import sys
import collections
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from wash import wash, split_sections          # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
PROC = ROOT / "dataset" / "processed"
ARTICLES = HERE / "articles.json"
BASE_CHUNKS = PROC / "text_chunks.json"
LOCATIONS = PROC / "locations.json"

# ★ 新增块用独立的 chunk_type，便于检索/评测区分来源
CHUNK_TYPE = "article_section"
DATA_SOURCE = "jojowiki_article"


def main() -> int:
    if not ARTICLES.exists():
        print(f"✗ 缺少 {ARTICLES}（先跑 fetch_articles.py）")
        return 1

    art = json.loads(ARTICLES.read_text(encoding="utf-8"))
    pages = art.get("pages", [])
    stands = {s["stand_id"]: s for s in json.loads(
        (PROC / "stands.json").read_text(encoding="utf-8"))}
    # ★ 地点：只收 included=True 的正传（part 也从这里取）
    locs: dict[str, dict] = {}
    if LOCATIONS.exists():
        ld = json.loads(LOCATIONS.read_text(encoding="utf-8"))
        locs = {e["location_id"]: e for e in ld.get("locations", [])
                if e.get("included")}
    else:
        print(f"  ⚠ 缺少 {LOCATIONS.name}（先跑 build_locations.py）→ 本次不切地点")
    base = json.loads(BASE_CHUNKS.read_text(encoding="utf-8"))
    next_id = max(c["chunk_id"] for c in base) + 1

    out: list[dict] = []
    stat = collections.Counter()
    for p in pages:
        kind = p.get("kind")
        eid = p.get("entity_id")
        # ★ 地点只切正传（被排除的不参与检索）
        if kind == "location" and eid not in locs:
            stat["skipped_noncanon"] += 1
            continue
        tx = wash(p["wikitext"])
        blocks = split_sections(tx)
        # 替身页 part 从 stands.json 取；地点页从 locations.json 取
        part = None
        if kind == "stand" and eid in stands:
            part = stands[eid].get("part")
        elif kind == "location":
            part = locs[eid].get("part")
        for b in blocks:
            content = b["content"].strip()
            if len(content) < 80:          # 太短的块是噪声
                stat["skipped_short"] += 1
                continue
            # ★ 归属字段必须**规范**：替身页给 stand_id、角色页给 character_id、
            #   地点页给 location_id。早先版本把角色的 id 塞进 stand_id 靠
            #   extra.kind 区分 —— 下游（load_db）得猜，且会写进错误的外键。
            out.append({
                "chunk_id": next_id,
                "stand_id": eid if kind == "stand" else None,
                "character_id": eid if kind == "character" else None,
                "location_id": eid if kind == "location" else None,
                # ★ M72：篇章页给 arc_id —— 第三类归属。
                #   不填就会撞 chk_chunk_owner（stand/character/location 全空），
                #   而篇章块**已经**在检索库里（merging.py 运行时合并
                #   article_chunks.json）→ DB 不入会造成
                #   「检索能命中、SQL 查不到、证据回查失败」的分裂状态。
                "arc_id": eid if kind == "arc" else None,
                "stand_name": p["title"],
                "part": part,
                "chunk_type": CHUNK_TYPE,
                "content": content,
                "content_len": len(content),
                "section": b["section"] or "(lead)",
                "entity": "",
                "phonetic": None,
                "alias": None,
                "debut": None,
                "source_url": p["url"],
                "data_source": DATA_SOURCE,
                "extra": {"kind": kind},
            })
            next_id += 1
            stat[kind] += 1

    (PROC / "article_chunks.json").write_text(
        json.dumps(out, ensure_ascii=False), encoding="utf-8")
    merged = base + out
    (PROC / "text_chunks_aug.json").write_text(
        json.dumps(merged, ensure_ascii=False), encoding="utf-8")

    n_new_chars = sum(c["content_len"] for c in out)
    n_base_chars = sum(c["content_len"] for c in base)
    print(f"页面 {len(pages)} 个 → 新增块 {len(out)}"
          f"（替身 {stat['stand']} / 角色 {stat['character']} / "
          f"地点 {stat['location']}，跳过过短 {stat['skipped_short']}"
          f" / 非正传地点 {stat['skipped_noncanon']}）")
    print(f"新增文本 {n_new_chars:,} 字符")
    print(f"语料：{n_base_chars:,} → {n_base_chars + n_new_chars:,} 字符"
          f"（×{(n_base_chars + n_new_chars) / n_base_chars:.2f}）")
    print(f"块数：{len(base)} → {len(merged)}")
    print(f"→ {(PROC / 'text_chunks_aug.json').relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""从语料里反查「地点 ↔ 替身/角色」的关联，**每条都带证据**（M65）。

输入：`dataset/processed/article_chunks.json`（含替身/角色/地点块）
     `dataset/processed/locations.json`（113 个正传地点）
输出：`dataset/processed/location_mentions.json`

== 用户的要求 ==
  「不准瞎掰 必须要有语料证据」
→ 所以这里**不做任何推测**：每一条关联都必须能指出
   **哪一个语料块（chunk_id）**、**命中了哪个词（matched_term）**、
   **原文长什么样（snippet）**。前端可以点开看证据。

== ★★ 为什么分两个方向（证据强度不同）★★
  A) `location_page`（**强**）：**地点页正文里写了这个替身/角色**。
     如 `Angelo Rock` 页写明它的命名来源是 `Anjuro Katagiri`
     → 「安杰罗岩与这个角色相关」是语料直接说的。
  B) `entity_page`（**弱**）：替身/角色页里提到了该地名。
     问题：泛地名会爆炸 —— `Japan` / `England` / `Egypt` 这类
     在「born in Japan」这种句子里出现，会让几乎所有替身都关联上 Japan。
→ 两个方向都落盘（都算"有证据"），但**强度分开标注**，
  由下游决定怎么呈现；**不在这里替用户做取舍**。

== 匹配规则（★ 词边界很关键）==
  · 拉丁字母词：必须**词边界**匹配 —— 否则 `Rock` 会命中 `Rocky`，
    `England` 会命中 `New England`。
  · 中日文字符：无词边界概念 → 直接子串匹配（如「安杰罗岩」「杜王町」）。
  · 最短长度阈值：拉丁 ≥3 字符 / CJK ≥2 字（再短就是噪声）。

用法：
    python dataset/sources/jojowiki/build_location_mentions.py
"""
from __future__ import annotations

import collections
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PROC = ROOT / "dataset" / "processed"
CHUNKS = PROC / "article_chunks.json"
LOCATIONS = PROC / "locations.json"
OUT = PROC / "location_mentions.json"

SNIPPET_W = 70          # 证据片段前后各取多少字符
MIN_LATIN = 3           # 拉丁词最短长度
MIN_CJK = 2             # 中日文名最短长度

_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]")

# ★★ 词形与英文常用词重合的实体名（**逐条确认，不是规则生成**）★★
#   为什么需要：JoJo 里有不少替身/角色名本身就是常用英文词，
#   在语料里当**普通词**用的次数远多于当专有名词。
#   实测反例：角色 `Forever`（第 3 部那只猩猩）被
#     `…he will live in the town forever as a rock…` 误命中，
#   于是「安杰罗岩 ↔ Forever」这种**证据不支持结论**的关联被写进去。
#   → 对这类名字**要求首字母大写**匹配（"Sun" 命中、"sun" 不命中）。
#   ★ 清单是人工定的，不完整 —— 与 M41「单字名不收」同一套思路：
#     宁可少收（真关联漏掉几条），也不产出看起来有证据、实际是错的关联。
AMBIGUOUS = {
    "sun", "surface", "harvest", "strength", "clash", "survivor",
    "forever", "justice", "kiss", "dolomite", "toru", "boy", "girl",
    "jail", "angel", "love", "star", "pearl", "green", "white", "red",
    "oasis", "highway", "rose", "sound", "echo", "number",
}


def has_cjk(s: str) -> bool:
    return bool(_CJK.search(s))


def build_pattern(term: str, strict_case: bool = False) -> re.Pattern | None:
    """把名字编译成匹配正则。

    ★ 拉丁词加词边界：`(?<![A-Za-z0-9])term(?![A-Za-z0-9])`
      实测必要性：`Rock` 不加边界会命中 `Rocky`，`England` 会命中 `New England`。
    ★ CJK 不加边界（中文/日文没有词的概念），但要求足够长。
    ★ strict_case：见 `AMBIGUOUS` —— 常用词型名字要求首字母大写。
    """
    t = (term or "").strip()
    if not t:
        return None
    if has_cjk(t):
        if len(t) < MIN_CJK:
            return None
        return re.compile(re.escape(t))
    if len(t) < MIN_LATIN:
        return None
    body = r"(?<![A-Za-z0-9])" + re.escape(t) + r"(?![A-Za-z0-9])"
    return re.compile(body) if strict_case else re.compile(body, re.IGNORECASE)


def snippet_of(text: str, m: re.Match) -> str:
    a = max(0, m.start() - SNIPPET_W)
    b = min(len(text), m.end() + SNIPPET_W)
    return ("…" if a > 0 else "") + text[a:b] + ("…" if b < len(text) else "")


def collect_entities(chunks: list[dict]) -> dict[tuple, dict]:
    """从语料块聚合出实体名表：{(kind, id): {terms: set[(match_kind, term)]}}。

    ★ 名字直接取自 chunks 的 `stand_name`（替身/角色页块里存的就是页面标题），
      不需要另找数据源 —— 少一个可能的"对不上"的地方。
    """
    ents: dict[tuple, dict] = {}
    for c in chunks:
        kind = (c.get("extra") or {}).get("kind")
        if kind == "stand" and c.get("stand_id"):
            key = ("stand", c["stand_id"])
        elif kind == "character" and c.get("character_id"):
            key = ("character", c["character_id"])
        else:
            continue
        e = ents.setdefault(key, {"terms": set()})
        if c.get("stand_name"):
            e["terms"].add(("name_en", c["stand_name"]))
    return ents


def main() -> int:
    for f in (CHUNKS, LOCATIONS):
        if not f.exists():
            print(f"✗ 缺少 {f}")
            return 1

    chunks = json.loads(CHUNKS.read_text(encoding="utf-8"))
    locdata = json.loads(LOCATIONS.read_text(encoding="utf-8"))
    locs = [e for e in locdata["locations"] if e.get("included")]
    print(f"语料块 {len(chunks)} · 正传地点 {len(locs)}")

    # ---- 实体名表（替身 + 角色），并补上替身别名 ----
    ents = collect_entities(chunks)
    try:
        sys.path.insert(0, str(ROOT / "services"))
        import aliases                                        # noqa: E402
        _, id2alias = aliases.build_alias_table()
        for (kind, eid), e in ents.items():
            if kind == "stand":
                for a in (id2alias.get(eid) or []):
                    e["terms"].add(("alias", a))
    except Exception as ex:                                   # noqa: BLE001
        print(f"  ⚠ 别名表加载失败（{type(ex).__name__}）：只用页面名匹配")
    n_stand = sum(1 for k in ents if k[0] == "stand")
    n_char = sum(1 for k in ents if k[0] == "character")
    print(f"实体：替身 {n_stand} · 角色 {n_char}")

    # ---- 预编译（性能：术语总数 × 块数 = 数千万次匹配，先筛掉编不出来的）----
    ent_pats = []
    for (kind, eid), e in ents.items():
        pats = []
        for mk, term in e["terms"]:
            # ★ 常用词型名字 → 要求首字母大写（见 AMBIGUOUS）
            strict = (not has_cjk(term)) and (term.strip().lower() in AMBIGUOUS)
            p = build_pattern(term, strict_case=strict)
            if p:
                pats.append((mk + "_upper" if strict else mk, term, p))
        if pats:
            ent_pats.append((kind, eid, pats))
    loc_pats = []
    for L in locs:
        pats = []
        for mk, term in (("name_en", L["title"]), ("name_ja", L.get("name_ja"))):
            p = build_pattern(term or "")
            if p:
                pats.append((mk, term, p))
        if pats:
            loc_pats.append((L["location_id"], pats))
    print(f"可匹配：实体 {len(ent_pats)} · 地点 {len(loc_pats)}")

    mentions: list[dict] = []
    seen: set[tuple] = set()

    def add(loc_id, chunk_id, kind, eid, direction, mk, term, snip):
        key = (loc_id, chunk_id, kind, eid, term)
        if key in seen:
            return
        seen.add(key)
        mentions.append({
            "location_id": loc_id, "chunk_id": chunk_id,
            "entity_kind": kind, "entity_id": eid, "direction": direction,
            "match_kind": mk, "matched_term": term, "snippet": snip,
        })

    # ---- 逐块匹配（块是唯一的公共维度，两个方向共用这一遍循环）----
    for c in chunks:
        kind = (c.get("extra") or {}).get("kind")
        text = c.get("content") or ""
        cid = c.get("chunk_id")
        if kind == "location":
            # A) 强证据：地点页正文 → 提到了哪些替身/角色
            lid = c.get("location_id")
            if not lid:
                continue
            for ek, eid, pats in ent_pats:
                for mk, term, p in pats:
                    m = p.search(text)
                    if m:
                        add(lid, cid, ek, eid, "location_page", mk, term,
                            snippet_of(text, m))
                        break        # 同一实体同一块只记一次（最强的那次）
        elif kind in ("stand", "character"):
            # B) 弱证据：替身/角色页正文 → 提到了哪些地点
            ek = kind
            eid = c.get("stand_id") if kind == "stand" else c.get("character_id")
            if not eid:
                continue
            for lid, pats in loc_pats:
                for mk, term, p in pats:
                    m = p.search(text)
                    if m:
                        add(lid, cid, ek, eid, "entity_page", mk, term,
                            snippet_of(text, m))
                        break

    OUT.write_text(json.dumps(
        {"n_chunks": len(chunks), "n_locations": len(locs),
         "_note": ("每条关联都带证据：chunk_id + matched_term + snippet。"
                   "direction: location_page=地点页正文提到该实体（强）／"
                   "entity_page=实体页提到该地名（弱，泛地名会很多）。"),
         "mentions": mentions},
        ensure_ascii=False), encoding="utf-8")

    # ---- 统计（含「泛地名」预警）----
    by_dir = collections.Counter(m["direction"] for m in mentions)
    per_loc = collections.Counter(m["location_id"] for m in mentions)
    print(f"\n{'='*62}")
    print(f"关联 {len(mentions)} 条（强证据 {by_dir['location_page']} · "
          f"弱证据 {by_dir['entity_page']}）")
    print(f"覆盖地点 {len(per_loc)}/{len(locs)}")
    print("\n关联最多的 8 个地点（★ 泛地名会让 entity_page 爆炸，"
          "这就是分方向的原因）：")
    for lid, n in per_loc.most_common(8):
        nm = next((L["title"] for L in locs if L["location_id"] == lid), lid)
        strong = sum(1 for m in mentions
                     if m["location_id"] == lid and m["direction"] == "location_page")
        print(f"   {nm:28s} 共 {n:4d}（强 {strong:3d}）")
    strong_only = [lid for lid in per_loc
                   if any(m["location_id"] == lid and m["direction"] == "location_page"
                          for m in mentions)]
    print(f"\n有强证据的地点：{len(strong_only)}/{len(locs)}")
    print(f"→ {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

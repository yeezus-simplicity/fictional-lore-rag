"""地点维度（M65）：加载地点数据、识别地点问句、组织地点答案。

数据来源（全部是仓库内的可复核产物）：
  dataset/processed/locations.json          113 个正传地点（含层级路径与判部依据）
  dataset/processed/location_name_zh.json   地点中文名（59 个，带来源/依据/可信度）
  dataset/processed/location_mentions.json  地点↔替身/角色的关联（2588 条，**每条带语料证据**）
  dataset/processed/stands.json             拿替身英文名（关联结果要显示成人看得懂的名字）

★★ 为什么从 JSON 读、而不是查数据库 ★★
  与检索层一致：结构化产物都以 JSON 形式随仓库走，
  DB 不可用时（--no-data / 降级模式）本模块**仍然可用**，
  而结构化 SQL 那条路会直接返回「数据库不可用」。
  （migration 003 的表仍然存在，供 SQL 层与后续扩展使用。）

★★ 本项目的一条硬规则：缺数据就写「源站未提供」，**不填 0、不编** ★★
  地点 Infobox 的 residents 字段实测只有 2% 填充率
  → 界面上必须照实说，不能显示成「无居住者」。
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROC = ROOT / "dataset" / "processed"

# 供前端区分「有值 / 源站未提供」的字段清单
OPTIONAL_FIELDS = ("type", "place_paths", "mangadebut", "name_zh", "name_ja")


def _read(name: str):
    p = PROC / name
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


@lru_cache(maxsize=1)
def load() -> dict:
    """一次性装载全部地点数据（进程内缓存）。"""
    loc_raw = _read("locations.json") or {}
    zh_raw = _read("location_name_zh.json") or {}
    men_raw = _read("location_mentions.json") or {}
    stands_raw = _read("stands.json") or []

    stand_name = {s.get("stand_id"): s.get("name_en") for s in stands_raw}

    # ---- 角色显示名 ----
    # ★ 不能用 character_ja.json：它的键是**英文名**（'Vanilla Ice' → 'ヴァニラ・アイス'），
    #   不是 character_id，按 id 查永远落空（我第一版就是这么写的，
    #   结果地点卡上显示的是 `josuke_higashikata` 这种内部 id）。
    # ★ 正确的 id→名字 来源是**语料块**：角色页切出来的块里
    #   `character_id` 与 `stand_name`（页面标题）是同一条记录的两个字段。
    #   ★ article_chunks.json 在 .gitignore（可由脚本重建）→ 缺失时退回 id，
    #     宁可显示得难看，也不去猜名字。
    char_name: dict[str, str] = {}
    char_en: dict[str, str] = {}
    for c in (_read("article_chunks.json") or []):
        cid = c.get("character_id")
        if cid and (c.get("extra") or {}).get("kind") == "character":
            char_en.setdefault(cid, c.get("stand_name") or cid)
            char_name.setdefault(cid, char_en[cid])
    # 中文名（130 个角色，来自既有别名系统）—— 有就优先显示中文
    try:
        import sys as _sys
        if str(ROOT / "services") not in _sys.path:
            _sys.path.insert(0, str(ROOT / "services"))
        from aliases import load_char_zh_map as _lcz
        for cid, names in (_lcz() or {}).items():
            if names:
                char_name[cid] = names[0]
    except Exception:  # noqa: BLE001
        pass

    zh_by_id = {}
    for e in (zh_raw.get("entries") or []):
        if e.get("name_zh"):
            zh_by_id[e["location_id"]] = e

    by_id: dict[str, dict] = {}
    for e in (loc_raw.get("locations") or []):
        if not e.get("included"):
            continue
        lid = e["location_id"]
        rec = dict(e)
        z = zh_by_id.get(lid) or {}
        rec["name_zh"] = z.get("name_zh")
        rec["name_zh_source"] = z.get("source")
        rec["name_zh_evidence"] = z.get("evidence")
        rec["name_zh_confidence"] = z.get("confidence")
        by_id[lid] = rec

    # ---- 名称 → location_id ----
    # ★ 别名优先级：中文名 > 英文名 > 日文名（同名冲突时前面的赢，
    #   并且**记下被覆盖的名字**，避免静默丢失）
    by_name: dict[str, str] = {}
    collisions: list[tuple[str, str, str]] = []
    for lid, rec in by_id.items():
        for nm in (rec.get("name_zh"), rec.get("title"), rec.get("name_ja")):
            if not nm:
                continue
            key = nm.strip().lower()
            if key in by_name and by_name[key] != lid:
                collisions.append((nm, by_name[key], lid))
                continue
            by_name[key] = lid

    # ---- 地点归属路径反查：place_name → 被它包含的 location_id 集合 ----
    #   用于「杜王町有哪些地点」：把 place_chain 里的每一级都建索引。
    contained_by: dict[str, set[str]] = {}
    for lid, rec in by_id.items():
        for chain in (rec.get("place_paths") or []):
            for place in chain:
                contained_by.setdefault(place.strip().lower(), set()).add(lid)

    # ---- 关联（每条都带语料证据）----
    mentions: dict[str, list[dict]] = {}
    for m in (men_raw.get("mentions") or []):
        kind = m.get("entity_kind")
        eid = m.get("entity_id")
        if kind == "stand":
            nm, nm_en = stand_name.get(eid), stand_name.get(eid)
        else:
            nm, nm_en = char_name.get(eid), char_en.get(eid)
        mentions.setdefault(m["location_id"], []).append({
            **m,
            "entity_name": nm or eid,
            "entity_name_en": nm_en or eid,
        })

    # ---- ★ M70：同名篇章（location ↔ story arc）----
    #   用户实测「勒索之路的替身是什么」答得不对：源站里"勒索之路"
    #   **既是地点（杜王町的一条无车街）也是篇章（JoJolion 第 4 个篇章）**，
    #   两个独立页面靠 hatnote 互相指路。我们只建了地点 →
    #   把篇章的诉求（这一段里有哪些替身）当成了地点的关联来答。
    #   → 建 name → arc 索引，答问句时把两种口径都给出并说明区别。
    arcs_raw = _read("story_arcs.json") or {}
    arcs_by_id: dict[str, dict] = {}
    arcs_by_name: dict[str, list[dict]] = {}
    for a in (arcs_raw.get("arcs") or []):
        aid = a.get("arc_id")
        if not aid:
            continue
        arcs_by_id[aid] = a
        for nm in (a.get("name_en"), a.get("arc_id"),
                   a.get("location_title")):
            if nm:
                arcs_by_name.setdefault(str(nm).strip().lower(), []).append(a)

    # ---- ★ M71：篇章 ↔ 替身语料证据链 ----
    am_raw = _read("arc_mentions.json") or {}
    arc_mentions: dict[str, dict] = {}
    for a in (am_raw.get("arcs") or []):
        if a.get("arc_id"):
            arc_mentions[a["arc_id"]] = a

    return {
        "by_id": by_id,
        "by_name": by_name,
        "collisions": collisions,
        "contained_by": contained_by,
        "place_index": {},          # place_name → location_id（该地名本身也是个地点时）
        "mentions": mentions,
        "arcs_by_id": arcs_by_id,
        "arcs_by_name": arcs_by_name,
        "arc_mentions": arc_mentions,
        "n_arcs_with_stands": am_raw.get("n_with_stands") or 0,
        "n_included": loc_raw.get("n_included") or len(by_id),
        "n_with_zh": zh_raw.get("n_with_zh") or 0,
        "n_mentions": len(men_raw.get("mentions") or []),
        "n_arcs": arcs_raw.get("n_arcs") or 0,
        "n_arcs_linked": arcs_raw.get("n_linked_to_location") or 0,
    }


def same_name_arcs(location_id: str) -> list[dict]:
    """与该地点**同名**的篇章（M70 同名消歧用）。

    ★ 只认「源站 hatnote 明确互指」的那一对，不按名字模糊匹配 ——
      否则「Anubis」这种地点/替身同名会造出一堆假关联
      （与 M65「泛地名误匹配」同一类教训）。
    """
    st = load()
    for a in st["arcs_by_id"].values():
        if a.get("location_id") == location_id:
            return [a]
    return []


def find_arcs(name: str) -> list[dict]:
    """按名字查篇章（用户直接打篇章名时用）。"""
    st = load()
    return st["arcs_by_name"].get((name or "").strip().lower(), [])


def n_arcs() -> int:
    return load()["n_arcs"]


# ---- M71：篇章口径的问答（「这一段里有哪些替身」）----
#   M70 只把篇章做成地点答案里的**附属提示**，用户问篇章本身只能得到
#   「没找到」。这一层补上：按名字找篇章 → 给该章范围的替身（带话数证据）。
_ARC_ASK = re.compile(r"(篇章|故事|arc|段)", re.I)


def find_arc_mention(name: str) -> dict | None:
    """按名字查篇章证据链。"""
    st = load()
    key = (name or "").strip().lower().strip('"')
    # 先精确匹配 arc_id / 显示名，再退化为包含匹配
    for aid, a in st["arc_mentions"].items():
        if key in (aid, (a.get("name") or "").strip().lower().strip('"')):
            return a
    for aid, a in st["arc_mentions"].items():
        if key and (key in aid or key in (a.get("name") or "").lower()):
            return a
    return None


def answer_arc_from_query(q: str) -> dict | None:
    """问句里直接含篇章名 → 找篇章（M71）。

    ★ 为什么需要：多数篇章**没有同名地点**（Anubis / Another One Bites
    the Dust / 1st Stage …），而 `resolve()` 只建了地点名索引 →
    这些篇章名在问句里根本抽不到，只能走这一路。

    ★ 按**名字长度降序**匹配，避免「Anubis」被「Anubis (Story Arc)」
    之类的短名抢先；同时要求匹配到词边界，防止把
    「DIO」匹配进「DIO's World」这类子串误配。
    """
    st = load()
    ql = (q or "").lower()
    cands: list[tuple[str, dict]] = []
    for aid, a in st["arc_mentions"].items():
        for nm in (a.get("name"), aid):
            if not nm:
                continue
            n = str(nm).strip().lower().strip('"')
            if len(n) >= 3 and n in ql:
                cands.append((n, a))
    if not cands:
        return None
    cands.sort(key=lambda x: -len(x[0]))
    a = cands[0][1]
    return _arc_answer_dict(a)


def answer_arc(name: str) -> dict | None:
    """组织篇章答案：该章范围出场的替身（每条带话数 + 原文片段）。

    ★ 与 M65 的 location_related 是同构的：都只答**有语料证据**的，
      没有证据的一行都不列。
    """
    a = find_arc_mention(name)
    if not a:
        return None
    return _arc_answer_dict(a)


def _arc_answer_dict(a: dict) -> dict:
    """篇章证据链 → 答案 dict（M71）。"""
    stands = [{
        "name": s.get("name_en"),
        "stand_id": s.get("stand_id"),
        "chapter": s.get("chapter"),
        "snippet": s.get("snippet"),
        "n_forms": s.get("n_forms", 1),
    } for s in (a.get("stands") or [])]

    return {
        "type": "arc_fact",
        "arc": {
            "arc_id": a["arc_id"],
            "name": a.get("name"),
            "part": a.get("part"),
            "chapters": a.get("chapters"),
            "chapters_abs": a.get("chapters_abs"),
        },
        "count": len(stands),
        "stands": stands,
        "note": (
            "以上是该**篇章章节范围**内、源站替身条目里记录到出场的替身，"
            "每条标注了最早出场话数与原文，可回查。"
            + ("★ 另有同名**地点**，口径不同（问地点请去掉「篇章」二字）。"
               if a.get("location_id") else "")
            + (a.get("note") or "")
        ),
    }


def all_names() -> list[str]:
    """全部地点写法（中文/英文/日文），供路由与别名表使用。"""
    return list(load()["by_name"].keys())


def alias_pairs() -> list[tuple[str, str]]:
    """(写法, location_id) —— 供并入别名表（与替身别名同一套路）。"""
    return [(nm, lid) for nm, lid in load()["by_name"].items()]


def n_locations() -> int:
    return load()["n_included"]


# ------------------------------------------------------------------
# 匹配
# ------------------------------------------------------------------
def match(q: str) -> list[tuple[str, str]]:
    """从问句里找出提到的地点 → [(location_id, 命中的写法)]，长名优先。

    ★ 与替身别名一致：长名优先，避免「Morioh」抢走「Morioh (JoJolion)」。
    ★ 拉丁名要求词边界（避免 `Rome` 命中 `Romeo`）—— 这条在 M65 的
      关联脚本里已经验证过必要性，这里沿用。
    """
    data = load()
    ql = q.lower()
    hits: list[tuple[str, str]] = []
    for nm, lid in data["by_name"].items():
        if len(nm) < 2:
            continue
        if re.search(r"[a-z]", nm):
            if not re.search(r"(?<![a-z0-9])" + re.escape(nm) + r"(?![a-z0-9])", ql):
                continue
        elif nm not in ql:
            continue
        hits.append((lid, nm))
    # 长名优先 + 去重（同一个地点保留最长命中）
    hits.sort(key=lambda x: -len(x[1]))
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for lid, nm in hits:
        if lid in seen:
            continue
        seen.add(lid)
        out.append((lid, nm))
    return out


def resolve(q: str) -> list[dict]:
    """把问句里的地点解析成带展示名的记录（长名优先）。"""
    data = load()
    return [{**data["by_id"][lid], "matched_term": nm} for lid, nm in match(q)]


def contains(place_name: str) -> list[dict]:
    """找出**归属路径中包含该地名**的地点。

    例：`杜王町` → Angelo Rock / Shakedown Road / …
    ★ 这是「杜王町有哪些地点」这类问题的实现方式 ——
      它读的是源站 Infobox 的 place 层级，不是我推的。
    """
    data = load()
    lid = data["by_name"].get((place_name or "").strip().lower())
    key = (place_name or "").strip().lower()
    ids = data["contained_by"].get(key) or set()
    if lid:
        ids = set(ids) | {lid}
    return [data["by_id"][i] for i in sorted(ids) if i in data["by_id"]]


def by_part(part: int) -> list[dict]:
    data = load()
    return [r for r in data["by_id"].values() if r.get("part") == part]


def related(location_id: str, direction: str | None = None) -> list[dict]:
    """该地点的关联实体。

    Args:
        direction: 'location_page'（强：地点页正文提到了它）
                   / 'entity_page'（弱：实体页提到了这个地名）
                   / None（两者都返回，各自带 direction 标出来）
    ★ 刻意**不在这里替用户做取舍** —— 两个方向都返回，
      由调用方（答案组织/前端）决定展示哪一部分。
    """
    ms = load()["mentions"].get(location_id) or []
    if direction:
        ms = [m for m in ms if m.get("direction") == direction]
    # 强证据优先，其次按实体名
    ms = sorted(ms, key=lambda m: (m.get("direction") != "location_page",
                                   m.get("entity_name") or ""))
    return ms


# ------------------------------------------------------------------
# 答案组织
# ------------------------------------------------------------------
_LIST_ASK = re.compile(r"(哪些|有什么|有什么样|列表|全部|所有|一览|list)")
_PART_RE = re.compile(r"第\s*([0-9一二三四五六七八九])\s*部")
_PART_CN = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
            "六": 6, "七": 7, "八": 8, "九": 9}


def _part_of(q: str) -> int | None:
    m = _PART_RE.search(q)
    if not m:
        return None
    t = m.group(1)
    return int(t) if t.isdigit() else _PART_CN.get(t)


def _card(rec: dict, mentions: list[dict]) -> dict:
    """地点档案卡（前端渲染用）。缺的字段保留 None，由前端写「源站未提供」。"""
    strong = [m for m in mentions if m.get("direction") == "location_page"]
    weak = [m for m in mentions if m.get("direction") == "entity_page"]

    def _uniq(ms: list[dict], kind: str):
        seen, out = set(), []
        for m in ms:
            if m.get("entity_kind") != kind:
                continue
            eid = m.get("entity_id")
            if eid in seen:
                continue
            seen.add(eid)
            nm_en = m.get("entity_name_en")
            out.append({
                "id": eid,
                "name": m.get("entity_name") or eid,
                # 中文名与英文名不同时两者都给（前端显示英文作副标题）
                "name_en": nm_en if nm_en != (m.get("entity_name") or eid) else None,
                "matched_term": m.get("matched_term"),
                "chunk_id": m.get("chunk_id"),
                "snippet": m.get("snippet"),
            })
        return out

    return {
        "location_id": rec["location_id"],
        "name_en": rec.get("title"),
        "name_zh": rec.get("name_zh"),
        "name_zh_source": rec.get("name_zh_source"),
        "name_zh_evidence": rec.get("name_zh_evidence"),
        "name_zh_confidence": rec.get("name_zh_confidence"),
        "name_ja": rec.get("name_ja"),
        "name_romaji": rec.get("name_romaji"),
        "types": rec.get("types") or ([rec["type"]] if rec.get("type") else []),
        "part": rec.get("part"),
        "place_paths": rec.get("place_paths") or [],
        "mangadebuts": rec.get("mangadebuts") or [],
        "animedebuts": rec.get("animedebuts") or [],
        # ★ 强证据：地点页正文写了谁（这才是「有关联」的正面依据）
        "stands": _uniq(strong, "stand"),
        "characters": _uniq(strong, "character"),
        # ★ 弱证据只给数量与少量样例，并**明确标注是弱证据**
        "weak_count": len(weak),
        # 源站未提供的字段（前端照实显示，不填 0）
        "missing": [k for k in OPTIONAL_FIELDS if not rec.get(k)],
    }


def _evidence_from(mentions: list[dict], limit: int = 6) -> list[dict]:
    """把**强证据**（地点页写了该实体）转成可展示的检索证据。

    ★ 字段名必须与 API 的 Evidence 白名单模型对齐 —— 那是本项目踩过三次的坑
      （M13/M14 两次），未声明的字段会被 FastAPI **静默过滤掉**：
        · 片段用 `content`（模型里已有），不要自造 `snippet`
        · 地点 id 用 `location_id`（本轮刚加进模型与字符串字段白名单）
    """
    out = []
    for m in mentions:
        if m.get("direction") != "location_page":
            continue
        out.append({
            "type": "corpus_mention",
            "location_id": m["location_id"],
            "chunk_id": m.get("chunk_id"),
            "note": f"语料第 {m.get('chunk_id')} 块提到「{m.get('matched_term')}」"
                    f"（{m.get('match_kind')}）",
            "content": m.get("snippet"),
        })
        if len(out) >= limit:
            break
    return out


def _arc_hint(rec: dict) -> dict | None:
    """同名篇章提示（M70）。没有就返回 None —— 不硬造。"""
    arcs = same_name_arcs(rec["location_id"])
    if not arcs:
        return None
    a = arcs[0]
    return {
        "arc_id": a.get("arc_id"),
        "name": a.get("name_en"),
        "part": a.get("part"),
        "chapters": a.get("chapters"),
        "note": ("⚠ 这个名字在源站里**同时指一个地点和一个篇章**。"
                 "上面列的是「该**地点**条目里写到的」。"
                 "若你想问的是「这一**篇章**里有哪些替身/角色」，"
                 "请补一个字（如「篇章 勒索之路」），口径不同。"),
    }


def answer(q: str, top_k: int = 3):
    """组织地点问句的答案。

    Returns:
        (answer: dict, evidence: list[dict], card: dict | None)
        answer['type'] ∈ {location_fact, location_list, location_related,
                          location_hint}
    """
    from_name = resolve(q)
    part = _part_of(q)
    wants_list = bool(_LIST_ASK.search(q))
    wants_entity = bool(re.search(r"(替身|stand|角色|人物|使者)", q, re.I))

    # ---- ⓿ ★ M71：问句显式提到「篇章」→ 走篇章口径 ----
    #   必须排在最前：同名时（勒索之路）地点和篇章是两个东西，
    #   用户明确说了「篇章」就该答篇章，不能被地点抢走。
    #   ★ 只认「篇章」这个词，不靠猜 —— 没这个词就走原有地点口径。
    if _ARC_ASK.search(q):
        # ① 问句里的名字是**地点名**（同名篇章，如「篇章 勒索之路」）
        arc_title = from_name[0].get("title") if from_name else None
        arc_ans = answer_arc(arc_title) if arc_title else None
        # ② 名字不是地点名 —— 直接按篇章名在问句里找
        #    （Anubis / Another One Bites the Dust 这类篇章没有同名地点，
        #     resolve() 只认地点名 → 必须另找一路）
        if arc_ans is None:
            arc_ans = answer_arc_from_query(q)
        if arc_ans:
            return (arc_ans, [], None)

    # ---- ① 「第X部有哪些地点」----
    if part and wants_list and not from_name:
        items = by_part(part)
        return ({
            "type": "location_list",
            "scope": f"第 {part} 部",
            "count": len(items),
            "items": [{"location_id": r["location_id"],
                       "name": r.get("name_zh") or r.get("title"),
                       "name_en": r.get("title")} for r in items],
        }, [], None)

    # ---- ② 问某个地点「有哪些替身/角色」----
    #   ★ 必须排在 ③④ 的列表分支**之前**：`"勒索之路有哪些替身"` 里
    #     「哪些」会同时命中 _LIST_ASK，若先判列表就会答成一串地名。
    if from_name and wants_entity:
        rec = from_name[0]
        ms = related(rec["location_id"])
        card = _card(rec, ms)
        strong = [m for m in ms if m.get("direction") == "location_page"]
        return ({
            "type": "location_related",
            "location": {"location_id": rec["location_id"],
                         "name": card["name_zh"] or rec.get("title"),
                         "name_en": rec.get("title")},
            "stands": [{"name": s["name"], "name_en": s.get("name_en"),
                        "snippet": s["snippet"]} for s in card["stands"]],
            "characters": [{"name": c["name"], "name_en": c.get("name_en"),
                            "snippet": c["snippet"]} for c in card["characters"]],
            # ★ 弱证据只给数量 —— 泛地名会让「角色页提到该地」爆炸
            #   （Morioh 173 条），全列出来反而误导。
            "weak_count": card["weak_count"],
            "note": "以上「相关」= **该地点条目正文里写到过**（强证据）；"
                    f"另有 {card['weak_count']} 条是这些实体的条目里提到本地名"
                    "（弱证据，泛地名时会很多，故不展开）。",
            # ★ M70 同名消歧：源站里这个名字**同时是地点和篇章**
            #   （两个独立页面，hatnote 互相指路）→ 明确告知有另一种口径，
            #   免得用户以为「这一段的替身」就是「这条街的关联」。
            "also_story_arc": _arc_hint(rec),
        }, _evidence_from(ms), card)

    # ---- ③ 问某个地点「有哪些地点」（包含关系）----
    if from_name and wants_list:
        rec = from_name[0]
        subs = contains(rec.get("title") or "")
        subs += contains(rec.get("name_zh") or "")
        # 去掉自己
        subs = [s for s in {s["location_id"]: s for s in subs}.values()
                if s["location_id"] != rec["location_id"]]
        subs.sort(key=lambda r: (r.get("part") or 0, r.get("title") or ""))
        if subs:
            return ({
                "type": "location_list",
                "scope": f"{rec.get('name_zh') or rec.get('title')} 之内",
                "count": len(subs),
                "items": [{"location_id": r["location_id"],
                           "name": r.get("name_zh") or r.get("title"),
                           "name_en": r.get("title"),
                           "part": r.get("part")} for r in subs],
                "note": "按源站 Infobox 的归属层级（place 字段）筛出，"
                        "不是靠关键词命中。",
            }, [], None)

    # ---- ④ 泛问「有哪些地点」----
    if wants_list and not from_name:
        items = sorted(load()["by_id"].values(),
                       key=lambda r: (r.get("part") or 0, r.get("title") or ""))
        return ({
            "type": "location_list",
            "scope": "全部（正传 9 部）",
            "count": len(items),
            "items": [{"location_id": r["location_id"],
                       "name": r.get("name_zh") or r.get("title"),
                       "name_en": r.get("title"),
                       "part": r.get("part")} for r in items],
        }, [], None)

    # ---- ⑤ 单地点档案（默认）----
    if from_name:
        rec = from_name[0]
        ms = related(rec["location_id"])
        card = _card(rec, ms)
        return ({
            "type": "location_fact",
            "location_id": rec["location_id"],
            "name_en": rec.get("title"),
            "name_zh": rec.get("name_zh"),
            "name_ja": rec.get("name_ja"),
            "types": card["types"],
            "part": rec.get("part"),
            "place_paths": card["place_paths"],
            "mangadebuts": card["mangadebuts"],
            "animedebuts": card["animedebuts"],
            "related_stands": [s["name"] for s in card["stands"]],
            "related_characters": [c["name"] for c in card["characters"]],
        }, _evidence_from(ms), card)

    # ---- ⑥ 提到「地点」但没识别出具体地点 ----
    return ({
        "type": "location_hint",
        "note": f"没有识别出具体地点。本库收录 {n_locations()} 个正传地点，"
                "可问「安杰罗岩是什么地方」「杜王町有哪些地点」。",
    }, [], None)


# ---------------------------------------------------------------
if __name__ == "__main__":
    d = load()
    print(f"地点 {d['n_included']} 个 · 有中文名 {d['n_with_zh']} · "
          f"关联 {d['n_mentions']} 条 · 名称索引 {len(d['by_name'])} 个写法")
    if d["collisions"]:
        print("  ★ 同名冲突（已按优先级决定，供人工复核）:")
        for nm, a, b in d["collisions"]:
            print(f"     {nm} → 保留 {a}，忽略 {b}")
    print()
    for q in ["安杰罗岩是什么地方", "杜王町有哪些地点", "恶魔掌心在哪里",
              "勒索之路有哪些替身", "第 1 部有哪些地点"]:
        a, ev, card = answer(q)
        print(f"  {q:22s} → {a['type']:16s}", end="")
        if a["type"] == "location_fact":
            print(f"{a['name_zh'] or a['name_en']} 路径={a['place_paths']} "
                  f"相关替身={a['related_stands'][:3]}")
        elif a["type"] == "location_list":
            print(f"{a['scope']} 共 {a['count']}")
        elif a["type"] == "location_related":
            print(f"替身 {len(a['stands'])} / 角色 {len(a['characters'])}")
        else:
            print(a.get("note", "")[:40])

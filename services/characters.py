"""角色维度（M66）：加载角色档案、识别角色问句、组织角色答案。

数据来源：
  dataset/processed/characters_ext.json   168 个角色（含简介/背景/性格 + Infobox 字段）
  dataset/processed/article_chunks.json   语料块（给简介/背景/性格配 chunk_id，可回查原文）

★★ 两条本项目硬规则在这里怎么落地 ★★
  ① **不机翻**：简介/背景/性格保留**英文原文**。
     中文只给**结构化事实**（中文名/替身/部数/职业…）——
     与 M32 中文摘要卡、M65 地点卡同一口径：机器翻译会破坏「证据可追溯」。
  ② **每条都能指出语料块**：`bio_chunk_id` / `background_chunk_id` /
     `personality_chunk_id` 来自角色页切块，前端可展开看原文。

★ 与 M65 地点模块的取舍一致：**别名不参与匹配**。
  角色 Infobox 的 alias 里有 `JoJo`（168 个角色里好几个都叫 JoJo），
  用它做匹配会让「JoJo 是谁」命中一堆人。→ 别名只在卡片上展示。

★ 从 JSON 读、不查 DB：与检索层一致，DB 不可用时角色问题仍可用。
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROC = ROOT / "dataset" / "processed"

_PART_RE = re.compile(r"第\s*([0-9一二三四五六七八九])\s*部")
_PART_CN = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
            "六": 6, "七": 7, "八": 8, "九": 9}
_LIST_ASK = re.compile(r"(哪些|有什么|列表|全部|所有|一览|list)")
_STAND_ASK = re.compile(r"(替身|stand|能力)", re.I)


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
    raw = _read("characters_ext.json") or {}
    chars = raw.get("characters") or []

    # 语料块：section → chunk_id（人物简介/背景/性格要能回查原文）
    chunk_of: dict[tuple[str, str], int] = {}
    for c in (_read("article_chunks.json") or []):
        if (c.get("extra") or {}).get("kind") != "character":
            continue
        cid = c.get("character_id")
        if cid:
            chunk_of.setdefault((cid, c.get("section") or "(lead)"),
                                c.get("chunk_id"))

    by_id = {c["character_id"]: c for c in chars}

    # ---- 名称 → character_id ----
    #   ★ 只用**名字**（英/日/中 + 中文异译），**不用 alias**（见模块说明）
    by_name: dict[str, str] = {}
    for cid, c in by_id.items():
        for nm in ([c.get("name_zh")] + list(c.get("name_zh_variants") or [])
                   + [c.get("name_en"), c.get("name_ja")]):
            if not nm:
                continue
            k = nm.strip().lower()
            if len(k) < 2:
                continue
            by_name.setdefault(k, cid)

    # ★★ M73：并上**别名表**（含唯一归属的简称）★★
    #   为什么必须并：上面只收 characters_ext.json 的
    #   `name_zh` / `name_zh_variants` —— 那是**全名**
    #   （「布罗诺·布加拉提」）→ 用户打简称「布加拉提」时
    #   `match()` 抽不到角色 → 「布加拉提是谁」返回
    #   character_hint「没有识别出具体角色」（实测）。
    #   而 `aliases.build_char_alias_table()` 里有简称
    #   （M73 的 split_short_aliases 把「布罗诺·布加拉提」拆出「布加拉提」）。
    #   ★ 同一个概念（「角色名有哪些写法」）维护两份必然漂移 ——
    #     这个项目已在 M69/M70/M71/M72 栽过四次。→ 并集，单一来源。
    #   ★ 只收 `by_id` 里真实存在的角色 id（防别名表里的陈旧键）。
    try:
        from aliases import build_char_alias_table as _alias_tbl
        for nm, cid in _alias_tbl().items():
            if cid in by_id:
                by_name.setdefault(nm.strip().lower(), cid)
    except Exception:  # noqa: BLE001
        pass

    return {
        "by_id": by_id,
        "by_name": by_name,
        "n": len(by_id),
        "n_with_zh": sum(1 for c in by_id.values() if c.get("name_zh")),
        "n_with_bio": sum(1 for c in by_id.values() if c.get("bio")),
        "n_with_background": sum(1 for c in by_id.values()
                                 if c.get("background")),
        "n_with_personality": sum(1 for c in by_id.values()
                                  if c.get("personality")),
    }


def all_names() -> list[str]:
    return list(load()["by_name"].keys())


def n_characters() -> int:
    return load()["n"]


# ============================================================
# ★★ M74：模糊匹配（用户反馈「搜承太郎/齐贝林搜不到，根本没有模糊搜索」）★★
# ============================================================

# 问句里的中文连续段
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]{2,}")
# 片段里出现这些字 → 一定是**问法词**，不是名字
_NON_NAME = re.compile(
    r"(的|是|谁|替身|使者|本体|有|什么|哪些|哪个|哪几|第|部|吗|呢|吧|"
    r"能力|介绍|背景|性格|出场|登场|章节|话|集|多少|一共|列出|相关|"
    r"角色|人物|所有|全部|最高|最强|最低|最弱|排名|是几|几级)")


@lru_cache(maxsize=1)
def _zh_substr_index() -> dict[str, tuple[str, ...]]:
    """中文名**子串** → (cid, ...)，用于「用户打简称/姓氏」（M74）。

    ★ 为什么需要：现有 `match()` 是「**名字 ⊂ 问句**」（用户打了全名）。
      而用户打「承太郎」「齐贝林」时方向是反的 —— **问句片段 ⊂ 名字**
      → 精确匹配一条都命中不了（实测三条全空）。

    ★ 只索引 ≥2 字的片段，并过滤问法词（`_NON_NAME`）：
      不过滤时「的替身」「是谁」这类片段会把一票角色拉进来。
    ★ 同时索引「去间隔号形」：用户不会打「布罗诺·布加拉提」中间那个点。
    """
    idx: dict[str, list[str]] = {}
    for nm, cid in load()["by_name"].items():
        if not _CJK_RUN.search(nm):
            continue
        for variant in {nm, re.sub(r"[·・]", "", nm)}:
            for i in range(len(variant)):
                for L in range(2, min(8, len(variant) - i) + 1):
                    seg = variant[i:i + L]
                    if _NON_NAME.search(seg):
                        continue
                    lst = idx.setdefault(seg, [])
                    if cid not in lst:
                        lst.append(cid)
    return {k: tuple(v) for k, v in idx.items()}


def fuzzy_hits(q: str, limit: int = 12) -> list[tuple[str, str]]:
    """从问句里找角色名片段（简称 / 姓氏 / 英文前缀）→ [(cid, 片段)]。

    ★ 与 `match()` 的分工（方向相反，别合并）：
      match()      = **名字 ⊂ 问句**（用户打了全名）
      fuzzy_hits() = **问句片段 ⊂ 名字**（用户打了简称/姓氏/英文前半）

    ★ 贪心取**最长**且不重叠的片段：
      「齐贝林是谁」先命中「齐贝林」(3 字)，就不会再用「贝林」(2 字) 去撞别人。
      不做这层的话，2 字片段会把大量无关角色拉进来。
    """
    idx = _zh_substr_index()
    ql = q.lower()
    spans: list[tuple[int, int, str]] = []
    for m in _CJK_RUN.finditer(ql):
        run, base = m.group(0), m.start()
        for i in range(len(run)):
            for L in range(min(8, len(run) - i), 1, -1):
                seg = run[i:i + L]
                if seg in idx:
                    spans.append((base + i, base + i + L, seg))
    spans.sort(key=lambda x: (-(x[1] - x[0]), x[0]))

    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    taken: list[tuple[int, int]] = []
    for s, e, seg in spans:
        if any(not (e <= ts or s >= te) for ts, te in taken):
            continue
        taken.append((s, e))
        for cid in idx[seg]:
            if cid not in seen:
                seen.add(cid)
                out.append((cid, seg))
        if len(out) >= limit:
            return out[:limit]

    # ---- 英文：问句里的英文词是某英文名的**前缀/整词**（大小写不敏感）----
    #   实测：「dio」查不到 Dio Brando（只做全名匹配）。
    for w in re.findall(r"[a-z][a-z0-9'’\-]{2,}", ql):
        for nm, cid in load()["by_name"].items():
            if cid in seen or not re.match(r"[a-z]", nm):
                continue
            if nm == w or nm.startswith(w + " ") or nm.replace(" ", "") == w:
                seen.add(cid)
                out.append((cid, w))
                if len(out) >= limit:
                    return out[:limit]
    return out[:limit]


def match(q: str) -> list[tuple[str, str]]:
    """从问句里找出角色 → [(character_id, 命中的写法)]，长名优先。

    ★★ M74：两轮匹配 ★★
      ① 精确：**名字 ⊂ 问句**（全名/别名）
      ② 模糊：**问句片段 ⊂ 名字**（简称/姓氏/英文前缀）
      精确结果排在前面 —— 打了全名就该命中全名那条。
    """
    data = load()
    ql = q.lower()
    hits: list[tuple[str, str]] = []
    for nm, cid in data["by_name"].items():
        if re.search(r"[a-z]", nm):
            if not re.search(r"(?<![a-z0-9])" + re.escape(nm) + r"(?![a-z0-9])",
                             ql):
                continue
        elif nm not in ql:
            continue
        hits.append((cid, nm))
    hits.sort(key=lambda x: -len(x[1]))
    seen: set[str] = set()
    out = []
    for cid, nm in hits:
        if cid in seen:
            continue
        seen.add(cid)
        out.append((cid, nm))

    # ★★ M75：**精确名也可能一对多**（第 4/8 部「吉良吉影」中文名完全相同）★★
    #   `by_name` 是 `dict[str, str]`，构建时用 `setdefault` → 只留第一个 cid
    #   → 第 8 部那位**永远搜不到**（实测：搜「吉良吉影」只出第 4 部）。
    #   `build_char_alias_index()` 是多值的（M74 为"搜索"建的），这里用它补齐。
    if out:
        try:
            from aliases import build_char_alias_index as _idx
            _multi = {k.strip().lower(): v for k, v in _idx().items()}
            for cid, nm in list(out):
                for cid2 in _multi.get(nm, ()):
                    if cid2 not in seen:
                        seen.add(cid2)
                        out.append((cid2, nm))
        except Exception:  # noqa: BLE001
            pass

    # ★★ M75：排除「被另一个命中名包含」的短名 ★★
    #   实测：搜「吉良吉影」会同时命中
    #     · 「吉良吉影」（yoshikage_kira / _jojolion）
    #     · 「吉良」——`holy_joestar_kira` 的简称
    #       （来自「吉良·荷莉·乔斯达」按间隔号拆出的姓氏）
    #   两者是子串关系，短的那个纯属噪声 → 「吉良吉影是谁」会多出
    #   一个「荷莉·乔斯达」的候选卡片。
    #   ★ 只排除**被同一轮命中的更长名字包含**的，不影响「东方」这类
    #     独立查询（搜「东方」时没有任何更长的命中名包含它）。
    _hit_names = [nm for _, nm in out]
    out = [(cid, nm) for cid, nm in out
           if not any(nm != o and nm in o for o in _hit_names)]
    seen = {cid for cid, _ in out}

    # ★ 第二轮：模糊（简称/姓氏）—— 已命中的不重复
    # ★★ 只在**第一轮没有任何精确命中**时才跑 ★★
    #   踩坑：`Rock Human 是什么` 第一轮已精确命中 rock_human，
    #   第二轮又用英文前缀 "rock" 把 **rock_organism** 也拉了进来
    #   → 答案从 character_fact 变成 2 个候选，M72 的
    #   「问组织/物种时给出『不是个体角色』说明」当场挂掉。
    #   → 精确优先：能按全名/别名认出来，就不该再猜简称。
    if not out:
        for cid, seg in fuzzy_hits(q):
            if cid not in seen:
                seen.add(cid)
                out.append((cid, seg))
    return out


def resolve(q: str) -> list[dict]:
    data = load()
    return [{**data["by_id"][cid], "matched_term": nm}
            for cid, nm in match(q)]


# 页面类型 → 给用户看的说明（M72）。
# ★ 只有**不是个体角色**的才给说明 —— 正常角色不加噪音。
_KIND_NOTE = {
    "group": "⚠ 源站把它归在角色分类里，但条目用的是「组织/物种」模板"
             "——**这是一个群体概念，不是某一个人物**。",
    "family": "⚠ 源站把它归在角色分类里，但条目用的是「家族」模板"
              "——**这是一个家族，不是某一个人物**。",
    "item": "⚠ 源站把它归在角色分类里，但条目用的是「道具」模板"
            "——**这是一件物品，不是人物**。",
    "unknown": "⚠ 该条目没有 Infobox，源站分类把它列在角色里，"
               "但无法确认是独立角色（可能是无名小角色或列表条目）。",
}


def _kind_note(kind: str | None) -> str | None:
    return _KIND_NOTE.get(kind or "")


@lru_cache(maxsize=1)
def _stand_zh() -> dict[str, dict]:
    """替身 id → 中文名记录（`zh_wiki_pairs`，离线可用）。

    ★ 走 aliases.load_zh_map 而不是查库：降级模式（DB 不可用）下也要能显示中文名。
    """
    try:
        from aliases import load_zh_map
        return load_zh_map()
    except Exception:  # noqa: BLE001
        return {}


def image_url(character_id: str | None) -> str | None:
    """角色头像 URL（M74）。图片由 fetch_entity_images.py 预抓，走 /media 端点。

    ★ 不做「索引里有没有」的检查：前端 `<img onerror>` 会隐藏坏图，
      比后端每次多读一次 JSON 便宜。
    """
    return f"/media/character/{character_id}" if character_id else None


def list_by_part(part: int) -> list[dict]:
    return sorted((c for c in load()["by_id"].values() if c.get("part") == part),
                  key=lambda c: c.get("name_en") or "")


def _part_of(q: str) -> int | None:
    m = _PART_RE.search(q)
    if not m:
        return None
    t = m.group(1)
    return int(t) if t.isdigit() else _PART_CN.get(t)


def _part_of_char(c: dict, part_map: dict | None) -> tuple[int | None, str | None, str | None]:
    """决定角色的「部数」用哪一个，并说明差异。

    ★★ 为什么不能只用 mangadebut 推断 ★★
      实测：`Jotaro Kujo` 的首次出场是 **Chapter 113**（第 2 部最后一章，
      露个脸的伏笔），而他是**第 3 部主角**；`DIO` 同理（第 1 部登场、
      第 3 部才是主要登场）。168 个角色里有 **9 个**属于这种情况。
      → DB 里的 `characters.part` 是既有的、已被前几个里程碑验证过的值，
         **优先用它**；推断值只作兜底，并在两者不一致时**把差异写出来**
         （不静默丢弃 —— 否则用户看到「第 3 部」会以为数据错了）。
    """
    inferred = c.get("part")
    if part_map:
        p = part_map.get(c["character_id"])
        if p:
            note = None
            if inferred and inferred != p:
                note = (f"按首次出场算是第 {inferred} 部"
                        f"（{c.get('part_basis')}），主要登场为第 {p} 部")
            return p, "db", note
    return inferred, ("mangadebut" if inferred else None), None


def _str_or_list(v):
    if isinstance(v, list):
        return [x for x in v if x] or None
    return v or None


def card(c: dict, part_map: dict | None = None) -> dict:
    """角色档案卡（前端渲染用）。**缺的字段保留 None**，由前端写「源站未提供」。"""
    p, p_src, p_note = _part_of_char(c, part_map)
    return {
        "character_id": c["character_id"],
        "name_en": c.get("name_en"),
        "name_zh": c.get("name_zh"),
        "name_zh_variants": c.get("name_zh_variants") or [],
        "name_ja": c.get("name_ja"),
        "name_romaji": c.get("name_romaji"),
        "aliases": c.get("aliases") or [],
        "part": p,
        "part_source": p_src,
        "part_note": p_note,
        "part_by_debut": c.get("part"),
        "stand_ids": c.get("stand_ids") or [],
        "stand_names": c.get("stand_names") or [],
        "gender": c.get("gender"),
        "age": c.get("age"),
        "birthday": c.get("birthday"),
        "height": c.get("height"),
        "weight": c.get("weight"),
        "blood": c.get("blood"),
        "nations": c.get("nations") or [],
        "occupations": c.get("occupations") or [],
        "families": c.get("families") or [],
        "affiliations": c.get("affiliations") or [],
        "status": c.get("status"),
        "seiyuus": c.get("seiyuus") or [],
        "mangadebut": c.get("mangadebut"),
        "animedebut": c.get("animedebut"),
        "bio": c.get("bio"),
        "background": c.get("background"),
        "personality": c.get("personality"),
        "bio_chunk_id": c.get("bio_chunk_id"),
        "background_chunk_id": c.get("background_chunk_id"),
        "personality_chunk_id": c.get("personality_chunk_id"),
        "source_url": c.get("source_url"),
        # ★ 源站未提供的字段要**明说**，不填 0（本项目的硬规则）
        "missing": [k for k in ("name_zh", "bio", "background", "personality",
                                "age", "birthday", "nations")
                    if not c.get(k)],
    }


def _evidence(c: dict) -> list[dict]:
    """把简介/背景/性格转成可展示的证据（每条带 chunk_id 与原文）。"""
    out = []
    for key, section in (("bio", "简介（首段）"),
                         ("background", "背景"),
                         ("personality", "性格")):
        text = c.get(key)
        cid = c.get(f"{key}_chunk_id")
        if text:
            out.append({
                "type": "character_corpus",
                "chunk_id": cid,
                "section": section,
                "content": text,
                "note": (f"角色条目「{section}」"
                         + (f"，语料第 {cid} 块" if cid else "（未切块）")),
            })
    return out


def answer(q: str, top_k: int = 3, part_map: dict | None = None):
    """组织角色问句的答案。

    Returns:
        (answer: dict, evidence: list[dict], card: dict | None)
        answer['type'] ∈ {character_fact, character_list, character_stands,
                          character_hint}
    """
    from_name = resolve(q)
    part = _part_of(q)
    wants_list = bool(_LIST_ASK.search(q))
    wants_stand = bool(_STAND_ASK.search(q))

    # ---- ★★ M74：多个角色同名/同姓 → **全部返回**（用户明确要求）★★
    #   实测「齐贝林」命中 5 个（西撒 / 杰洛 / 格雷戈里奥 / 马力欧 / 威廉），
    #   「东方」命中一票东方家成员。
    #   以前的行为：M73 之前直接查不到（歧义简称不收）；
    #               之后只取 `from_name[0]` → **随机答一个**。
    #   用户要求原话：「要是有重名的角色，如齐贝林、乔斯达、东方仗助，
    #                    就都返回就行了」。
    #   ★ 条件里排除 `part`：问「第 4 部有哪些角色」时不该被候选列表抢走。
    if len(from_name) > 1 and not wants_list and not part:
        first = from_name[0]
        matched = first.get("matched_term") or ""
        return ({
            "type": "character_candidates",
            "query": q.strip(),
            "matched": matched,
            "count": len(from_name),
            "items": [{
                "character_id": c["character_id"],
                "name": c.get("name_zh") or c.get("name_en"),
                "name_en": c.get("name_en"),
                "part": _part_of_char(c, part_map)[0],
                "stands": c.get("stand_names") or [],
                "page_kind": c.get("page_kind"),
                "image": image_url(c["character_id"]),   # ★ M74 头像
            } for c in from_name],
            "note": (f"「{matched}」匹配到 **{len(from_name)}** 个角色"
                     "（同名或同姓），已全部列出。带上全名可以精确提问，"
                     "例如「" + (first.get("name_zh") or first.get("name_en")
                                  or "") + " 的替身是什么」。"),
        }, _evidence(first), None)

    # ---- ① 问某角色「的替身是什么」/「有什么能力」----
    #   ★ 注意：「X的替身是什么」在路由层已被 STAND_OF_ASK 拦走（那是**结构化反查**，
    #     用户要的就是一个替身名）。这里兜的是另一种问法：
    #     「东方仗助有什么替身」「东方仗助的能力」
    #   ★ 不能加 `not wants_list`：`"迪亚哥有什么替身"` 里「有什么」会同时命中
    #     列表词，加了就会掉到档案分支去（和 M65 地点那处同型的 bug）。
    if from_name and wants_stand:
        c = from_name[0]
        names = c.get("stand_names") or []
        ids = c.get("stand_ids") or []
        # ★★ M77：连中文名一起给 ★★
        #   实测用户问「使用者 Giorno Giovanna 有哪些替身？」，
        #   答案只有英文 Gold Experience —— 与界面上其它地方的中文名对不上。
        _zh = _stand_zh()
        names_zh = [(_zh.get(i) or {}).get("name_zh") for i in ids]
        if names:
            # ★ 说明文案必须**跟着数据变**：原来是一句固定的
            #   「没有替身说明其条目未标（…）」，挂在 2 个替身下面
            #   读起来像「系统认为他没有替身」—— 用户当场反馈「对不上」。
            #   ★ 不举具体例子：中文译名不统一（「镇魂歌 / 镇魂曲」），
            #     写一个错的例子比不写更糟（实测一开始就踩到）。
            note = (f"共 {len(names)} 个替身（同一替身的不同形态或进化会并列）。"
                    "数据来自源站角色条目的 Infobox（stand 字段）。")
        else:
            note = ("该角色条目未标注替身"
                    "（例如第 1、2 部使用波纹的角色本来就没有替身）。")
        return ({
            "type": "character_stands",
            "character_id": c["character_id"],
            "character": c.get("name_zh") or c.get("name_en"),
            "stands": names,
            "stands_zh": names_zh,          # ★ M77：与 stands 同序
            "stand_ids": ids,
            "note": note,
        }, _evidence(c), card(c, part_map))

    # ---- ② 「第 X 部有哪些角色」----
    if part and wants_list and not from_name:
        items = list_by_part(part)
        return ({
            "type": "character_list",
            "scope": f"第 {part} 部",
            "count": len(items),
            "items": [{"character_id": c["character_id"],
                       "name": c.get("name_zh") or c.get("name_en"),
                       "name_en": c.get("name_en"),
                       "has_stand": bool(c.get("stand_ids"))}
                      for c in items],
            "note": ("按角色条目的首次登场章节判部（与地点同一套编号区间）；"
                     "本库只收正传 9 部。"),
        }, [], None)

    # ---- ③ 泛问「有哪些角色」----
    if wants_list and not from_name:
        items = sorted(load()["by_id"].values(),
                       key=lambda c: (c.get("part") or 0, c.get("name_en") or ""))
        return ({
            "type": "character_list",
            "scope": "全部（正传 9 部）",
            "count": len(items),
            "items": [{"character_id": c["character_id"],
                       "name": c.get("name_zh") or c.get("name_en"),
                       "name_en": c.get("name_en"),
                       "part": c.get("part")} for c in items],
        }, [], None)

    # ---- ★★ M77：亲属关系（「普奇的弟弟是谁」）★★ ----
    #   用户实测：问「普奇的弟弟是谁」只返回普奇的档案。
    #   数据其实在 `families` 里（`Domenico Pucci (Twin Brother)`），
    #   但从来没有代码读它 —— 问句于是被当成「普奇 是谁」。
    #   ★ 必须放在**档案兜底之前**，且不影响 ①（问替身）/②（问列表）。
    if from_name:
        try:
            from relations import detect as _rel_detect, relatives_of as _rel_of
            _role = _rel_detect(q)
            if _role:
                c = from_name[0]
                return _rel_of(c, _role), _evidence(c), card(c, part_map)
        except Exception:  # noqa: BLE001
            # 亲属是增强项，出错不能影响主答案（继续走档案分支）
            pass

    # ---- ④ 单角色档案（默认）----
    if from_name:
        c = from_name[0]
        k = card(c, part_map)
        return ({
            "type": "character_fact",
            "character_id": c["character_id"],
            "name_en": c.get("name_en"),
            "name_zh": c.get("name_zh"),
            "name_ja": c.get("name_ja"),
            "part": k["part"],
            "part_note": k.get("part_note"),
            "stands": c.get("stand_names") or [],
            "status": c.get("status"),
            # ★ M72：如实说明「这一页是什么」。
            #   源站分类不干净，混着组织/家族/物种（Group/Family Infobox）
            #   与少数无 Infobox 的文章。它们**不是个体角色** ——
            #   用户问「岩人是什么」时应该说明这是物种而非某个人，
            #   而不是含糊地当成角色介绍。
            "page_kind": c.get("page_kind"),
            "page_kind_note": _kind_note(c.get("page_kind")),
            # ★ M74：头像（用户反馈「搜索人物时不会返回人物的图片」）
            "image": image_url(c["character_id"]),
            # ★ 简介是**英文原文**（不机翻），前端与「角色资料」卡里都显示
            "bio": c.get("bio"),
            "bio_chunk_id": c.get("bio_chunk_id"),
        }, _evidence(c), k)

    # ---- ⑤ 提到角色意图但没识别出名字 ----
    return ({
        "type": "character_hint",
        "note": (f"没有识别出具体角色。本库收录 {n_characters()} 个正传角色，"
                 "可问「东方仗助是谁」「第 4 部有哪些角色」。"),
    }, [], None)


# ---------------------------------------------------------------
if __name__ == "__main__":
    # 手测时若 DB 可用，就把 DB 的部数一起带上（与 API 行为一致）
    db_parts: dict = {}
    try:
        import sys as _s
        _s.path.insert(0, str(ROOT / "database"))
        from load_db import connect as _conn
        _c = _conn().cursor()
        _c.execute("SELECT character_id, part FROM characters")
        db_parts = {a: b for a, b in _c.fetchall() if b}
        _c.close()
    except Exception:                                   # noqa: BLE001
        pass
    d = load()
    print(f"角色 {d['n']} 个 · 有中文名 {d['n_with_zh']} · "
          f"简介 {d['n_with_bio']} · 背景 {d['n_with_background']} · "
          f"性格 {d['n_with_personality']} · 名称索引 {len(d['by_name'])} 个写法")
    print()
    for q in ["东方仗助是谁", "空条承太郎是谁", "第 4 部有哪些角色",
              "赫特·潘兹的背景", "迪亚哥·布兰度有什么替身", "东方仗助"]:
        a, ev, k = answer(q, part_map=db_parts)
        line = f"  {q:20s} → {a['type']:17s}"
        if a["type"] == "character_fact":
            line += f"{a['name_zh'] or a['name_en']} 第{a['part']}部 替身={a['stands']}"
        elif a["type"] == "character_list":
            line += f"{a['scope']} 共 {a['count']}"
        elif a["type"] == "character_stands":
            line += f"{a['character']} → {a['stands']}"
        else:
            line += a.get("note", "")[:40]
        print(line)
        if ev:
            print(f"      证据 {len(ev)} 条（简介块 {a.get('bio_chunk_id')}）")

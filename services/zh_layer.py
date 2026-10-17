"""中文要点层（M77）：把「库里已有的中文事实」拼成中文句子，放在英文原文之前。

★★ 为什么需要 ★★
  语料 100% 来自 jojowiki 英文站（`text_chunks` 10866 块里含中文的只有 155 块，
  且那 155 块是替身名里的汉字，如「女帝」）。用户问「黄金体验的能力是什么」，
  检索是对的，但返回的是一整段英文 —— M32 的中文卡只解决了「是谁 / 六维」，
  没解决「能力是什么」这类描述性正文。

★★ 为什么不是机器翻译 ★★
  项目硬规则：**不造词、不编内容**。JOJO 的替身名/招式名/专有名词译法不统一
  （「布差拉迪 / 布加拉提」「黄金体验 / 黄金之风」），机翻会把可追溯的证据链
  变成不可核对的二次创作。
  → 这里只做两件事：
    ① 用**已核验的结构化字段**（中文名/使用者/部数/六维/招式数）拼中文句子；
    ② 明确标注「描述性正文仍是源站英文原文，见下方」，让用户知道自己在读什么。

★ 缺数据一律写「源站未提供」，**不填 0、不编**（与 M17 雷达图同一条口径）。
"""
from __future__ import annotations

from typing import Any, Optional

# 部名（正传 9 部，来自中文维基各部的正式译名；第 9 部尚无统一中译 → 留英文）
PART_CN = {
    1: "幻影血脉",
    2: "战斗潮流",
    3: "星尘斗士",
    4: "不灭钻石",
    5: "黄金之风",
    6: "石之海",
    7: "飙马野郎",
    8: "JoJolion",
}

_NOTE = ("以上中文要点由库内**结构化字段**拼成（不是机器翻译）；"
         "描述性正文仍是源站英文原文，见下方。")

# 状态是**封闭小集合**（源站只有这几个词），可以安全映射；
# 其余带描述的长句（如 "Deceased (Alive as Stray Cat)"）**保持原文**，
# 不硬拆 —— 拆错比显示英文更糟。
STATUS_CN = {
    "Alive": "存活",
    "Deceased": "已故",
    "Retired": "退场",
    "Missing": "下落不明",
    "Unknown": "未知",
    "Permanently Retired": "永久退场",
}


def _status(v: Optional[str]) -> Optional[str]:
    if not v:
        return None
    s = v.strip()
    return STATUS_CN.get(s, s)


def part_label(part: Optional[int]) -> str:
    """第 5 部《黄金之风》/ 第 9 部 / 部数未知。"""
    if not part:
        return "部数未知"
    name = PART_CN.get(int(part))
    return f"第 {int(part)} 部《{name}》" if name else f"第 {int(part)} 部"


def _join(items: list[str], empty: str = "源站未提供") -> str:
    items = [x for x in items if x]
    return "、".join(items) if items else empty


def stand_summary(zh_card: dict | None,
                  moves: Optional[list[str]] = None) -> Optional[dict]:
    """替身的中文要点（喂给前端，放在英文原文之前）。"""
    if not zh_card:
        return None
    name_zh = zh_card.get("name_zh")
    name_en = zh_card.get("name_en")
    owner = zh_card.get("owner") or zh_card.get("owner_en")
    part = zh_card.get("part")

    head = name_zh or name_en or "该替身"
    if name_zh and name_en:
        head = f"{name_zh}（{name_en}）"

    lines: list[str] = []
    who = f"{owner}的替身" if owner else "替身（使用者源站未提供）"
    lines.append(f"{head}是{part_label(part)}中{who}。")

    stats = zh_card.get("stats_cn") or {}
    if stats:
        shown = [f"{k} {v if v else '源站未提供'}" for k, v in stats.items()]
        lines.append("六维：" + "、".join(shown) + "。")
    else:
        lines.append("六维：源站未提供。")

    if moves:
        # 招式名保留源站原文（中文译名不统一，不机翻）
        lines.append(f"已知招式 {len(moves)} 个：" + "、".join(moves) + "。")
    elif moves is not None:
        lines.append("招式：源站该条目没有独立招式小节。")

    return {"kind": "stand", "lines": lines, "note": _NOTE}


def character_summary(card: dict | None) -> Optional[dict]:
    """角色的中文要点。"""
    if not card:
        return None
    name_zh = card.get("name_zh")
    name_en = card.get("name_en")
    head = f"{name_zh}（{name_en}）" if name_zh and name_en else (name_zh or name_en)
    if not head:
        return None

    lines = [f"{head}是{part_label(card.get('part'))}的角色。"]
    stands = card.get("stand_names") or []
    lines.append("替身：" + _join(stands) + "。")

    facts = []
    if card.get("status"):
        facts.append(f"状态：{_status(card['status'])}")
    if card.get("age"):
        facts.append(f"年龄：{card['age']}")
    if card.get("occupations"):
        facts.append("职业：" + _join(card["occupations"]))
    if facts:
        lines.append("；".join(facts) + "。")

    return {"kind": "character", "lines": lines, "note": _NOTE}


def location_summary(card: dict | None) -> Optional[dict]:
    """地点的中文要点。"""
    if not card:
        return None
    name_zh = card.get("name_zh")
    name_en = card.get("name_en")
    head = f"{name_zh}（{name_en}）" if name_zh and name_en else (name_zh or name_en)
    if not head:
        return None

    lines = [f"{head}是{part_label(card.get('part'))}的地点。"]
    if card.get("types"):
        lines.append("类型：" + _join(card["types"]) + "。")
    paths = card.get("place_paths") or []
    if paths:
        top = paths[0]
        if isinstance(top, (list, tuple)) and top:
            lines.append("所属：" + " → ".join(str(x) for x in top) + "。")
    if card.get("mangadebuts"):
        lines.append("首次登场：" + _join(card["mangadebuts"]) + "。")

    return {"kind": "location", "lines": lines, "note": _NOTE}


def build(*, zh_card: dict | None = None,
          character_card: dict | None = None,
          location_card: dict | None = None,
          moves: Optional[list[str]] = None) -> Optional[dict[str, Any]]:
    """按优先级取一个要点块（替身 > 地点 > 角色 —— 与路由优先级一致）。"""
    for fn, arg in ((stand_summary, (zh_card, moves)),
                    (location_summary, (location_card,)),
                    (character_summary, (character_card,))):
        out = fn(*arg)
        if out and out.get("lines"):
            return out
    return None

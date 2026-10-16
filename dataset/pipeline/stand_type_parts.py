"""替身类型串切分（M69）。

== 为什么要单独一层 ==
`stands.stand_type` 是从 Infobox 的多项**直接拼接**来的，实测形如：
    "Close-RangeRange IrrelevantNatural HumanoidSentientEvolved"
    "AutomaticMaterializedPsychological AssaultArtificial Non-HumanoidMulti-UserSentient"
既没有分隔符、又夹着括号注记。
M32 已经在**展示层**用一份 15 词的词表清洗过一次，但那层有三个问题：
  ① 词表不全 —— 语料实测出现的 `Natural Non-Humanoid` /
     `Psychological Assault` / `Sentient` / `Multi-Unit` / `Multi-User` /
     `Foresight` / `Evolved` 都不在表里；而 `Natural Humanoid` 是
     `Natural Non-Humanoid` 的**子串**，先命中短的那个就会切错。
  ② 只在输出时清洗，**底层值仍然是粘的** → 任何按类型过滤/统计的查询
     都得在 SQL 之外自己再解析一遍。
  ③ 词表定义在 `api/main.py` 里，数据侧无法复用。

== ★ 与 M67 的区别（为什么当初放弃、现在能做）==
M67 修 owner 粘连时**明确放弃**了 stand_type，理由是 `{{ST}}` 是缩写模板、
无法从 wikitext 可靠展开。
本模块**不做展开**：它只切分 `stands.json` 里**已经展开好**的值。
词表由实测反推（见 `_VOCAB` 的注释），跑下来残余为 0 ——
即每个字符都能被某个已知词解释，不存在"猜"的部分。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

# ★ 封闭词表。顺序无关（切分时按长度降序贪心），
#   但**新增词必须先在 diagnose_stand_type.py 上验证残余为 0**。
_VOCAB: tuple[str, ...] = (
    # 射程
    "Range Irrelevant", "Close-Range", "Long-Range", "Long Distance",
    # 体质（长的在前：Natural Non-Humanoid 必须先于 Natural Humanoid）
    "Natural Non-Humanoid", "Artificial Non-Humanoid",
    "Natural Humanoid", "Artificial Humanoid",
    # 特性
    "Psychological Assault", "Physical Assault",
    "Reconnaissance", "Materialized", "Integrated", "Multilayered",
    "Phenomenon", "Automatic", "Colony", "Bound", "Wearable",
    "Tool", "Spirit", "Psychic", "Sentient", "Multi-Unit",
    "Multi-User", "Non-Existent", "Shared", "Crystallization",
    "Animal", "Foresight", "Evolved",
)

# 中文译名。只用于展示，不影响切分。
_CN: dict[str, str] = {
    "Close-Range": "近距离型", "Long-Range": "远距离型",
    "Range Irrelevant": "射程无关", "Long Distance": "远距离",
    "Reconnaissance": "侦查型",
    "Natural Humanoid": "自然人型", "Artificial Humanoid": "人造人型",
    "Natural Non-Humanoid": "自然非人型", "Artificial Non-Humanoid": "人造非人型",
    "Automatic": "自动操纵型", "Colony": "群体型", "Bound": "依附型",
    "Integrated": "一体型", "Wearable": "穿戴型", "Tool": "道具型",
    "Materialized": "物质化型", "Phenomenon": "现象型",
    "Spirit": "精神型", "Psychic": "超能力型", "Sentient": "有自主意识",
    "Multi-Unit": "多单元", "Multi-User": "多用户", "Shared": "共享型",
    "Foresight": "预知型", "Evolved": "进化型",
    "Psychological Assault": "精神攻击", "Physical Assault": "物理攻击",
    "Multilayered": "多层型", "Non-Existent": "非实体",
    "Crystallization": "结晶化", "Animal": "动物型",
}

# 括注是 Infobox 里的自由注记（如 "Phenomenon (Other)"），不入类型。
_NOTE_RE = re.compile(r"\([^)]*\)")

# 射程类词（"射程段"）：源站允许同时出现两个互相矛盾的射程
#   （实测 Aqua Necklace = "Close-Range…Range Irrelevant…"、
#     Bites the Dust = "Range Irrelevant…Close-Range…"）
#   → **如实记录全部**，不替源站做取舍；range_type 取第一个作为主射程。
_RANGE_WORDS = ("Range Irrelevant", "Close-Range", "Long-Range", "Long Distance")


@dataclass(frozen=True)
class TypeParts:
    """切分结果。

    ★ 用 dataclass 而不是 NamedTuple：内部要带两个以 `_` 开头的
      命中记录字段，而 namedtuple **禁止**下划线开头的字段名
      （`ValueError: Field names cannot start with an underscore`）。
    """

    range_type: Optional[str]       # 主射程（射程段的第一个）
    ranges: tuple[str, ...]         # 全部射程词（源站可能同时给两个矛盾的）
    kinds: tuple[str, ...]          # 体质段（人型/非人型，可多个）
    extras: tuple[str, ...]         # 其余特性（有序、无重复）
    residual: str                   # 未识别残余；期望为 ""
    words: tuple[str, ...] = ()     # 去重后的命中词（按源串顺序）
    hit_idx: tuple[int, ...] = ()   # words 中有效命中的下标

    @property
    def complete(self) -> bool:
        return self.residual == ""

    def all_en(self) -> list[str]:
        """按**源串里的原始顺序**还原（不是按段重排）。

        ★ 为什么保序：射程/体质/特性重排后，读起来会像是源站按那个顺序
          写的，而实测源串顺序本来就是乱的（Tusk 是 Evolved 在最前）。
          → 保序即如实。
        ★ 用原始命中记录而不是按段拼：这样 all_en 天然等于源串里
          词的出现顺序，也保证"三段合起来 == 全部命中"恒成立。
        """
        return [self.words[i] for i in self.hit_idx]

    def all_cn(self) -> list[str]:
        return [cn_of(w) for w in self.all_en()]

    def readable_cn(self) -> str:
        """「近距离型 · 自然人型 · 有自主意识」；识别不出返回空串。"""
        return " · ".join(self.all_cn())


def cn_of(word: str) -> str:
    return _CN.get(word, word)


def _ordered() -> list[str]:
    """按长度降序，保证 `Natural Non-Humanoid` 先于 `Natural Humanoid`。"""
    return sorted(set(_VOCAB), key=lambda w: (-len(w), w))


def split_stand_type(raw: Optional[str]) -> TypeParts:
    """把粘连串切成 (主射程, 全部射程, 体质, 特性, 残余)。永不为 None。"""
    if not raw:
        return TypeParts(None, (), (), (), "")
    text = _NOTE_RE.sub(" ", str(raw))
    low = text.lower()
    ordered = _ordered()

    # 贪心最长匹配 + 记录命中区间，避免"用 replace 删残余"误删。
    hits: list[tuple[int, int, str]] = []
    i = 0
    while i < len(low):
        for w in ordered:
            wl = w.lower()
            if low.startswith(wl, i):
                hits.append((i, i + len(wl), w))
                i += len(wl)
                break
        else:
            i += 1

    covered = bytearray(len(text))
    for a, b, _ in hits:
        for k in range(a, b):
            covered[k] = 1
    residual = "".join(ch for k, ch in enumerate(text) if not covered[k]).strip()

    words: list[str] = []          # 去重后的命中词，按源串顺序
    idx: list[int] = []             # words 中"有效命中"的下标
    for _, _, w in hits:
        if w in words:              # ★ 全局去重，不只是相邻去重
            continue
        idx.append(len(words))
        words.append(w)

    ranges = tuple(w for w in words if w in _RANGE_WORDS)
    range_type = ranges[0] if ranges else None
    kinds = tuple(w for w in words if w.endswith("Humanoid"))
    extras = tuple(w for w in words
                   if w not in _RANGE_WORDS and not w.endswith("Humanoid"))
    return TypeParts(range_type, ranges, kinds, extras, residual,
                     tuple(words), tuple(idx))


def readable_cn(raw: Optional[str]) -> Optional[str]:
    """给 api/main.py 用的清洗入口；识别不出返回 None（与 M32 行为一致）。"""
    p = split_stand_type(raw)
    if not p.all_en():
        return None
    return p.readable_cn()


# `Aggregate (form group)` 是 load_db 给未消解形态组合成的**父子标记**，
# 不是 Infobox 里的类型 → 不参与切分，否则会当成"残余"。
AGGREGATE_MARK = "Aggregate (form group)"


def is_aggregate(raw: Optional[str]) -> bool:
    return bool(raw) and str(raw).strip() == AGGREGATE_MARK


def audit(stands: Iterable[dict]) -> dict:
    """给测试与 collect_metrics 用的审计结果。"""
    rows, incomplete = [], []
    for s in stands:
        raw = s.get("stand_type")
        if not raw:
            continue
        p = split_stand_type(raw)
        rec = {
            "stand_id": s.get("stand_id"),
            "name_en": s.get("name_en"),
            "raw": raw,
            "range_type": p.range_type,
            "ranges": list(p.ranges),
            "kinds": list(p.kinds),
            "extras": list(p.extras),
            "readable": p.readable_cn(),
        }
        rows.append(rec)
        if not p.complete:
            incomplete.append(rec)
    return {
        "n_with_type": len(rows),
        "n_complete": len(rows) - len(incomplete),
        "n_incomplete": len(incomplete),
        "incomplete": incomplete,
        "rows": rows,
    }

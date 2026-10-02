"""
编码层：把数据源的原始字面量映射为规范化的 (value, category, note) 三元组。

本模块是 docs/数据规范.md §1.4 判定表的**可执行实现**，
校验规则 V1 / V10 是它的断言（见 validate.py）。

设计约束：
  - 纯函数，无IO、无外部依赖，便于单测
  - 输入输出全部保留原始字面量（*_raw），归一化是 lossy 操作
  - 判定顺序不可调换：条件值规则必须先于普通等级规则

作者：M1 数据管道
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Optional

# ------------------------------------------------------------------
# 常量
# ------------------------------------------------------------------

# 等级 → 数值（数据规范 §1.2）
# 采用 0–5 闭区间，None=0 且与 E=1 区分
RANK_VALUE = {
    "None": 0,
    "E": 1,
    "D": 2,
    "C": 3,
    "B": 4,
    "A": 5,
}

# 空值字面量白名单（数据规范 §1.3，v1.1 新增）
# 不同源用不同字面量表示同一语义，统一归一为 EMPTY_SLOT
# 注意：? 不在此列表，见 UNKNOWN
EMPTY_SLOT_TOKENS = {"∅", "undefined", "N/A", "none", "null", "NULL", "-", "—"}

# 六维字段名
STAT_DIMS = ["PWR", "SPD", "RNG", "STA", "PRC", "DEV"]

# 形态后缀噪声：源数据名里混入的尾随引号等
# 注意：不能剥离右括号 )，因为条件值说明里含完整括号，如
#   'C※Range: 2 m (6.6 ft)'  括号是语义的一部分
TRAILING_QUOTES = re.compile(r'[\s"“”‘’\']+$')


class Category(str, Enum):
    """能力值类别。取值与数据库 stat_category 枚举严格一致。"""

    RANKED = "RANKED"                                # 正常等级，有数值
    NONE = "NONE"                                    # 明确无此能力，数值 0
    EMPTY_SLOT = "EMPTY_SLOT"                        # 空位，数值 NULL
    UNKNOWN = "UNKNOWN"                              # 未知（?），数值 NULL
    NOT_APPLICABLE = "NOT_APPLICABLE"                # 不适用，数值 NULL
    INFINITE = "INFINITE"                            # 无限（∞），数值 NULL
    CONDITIONAL = "CONDITIONAL"                      # 条件值有基础等级
    CONDITIONAL_NO_BASE = "CONDITIONAL_NO_BASE"      # 条件值无基础等级
    UNPARSED = "UNPARSED"                            # 无法解析，需人工介入


# 数值有效的类别（V10 断言用）
CATEGORIES_WITH_VALUE = {Category.RANKED, Category.CONDITIONAL}
# 数值必须为 NULL 的类别（V10 断言用）
CATEGORIES_WITHOUT_VALUE = {
    Category.EMPTY_SLOT,
    Category.UNKNOWN,
    Category.NOT_APPLICABLE,
    Category.INFINITE,
    Category.CONDITIONAL_NO_BASE,
    Category.UNPARSED,
}


@dataclass(frozen=True)
class EncodedStat:
    """单维能力值的编码结果。

    Attributes:
        value: 规范化数值；无意义时为 None
        category: 语义类别
        note: 条件值/补充说明；无则None
        raw: 源原始字面量（永远保留，V11 要求非空）
    """

    value: Optional[int]
    category: Category
    note: Optional[str]
    raw: str

    @property
    def has_value(self) -> bool:
        return self.value is not None

    @property
    def is_anomalous(self) -> bool:
        """是否为异常值（非正常等级）—— D8 实验的分组依据。"""
        return self.category != Category.RANKED

    def to_dict(self) -> dict:
        return {
            "value": self.value,
            "category": self.category.value,
            "note": self.note,
            "raw": self.raw,
        }


# ------------------------------------------------------------------
# 预处理
# ------------------------------------------------------------------

def normalize(raw: str) -> str:
    """归一化字面量：NFKC、去首尾空白与多余引号、统一全角符号。

    处理实测发现的脏数据形态：
      - 不间断空格 NBSP (0xa0) → 普通空格
      - 尾随引号 ' " → 剥离（但**保留括号**，条件值说明依赖完整括号）
      - 全角括号（）→ 半角

    注意：这里只处理能力值字面量（含条件值说明），不做尾随括号剥离。
    make_stand_id 另有更激进的名称清洗逻辑，见该函数。
    """
    if raw is None:
        return ""
    s = unicodedata.normalize("NFKC", str(raw))
    s = s.replace("\xa0", " ")# NBSP
    s = TRAILING_QUOTES.sub("", s)
    s = s.strip()
    # 统一全角符号
    for full, half in (("：", ":"), ("（", "("), ("）", ")"), ("／", "/")):
        s = s.replace(full, half)
    return s


# ------------------------------------------------------------------
# 判定表（数据规范 §1.4）
#
# 顺序不可调换：
#   规则 1（X※描述）必须先于规则 4（纯等级），
#   否则 'B※20-30 meters' 会被误判为非法值
# ------------------------------------------------------------------

_RE_RANK_COND = re.compile(r"^([A-E])\s*※\s*(.+)$")
_RE_SPECIAL_COND = re.compile(r"^([?∅∞]|None|none)\s*※\s*(.+)$")
_RE_COND_LEAD = re.compile(r"^※\s*(.+)$")
_RE_RANK_ONLY = re.compile(r"^([A-E])$")
_RE_SPECIAL_ONLY = re.compile(r"^(None|none)$")
_RE_EMPTY = re.compile(r"^(∅|undefined|N/A|null|NULL|-|—)$")
_RE_UNKNOWN = re.compile(r"^\?$")
_RE_INFINITE = re.compile(r"^(∞|Infinite|infinite)$")


def encode_stat(raw: str) -> EncodedStat:
    """把单个能力值字面量编码为 EncodedStat。

    对应数据规范 §1.4 的九步判定表。实现与文档必须同步修改。

    Examples:
        >>> encode_stat("A").to_dict()
        {'value': 5, 'category': 'RANKED', 'note': None, 'raw': 'A'}

        >>> encode_stat("∅").to_dict()
        {'value': None, 'category': 'EMPTY_SLOT', 'note': None, 'raw': '∅'}

        >>> encode_stat("B※20-30 meters").to_dict()
        {'value': 4, 'category': 'CONDITIONAL', 'note': '20-30 meters', ...}

        >>> encode_stat("※Complete").to_dict()
        {'value': None, 'category': 'CONDITIONAL_NO_BASE', 'note': 'Complete', ...}
    """
    original = "" if raw is None else str(raw)
    s = normalize(original)

    if s == "":
        return EncodedStat(None, Category.EMPTY_SLOT, None, original)

    # 规则 1：X※描述 —— 有基础等级，条件值
    m = _RE_RANK_COND.match(s)
    if m:
        return EncodedStat(
            RANK_VALUE[m.group(1)], Category.CONDITIONAL, m.group(2), original
        )

    # 规则 2：?※ / ∅※ / ∞※ / None※ —— 特殊值带说明，不可数值化
    m = _RE_SPECIAL_COND.match(s)
    if m:
        return EncodedStat(
            None, Category.CONDITIONAL_NO_BASE, m.group(2), original
        )

    # 规则 3：※描述 —— 无基础等级
    m = _RE_COND_LEAD.match(s)
    if m:
        return EncodedStat(None, Category.CONDITIONAL_NO_BASE, m.group(1), original)

    # 规则 4：纯等级 A–E
    m = _RE_RANK_ONLY.match(s)
    if m:
        return EncodedStat(RANK_VALUE[m.group(1)], Category.RANKED, None, original)

    # 规则 5：None —— 明确无此能力，数值为 0
    if _RE_SPECIAL_ONLY.match(s):
        return EncodedStat(0, Category.NONE, None, original)

    # 规则 6：空位字面量（白名单）
    if _RE_EMPTY.match(s):
        return EncodedStat(None, Category.EMPTY_SLOT, None, original)

    # 规则 7：? —— 未知。注意这是独立语义，不并入 EMPTY_SLOT
    if _RE_UNKNOWN.match(s):
        return EncodedStat(None, Category.UNKNOWN, None, original)

    # 规则 8：∞ —— 无限，保留原值
    if _RE_INFINITE.match(s):
        return EncodedStat(None, Category.INFINITE, None, original)

    # 规则 9：兜底 —— 需人工介入
    return EncodedStat(None, Category.UNPARSED, s, original)


# ------------------------------------------------------------------
# 行级编码
# ------------------------------------------------------------------

def make_stand_id(name: str) -> str:
    """名称 → 规范化 ID（仅用于 ID 生成，可激进清洗）。

    与 normalize 的区别：这里会剥离括号等结构字符，
    因为 'Echoes (ACT3)' 与 'Echoes ACT3' 应指向同一实体。
    非平衡的括号直接删除，不影响能力值解析（那走 normalize）。

    >>> make_stand_id("Star Platinum")
    'star_platinum'
    >>> make_stand_id("Foo Fighters\"")
    'foo_fighters'
    >>> make_stand_id("Echoes (ACT3)")
    'echoes_act3'
    """
    s = unicodedata.normalize("NFKC", str(name or ""))
    s = s.replace("\xa0", " ")
    s = TRAILING_QUOTES.sub("", s.strip())
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_")


@dataclass
class EncodedRow:
    """一行六维的完整编码结果。"""

    stand_id: str
    name_raw: str
    stats: dict[str, EncodedStat]

    @property
    def missing_count(self) -> int:
        return sum(1 for st in self.stats.values() if st.value is None)

    @property
    def has_partial(self) -> bool:
        return self.missing_count > 0

    @property
    def composite(self) -> Optional[int]:
        """综合分。仅当 6 维全部有值时计算，否则 None。

        **禁止取平均** —— 数据规范 §1.7 明确约束。
        任一维缺失就置 NULL，否则会生成虚假的可比数值。
        """
        vals = [st.value for st in self.stats.values()]
        if any(v is None for v in vals):
            return None
        return sum(vals)

    def categories(self) -> dict[str, str]:
        return {d: st.category.value for d, st in self.stats.items()}

    def to_record(self) -> dict:
        """转为可直接写入 stand_stats 的扁平 dict。"""
        rec: dict = {
            "stand_id": self.stand_id,
            "composite": self.composite,
            "missing_count": self.missing_count,
            "has_partial": 1 if self.has_partial else 0,
        }
        for dim, st in self.stats.items():
            d = dim.lower()
            rec[d] = st.value
            rec[f"{d}_cat"] = st.category.value
            rec[f"{d}_note"] = st.note
            rec[f"{d}_raw"] = st.raw
        return rec


def encode_row(name: str, values: list[str]) -> EncodedRow:
    """编码一整行（名称 + 6 个字面量）。

    Args:
        name: 替身名称
        values:长度须为 6，顺序 PWR/SPD/RNG/STA/PRC/DEV

    Raises:
        ValueError: values 长度不为 6
    """
    if len(values) != 6:
        raise ValueError(f"期望 6 个能力值，收到 {len(values)}：{values!r}")
    stats = {
        dim: encode_stat(raw) for dim, raw in zip(STAT_DIMS, values)
    }
    return EncodedRow(stand_id=make_stand_id(name), name_raw=name, stats=stats)


def encode_csv_row(cells: list[str], name_index: int = 0) -> EncodedRow:
    """按位置编码（跳过名称列），忽略列名。

    重要：CSV 镜像的列名不可信（实测第 4 列名为 PER，实为 STA=持续力），
    因此一律**按位置映射**，不按列名。数据规范校验 V9。
    """
    if len(cells) < 7:
        raise ValueError(f"CSV 行长度不足 7 列：{cells!r}")
    return encode_row(cells[name_index], cells[name_index + 1 : name_index + 7])

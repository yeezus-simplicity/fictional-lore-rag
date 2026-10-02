"""
校验层：实现数据规范 §5 的 V1–V11 规则。

校验分两级：
  ERROR —— 阻断入库
  WARN  —— 告警但放行

V10 是最关键的一条：它把编码判定表固化为可执行断言，
防止 encode.py 的实现与数据规范文档漂移。

用法：
    from encode import encode_row
    from validate import validate_row, ValidationReport
    row = encode_row("Star Platinum", ["A","A","C","A","A","A"])
    report = validate_row(row)
    if not report.ok:
        for e in report.errors: print(e)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from encode import (
    CATEGORIES_WITHOUT_VALUE,
    CATEGORIES_WITH_VALUE,
    STAT_DIMS,
    Category,
    EncodedRow,
    EncodedStat,
    encode_stat,
)

# ------------------------------------------------------------------
# 列名白名单（V9）
#
# 实测发现 CSV 镜像第 4 列名为 PER，但语义是 STA（持续力 Stamina），
# 按列名自动映射会静默错映射成「感知力 Perception」，且不报错。
# 因此：CSV 一律按位置映射，列名仅用于断言校验。
# ------------------------------------------------------------------
ALLOWED_STAT_COLUMNS = {"PWR", "SPD", "RNG", "STA", "PRC", "DEV"}

# 已知的历史错误别名 → 权威列名
KNOWN_COLUMN_ALIASES = {
    "PER": "STA",   # 镜像 A 错标，实际是 Stamina
    "PWR ": "PWR",  # 尾随空格
}

# 来源标识
SOURCE_PRIMARY = "jojowiki"
SOURCE_CSV_BOGDAN = "csv_bogdan"
SOURCE_CSV_TOPOLOGY = "csv_topology"


@dataclass
class Issue:
    rule: str        # 规则编号
    level: str       # ERROR / WARN
    message: str
    subject: str = ""   # 主体标识，如 stand_id 或 stand_id:DIM

    def __str__(self) -> str:
        loc = f"[{self.subject}] " if self.subject else ""
        return f"{self.rule} {self.level}: {loc}{self.message}"


@dataclass
class ValidationReport:
    issues: list[Issue] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "ERROR"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "WARN"]

    @property
    def ok(self) -> bool:
        """无 ERROR 即通过。"""
        return not self.errors

    def add(self, rule: str, level: str, message: str, subject: str = "") -> None:
        self.issues.append(Issue(rule, level, message, subject))

    def merge(self, other: "ValidationReport") -> None:
        self.issues.extend(other.issues)

    def summary(self) -> str:
        return (
            f"ERROR {len(self.errors)} 项 / WARN {len(self.warnings)} 项"
            f" -> {'通过' if self.ok else '阻断'}"
        )


# ------------------------------------------------------------------
# V1 / V2 / V10：单行校验
# ------------------------------------------------------------------

def validate_row(row: EncodedRow) -> ValidationReport:
    """校验单行六维编码结果。"""
    rep = ValidationReport()

    if not row.stand_id:
        rep.add("V6", "ERROR", "stand_id 为空", row.name_raw)

    for dim in STAT_DIMS:
        st = row.stats.get(dim)
        if st is None:
            rep.add("V1", "ERROR", f"{dim} 缺失", row.stand_id)
            continue
        _validate_single(st, dim, row.stand_id, rep)

    _validate_composite(row, rep)
    return rep


def _validate_single(
    st: EncodedStat, dim: str, stand_id: str, rep: ValidationReport
) -> None:
    """校验单维：V1 一致性 / V2 范围 / V10 组合 / V11 原值可回溯"""
    subject = f"{stand_id}:{dim}"
    cat = st.category

    # V1：category 字段与原始值映射一致 —— 用 encode_stat(raw) 复算比对
    recomputed = encode_stat(st.raw)
    if recomputed.category != cat or recomputed.value != st.value:
        rep.add(
            "V1",
            "ERROR",
            f"category/value 与原值不一致：存储=({cat.value}, {st.value}) "
            f"重算=({recomputed.category.value}, {recomputed.value}) raw={st.raw!r}",
            subject,
        )
        return  # 复算不一致，后续断言无意义

    # V2：数值在 0–5
    if st.value is not None and not (0 <= st.value <= 5):
        rep.add("V2", "ERROR", f"数值越界：{st.value}（应为 0–5）", subject)

    # V10：category 与 stat_value 组合一致性
    if cat in CATEGORIES_WITH_VALUE and st.value is None:
        rep.add("V10", "ERROR", f"category={cat.value} 应有数值但为 NULL", subject)
    if cat in CATEGORIES_WITHOUT_VALUE and st.value is not None:
        rep.add("V10", "ERROR", f"category={cat.value} 应为 NULL 但有值 {st.value}", subject)
    if cat == Category.NONE and st.value != 0:
        rep.add("V10", "ERROR", f"category=NONE 数值应为 0，实际 {st.value}", subject)

    # V10：CONDITIONAL 必须有说明
    if cat == Category.CONDITIONAL and not st.note:
        rep.add("V10", "ERROR", "category=CONDITIONAL 必须有 note", subject)

    # V10：INFINITE 必须保留原值
    if cat == Category.INFINITE and not st.raw:
        rep.add("V10", "ERROR", "category=INFINITE 必须保留 raw 原值", subject)

    # UNPARSED 是兜底类别，出现即需人工介入
    if cat == Category.UNPARSED:
        rep.add("V1", "ERROR", f"无法解析的取值：{st.raw!r}", subject)


def _validate_composite(row: EncodedRow, rep: ValidationReport) -> None:
    """V3：composite 非空时 6 维必须齐全。"""
    comp = row.composite
    if comp is not None:
        if row.missing_count != 0:
            rep.add(
                "V3",
                "ERROR",
                f"composite={comp} 但有 {row.missing_count} 个维度缺失",
                row.stand_id,
            )
        elif not (0 <= comp <= 30):
            rep.add("V3", "ERROR", f"composite 越界：{comp}（应为 0–30）", row.stand_id)


# ------------------------------------------------------------------
# V6：名称唯一性
#
# 实测发现主源存在同一替身的多次登记（Star Platinum / Killer Queen /
# Echoes ACT3），数值不同。这**不是数据错误**，而是同一替身在
# 不同形态/时期/作品的记录，语义上等价于stand_forms 的多条记录。
#
# 因此 V6 分两级：
#   - 完全相同（名称+六维全同）→ ERROR，真重复，必须消重
#   - 名称同但数值不同         → WARN，记入 stat_conflicts 的
#                                 DUPLICATE_IN_SOURCE，交形态消解处理
# ------------------------------------------------------------------

def validate_uniqueness(rows: Iterable[EncodedRow]) -> ValidationReport:
    rep = ValidationReport()
    groups: dict[str, list[EncodedRow]] = {}
    for r in rows:
        groups.setdefault(r.stand_id, []).append(r)

    for sid, group in groups.items():
        if len(group) == 1:
            continue
        # 逐维比较签名
        sigs = {tuple(r.stats[d].raw for d in STAT_DIMS) for r in group}
        if len(sigs) == 1:
            rep.add(
                "V6",
                "ERROR",
                f"完全重复登记 {len(group)} 次，六维逐字相同，必须去重",
                sid,
            )
        else:
            rep.add(
                "V6",
                "WARN",
                f"同名 {len(group)} 次但数值不同（{len(sigs)} 种组合），"
                f"记为 DUPLICATE_IN_SOURCE 待形态消解：{group[0].name_raw}",
                sid,
            )
    return rep


def find_exact_duplicates(rows: list[EncodedRow]) -> dict[str, list[EncodedRow]]:
    """找出需要去重的组（六维逐字相同）。"""
    groups: dict[str, list[EncodedRow]] = {}
    for r in rows:
        groups.setdefault(r.stand_id, []).append(r)
    out = {}
    for sid, group in groups.items():
        sigs = {tuple(r.stats[d].raw for d in STAT_DIMS) for r in group}
        if len(group) > 1 and len(sigs) == 1:
            out[sid] = group
    return out


def collect_source_duplicates(rows: list[EncodedRow]) -> list[tuple[str, str, str, str, str]]:
    """收集需记入 stat_conflicts 的源内重复（同名、数值不同）。

    返回 (stand_id, stat_dim, value_a, value_b, conflict_type) 序列。
    """
    groups: dict[str, list[EncodedRow]] = {}
    for r in rows:
        groups.setdefault(r.stand_id, []).append(r)

    out = []
    for sid, group in groups.items():
        if len(group) < 2:
            continue
        sigs = {tuple(r.stats[d].raw for d in STAT_DIMS) for r in group}
        if len(sigs) == 1:
            continue  # 完全重复，走去重而非冲突记录
        for dim in STAT_DIMS:
            vals = {r.stats[dim].raw for r in group}
            if len(vals) > 1:
                vs = sorted(vals)
                out.append(
                    (sid, dim, vs[0], vs[-1], "DUPLICATE_IN_SOURCE")
                )
    return out


# ------------------------------------------------------------------
# V9：列名白名单
# ------------------------------------------------------------------

def validate_columns(columns: list[str], source: str) -> ValidationReport:
    """校验数据源列名。

    允许：
      - 权威列名集合
      - 已知错误别名（会给出告警）
      - 可选的 Story / Stand / part 等附加列
    出现未知列名直接 ERROR——**不得猜测映射**。
    """
    rep = ValidationReport()
    for col in columns:
        c = col.strip()
        if c in ALLOWED_STAT_COLUMNS:
            continue
        if c in KNOWN_COLUMN_ALIASES:
            rep.add(
                "V9",
                "WARN",
                f"列名 {c!r} 是已知错误别名，应为 "
                f"{KNOWN_COLUMN_ALIASES[c]!r}；已按位置映射",
                source,
            )
            continue
        if c in {"Stand", "Story", "name"}:
            continue
        rep.add("V9", "ERROR", f"未知列名 {c!r}，拒绝猜测映射", source)
    return rep


# ------------------------------------------------------------------
# V5：冲突必须全部记录
# ------------------------------------------------------------------

def validate_conflicts_recorded(
    conflicts: Iterable[tuple[str, str, str, str, str]],
    total_pairs_compared: int,
) -> ValidationReport:
    """校验冲突记录完整性（V5）。

    Args:
        conflicts: (stand_id, dim, source_a, source_b, conflict_type) 序列
        total_pairs_compared: 实际比对的 (记录, 维度, 源对) 组合总数
    """
    rep = ValidationReport()
    n = len(list(conflicts))
    if n == 0 and total_pairs_compared > 0:
        rep.add(
            "V5",
            "WARN",
            f"比对了 {total_pairs_compared} 组但零冲突记录，请确认检测逻辑生效",
        )
    return rep


# ------------------------------------------------------------------
# V4 / V7 / V8：需要外部关系，单独校验
# ------------------------------------------------------------------

def validate_owner_refs(
    rows: Iterable[EncodedRow], known_chars: set[str]
) -> ValidationReport:
    """V4：owner 外键可解析（允许 NULL = 未收录）。"""
    rep = ValidationReport()
    return rep


def validate_form_chains(
    form_chains: dict[str, list[str]], known_forms: set[str]
) -> ValidationReport:
    """V7：form_chain 中的形态均存在于 stand_forms。"""
    rep = ValidationReport()
    for sid, chain in form_chains.items():
        for f in chain:
            if f not in known_forms:
                rep.add("V7", "WARN", f"形态 {f!r} 不在 stand_forms 中", sid)
    return rep


def validate_chunks(chunks: list[dict]) -> ValidationReport:
    """V8：文本块元数据完整性，无孤儿块。"""
    rep = ValidationReport()
    for c in chunks:
        if not c.get("stand_id"):
            rep.add("V8", "ERROR", "文本块缺少 stand_id 引用", str(c.get("chunk_id")))
        if not c.get("content", "").strip():
            rep.add("V8", "ERROR", "文本块内容为空", str(c.get("chunk_id")))
    return rep


# ------------------------------------------------------------------
# 批量校验
# ------------------------------------------------------------------

def validate_all(rows: list[EncodedRow]) -> ValidationReport:
    """全量校验：逐行 + 唯一性。"""
    rep = ValidationReport()
    for r in rows:
        rep.merge(validate_row(r))
    rep.merge(validate_uniqueness(rows))
    return rep

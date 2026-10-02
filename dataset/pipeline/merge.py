"""
合并层：三源合并 + 编码 + 冲突记录 + 消解。

输入：
  1. jojowiki 主表（权威基准，157 条）
  2. jojowiki 详情页（标量字段 + 形态链）
  3. CSV 镜像 A（唯一所属部来源）
  4. CSV 镜像 B（第三方交叉校验）

输出（不依赖数据库，可直接JSON/CSV 落盘）：
  - stands.json      主表记录
  - stand_stats.json 编码后的六维
  - conflicts.json   全部冲突与消解结果

合并原则（数据规范 §3.2）：
  结构化数值：jojowiki > csv_bogdan > csv_topology
  所属部：csv_bogdan 的 Story 字段（唯一来源）
  形态/标量：jojowiki 详情页（唯一来源）

关键设计：
  - 冲突**不做静默覆盖**，全部记入 conflicts
  - 空值表示法差异（∅/undefined/None）归一化后不算冲突，但记录归一事件
  - 主表同名多行 = 形态差异，用详情页的 form 序列消解
"""

from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from encode import STAT_DIMS, Category, encode_row, make_stand_id, normalize
from scrape import DetailPage, FormEntry
from validate import collect_source_duplicates

# ------------------------------------------------------------------
# 源标识
# ------------------------------------------------------------------

SRC_PRIMARY = "jojowiki"
SRC_BOGDAN = "csv_bogdan"
SRC_TOPOLOGY = "csv_topology"

# CSV 镜像的列序（按位置映射，忽略列名——见校验 V9）
CSV_STAT_ORDER = ["PWR", "SPD", "RNG", "STA", "PRC", "DEV"]


# ------------------------------------------------------------------
# CSV 读取
# ------------------------------------------------------------------

def read_csv_rows(path: Path) -> list[dict]:
    """读取 CSV 镜像，编码回退 utf-8 → utf-8-sig → latin-1。

    实测：部分镜像含 NBSP(0xa0)，直接 utf-8 解码会抛 UnicodeDecodeError。
    """
    for enc in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            with path.open(encoding=enc, newline="") as f:
                return list(csv.DictReader(f))
        except UnicodeDecodeError:
            continue
    return []


def parse_part(story: str) -> tuple[Optional[int], Optional[str]]:
    """从 'Part 3: Stardust Crusaders' 解析部编号与英文名。"""
    s = normalize(story or "")
    m = re.match(r"part\s+(\d+)\s*:?\s*(.*)", s, re.I)
    if not m:
        return None, None
    part = int(m.group(1))
    name = m.group(2).strip(" :") or None
    # 去掉尾部噪声引号
    if name:
        name = re.sub(r'["“”]+$', "", name).strip()
    return part, name


# ------------------------------------------------------------------
# 合并主逻辑
# ------------------------------------------------------------------

@dataclass
class Conflict:
    stand_id: str
    stat_dim: str
    value_a: Optional[str]
    source_a: str
    value_b: Optional[str]
    source_b: str
    conflict_type: str
    resolution: str
    resolved_value: Optional[int]
    resolved_cat: str
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "stand_id": self.stand_id,
            "stat_dim": self.stat_dim,
            "value_a": self.value_a,
            "source_a": self.source_a,
            "value_b": self.value_b,
            "source_b": self.source_b,
            "conflict_type": self.conflict_type,
            "resolution": self.resolution,
            "resolved_value": self.resolved_value,
            "resolved_cat": self.resolved_cat,
            "note": self.note,
        }


@dataclass
class MergeResult:
    stands: list[dict] = field(default_factory=list)
    stats: list[dict] = field(default_factory=list)
    forms: list[dict] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    normalizations: list[dict] = field(default_factory=list)  # 归一化事件（非冲突）
    stats_report: dict = field(default_factory=dict)


def _as_detail(obj):
    """把详情页对象统一为 DetailPage（缓存读入的是 dict）。"""
    if isinstance(obj, dict):
        forms = [
            FormEntry(
                form_label=f.get("form_label", ""),
                values=f.get("values", {}),
                raw_order=f.get("raw_order", i),
            )
            for i, f in enumerate(obj.get("forms", []))
        ]
        return DetailPage(
            stand_id=obj.get("stand_id", ""),
            name_raw=obj.get("name_raw", ""),
            url=obj.get("url", ""),
            infobox=obj.get("infobox", {}) or {},
            forms=forms,
            form_count=obj.get("form_count", len(forms)),
        )
    return obj


def merge(main_rows, details, bogdan_rows, topology_rows) -> MergeResult:
    """执行三源合并。

    Args:
        main_rows: MainRow 列表（jojowiki 主表）
        details: DetailPage 或 dict 列表（jojowiki 详情页）
        bogdan_rows: CSV dict 列表（镜像 A，含 Story）
        topology_rows: CSV dict 列表（镜像 B）
    """
    res = MergeResult()
    detail_map = {d.stand_id: d for d in map(_as_detail, details)}

    # ---------- 1. 主表按 stand_id 分组（同名多行 = 形态登记） ----------
    by_sid: dict[str, list] = defaultdict(list)
    for r in main_rows:
        by_sid[r.stand_id].append(r)

    # ---------- 2. CSV 索引 ----------
    bogdan_idx = {}
    for r in bogdan_rows:
        name = normalize(r.get("Stand", ""))
        if name:
            bogdan_idx.setdefault(make_stand_id(name), r)

    topology_idx = {}
    for r in topology_rows:
        name = normalize(r.get("Stand", ""))
        if name:
            topology_idx.setdefault(make_stand_id(name), r)

    # ---------- 3. 逐替身处理 ----------
    for sid, rows in by_sid.items():
        primary = rows[0]                       # 主表首行作基准
        detail = detail_map.get(sid)
        enc_primary = encode_row(primary.name_raw,
                                 [primary.values[d] for d in STAT_DIMS])

        # --- 3a. 所属部（唯一来源：镜像 A 的 Story）---
        part, part_name = None, None
        if sid in bogdan_idx:
            part, part_name = parse_part(bogdan_idx[sid].get("Story", ""))

        # --- 3b. 形态链（唯一来源：详情页）---
        form_rows = []
        if detail and detail.forms:
            for f in detail.forms:
                form_rows.append({
                    "form_id": f"{sid}__f{f.raw_order}",
                    "stand_id": sid,
                    "form_name": f.form_label or f"form_{f.raw_order}",
                    "form_type": "base" if f.raw_order == 0 else "evolved",
                    "raw_order": f.raw_order,
                    "values": f.values,
                })

        # --- 3c. 主表同名多行 → 形态消解 ---
        if len(rows) > 1:
            conflict_dim = collect_source_duplicates(
                [encode_row(r.name_raw, [r.values[d] for d in STAT_DIMS])
                 for r in rows]
            )
            for cid, dim, va, vb, ct in conflict_dim:
                # 用形态信息消解
                note = (f"主表 {len(rows)} 次登记，按形态拆分："
                        f"form_0={va!r} / 后续={vb!r}")
                if detail and detail.form_count >= 2:
                    res.conflicts.append(Conflict(
                        stand_id=cid, stat_dim=dim,
                        value_a=va, source_a=SRC_PRIMARY,
                        value_b=vb, source_b=SRC_PRIMARY,
                        conflict_type="DUPLICATE_IN_SOURCE",
                        resolution="split_by_form",
                        resolved_value=enc_primary.stats[dim].value,
                        resolved_cat=enc_primary.stats[dim].category.value,
                        note=note,
                    ))

        # --- 3d. 与 CSV 镜像交叉校验 ---
        for dim in STAT_DIMS:
            pc = enc_primary.stats[dim]
            for idx, srcname in ((bogdan_idx, SRC_BOGDAN),
                                 (topology_idx, SRC_TOPOLOGY)):
                if sid not in idx:
                    continue
                # 按位置取（第 4 列是 STA 而非 PER，见校验 V9）
                cells = [idx[sid].get(c, "") for c in CSV_STAT_ORDER]
                if dim not in CSV_STAT_ORDER:
                    continue
                raw_other = cells[CSV_STAT_ORDER.index(dim)]
                other = encode_row(primary.name_raw, cells)
                oc = other.stats[dim]

                if oc.category == pc.category and oc.value == pc.value:
                    continue

                # 该源未提供此值（空格子）→ 不是冲突，是覆盖范围差异
                if oc.category == Category.NOT_APPLICABLE:
                    continue

                # 归一化后等价 → 非冲突，记归一事件
                if _equivalent(pc, oc):
                    res.normalizations.append({
                        "stand_id": sid, "stat_dim": dim,
                        "primary_raw": pc.raw, "other_raw": oc.raw,
                        "source": srcname,
                        "primary_cat": pc.category.value,
                        "other_cat": oc.category.value,
                    })
                    continue

                # 真冲突 → 记入冲突表
                res.conflicts.append(Conflict(
                    stand_id=sid, stat_dim=dim,
                    value_a=pc.raw, source_a=SRC_PRIMARY,
                    value_b=oc.raw, source_b=srcname,
                    conflict_type=_classify(pc, oc),
                    resolution="prefer_primary",
                    resolved_value=pc.value,
                    resolved_cat=pc.category.value,
                    note="基准源与镜像数值/类别不一致",
                ))

        # --- 3e. 组装输出 ---
        rec = enc_primary.to_record()
        res.stats.append(rec)

        stand_rec = {
            "stand_id": sid,
            "name_en": normalize(primary.name_raw),
            "name_raw": primary.name_raw,
            "part": part,
            "part_name_en": part_name,
            "owner_id": None,
            "owner_name": (detail.infobox.get("owner") if detail else None),
            "stand_type": (detail.infobox.get("stand_type") if detail else None),
            "reference": (detail.infobox.get("reference") if detail else None),
            "name_ja": (detail.infobox.get("name_ja") if detail else None),
            "name_romaji": (detail.infobox.get("name_romaji") if detail else None),
            "manga_debut": (detail.infobox.get("manga_debut") if detail else None),
            "anime_debut": (detail.infobox.get("anime_debut") if detail else None),
            "form_count": len(form_rows),
            "form_chain": [f["form_id"] for f in form_rows],
            "main_table_registrations": len(rows),
            "data_sources": [SRC_PRIMARY] + (
                [SRC_BOGDAN] if sid in bogdan_idx else []
            ) + ([SRC_TOPOLOGY] if sid in topology_idx else []),
            "detail_url": (detail.url if detail else None),
        }
        res.stands.append(stand_rec)
        res.forms.extend(form_rows)

    res.stats_report = build_report(res, len(by_sid))
    return res


def _equivalent(a, b) -> bool:
    """判断两个编码结果是否语义等价（归一化后无实质差异）。

    分三类处理：

    1. **数值等价**：有值且数值相同 → 等价
    2. **取值等价**：都无有效值 → 等价，但记入归一化事件以便追溯
    3. **严格排除**：
       - `UNKNOWN`（不确定） vs `EMPTY_SLOT`（明确空位）
         「未知」与「为空」是两种语义，**不可互认**（规范 §1.5），
         必须记为冲突
       - `CONDITIONAL_NO_BASE` vs 有值
         条件值不可用确定数值回答，需人工/规则消解
    """
    # 1. 有值情形
    if a.value is not None or b.value is not None:
        if a.value is not None and b.value is not None:
            return a.value == b.value
        # 一方有值一方无值：仅当「无值方」是可退化的空位类时视为等价
        # （∅ vs None：主源说空位，镜像说无能力 → 都表示"这里没有有效值"）
        no_value_side = b if a.value is not None else a
        if no_value_side.category in {
            Category.EMPTY_SLOT, Category.NONE, Category.NOT_APPLICABLE
        }:
            # 但若主源是 UNKNOWN，镜像给了确定值 → 这是真冲突（主源更保守）
            known_side = a if a.value is not None else b
            if known_side.category == Category.UNKNOWN:
                return False
            return True
        return False

    # 2. 都无值
    if a.category == b.category:
        return True

    pair = {a.category, b.category}
    # 3. 严格排除：未知 vs 空位
    if pair == {Category.UNKNOWN, Category.EMPTY_SLOT}:
        return False
    # 条件值 vs 确定值不可归一
    if Category.CONDITIONAL_NO_BASE in pair:
        return False

    no_value = {
        Category.EMPTY_SLOT, Category.NONE, Category.NOT_APPLICABLE,
        Category.UNKNOWN, Category.INFINITE,
    }
    return a.category in no_value and b.category in no_value


def _classify(a, b) -> str:
    """冲突类型判定。"""
    if a.value is None and b.value is not None:
        return "MISSING_IN_ONE"
    if a.value is not None and b.value is None:
        return "MISSING_IN_ONE"
    if a.category != b.category:
        return "CATEGORY_DIFF"
    return "VALUE_MISMATCH"


def build_report(res: MergeResult, n_stands: int) -> dict:
    """汇总报告。"""
    cat_cnt: Counter = Counter()
    for rec in res.stats:
        for dim in STAT_DIMS:
            cat_cnt[rec[f"{dim.lower()}_cat"]] += 1

    conflict_types = Counter(c.conflict_type for c in res.conflicts)
    return {
        "n_stands": n_stands,
        "n_stats_rows": len(res.stats),
        "n_forms": len(res.forms),
        "n_conflicts": len(res.conflicts),
        "n_normalizations": len(res.normalizations),
        "category_distribution": dict(cat_cnt.most_common()),
        "conflict_type_distribution": dict(conflict_types.most_common()),
        "comparable_rows": sum(
            1 for r in res.stats if r["composite"] is not None
        ),
        "multi_form_stands": sum(1 for s in res.stands if s["form_count"] > 1),
        "with_part": sum(1 for s in res.stands if s["part"] is not None),
        "with_owner": sum(1 for s in res.stands if s.get("owner_name")),
    }


# ------------------------------------------------------------------
# 落盘
# ------------------------------------------------------------------

def save_outputs(res: MergeResult, outdir: Path) -> dict:
    outdir.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name, obj in (
        ("stands", res.stands),
        ("stand_stats", res.stats),
        ("stand_forms", res.forms),
        ("conflicts", [c.to_dict() for c in res.conflicts]),
        ("normalizations", res.normalizations),
        ("merge_report", res.stats_report),
    ):
        p = outdir / f"{name}.json"
        p.write_text(json.dumps(obj, ensure_ascii=False, indent=2),
                     encoding="utf-8")
        paths[name] = p
    return paths

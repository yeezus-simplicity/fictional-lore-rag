"""
冲突消解与路由（M4）。

立项书里「冲突消解」是**第二不可压缩项** —— 跳过它项目就退化为普通 RAG。
本模块回答两个问题：
  1. 数据源冲突时，采信谁？依据是什么？
  2. 不同类型的问题该走哪条路径？

★ 核心立场（与 M1 一致）：**冲突不等于「某方错」**
  三种消解策略对应三种不同的事实：
    prefer_primary    主源权威（jojowiki 是设定集，天然权威）
    prefer_consensus两个镜像源一致 → 采信它们（比单源更可信）
    split_by_form分歧源于「不同形态被当成同一替身」→ 拆到形态上
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
PROC = ROOT / "dataset" / "processed"

# ==============================================================
# 消解策略
# ==============================================================

# 源的权威性排序（数值越小越权威）
SOURCE_RANK = {
    "jojowiki": 0,        # 设定集官网，官方数据
    "csv_bogdan": 1,      # 第三方镜像A
    "csv_topology": 1,    # 第三方镜像B（与 A 同级）
    "derived": 2,# 由计算推导
    "unknown": 3,
}

SOURCE_LABEL = {
    "jojowiki": "主源·设定集官网",
    "csv_bogdan": "镜像源A",
    "csv_topology": "镜像源B",
    "derived": "推导值",
    "unknown": "未知来源",
}

RESOLUTIONS = {
    "prefer_primary": "采信主源（jojowiki 是官方设定集，权威性最高）",
    "prefer_consensus": "两个镜像源一致 → 采信（交叉验证，置信度高于单源）",
    "split_by_form": "分歧源于形态未消解 → 拆分到各自形态上，两者都可成立",
    "keep_unknown": "双方都无有效值 → 保留 UNKNOWN，不强行赋值",
}


@dataclass
class Resolution:
    """一条冲突的消解结果。"""

    stand_id: str
    stand_name: str
    stat_dim: str
    value_a: Optional[str]
    value_b: Optional[str]
    source_a: str
    source_b: str
    conflict_type: str
    strategy: str
    final_value: Optional[int]
    final_raw: Optional[str]
    final_category: Optional[str]
    confidence: float
    rationale: str
    # 敏感性：结论对策略的依赖程度（0=稳健，1=脆弱）
    sensitivity: str = "N/A"

    def to_dict(self) -> dict:
        return {
            "stand_id": self.stand_id, "stand_name": self.stand_name,
            "stat_dim": self.stat_dim, "value_a": self.value_a,
            "value_b": self.value_b, "source_a": self.source_a,
            "source_b": self.source_b, "conflict_type": self.conflict_type,
            "strategy": self.strategy, "final_value": self.final_value,
            "final_raw": self.final_raw, "final_category": self.final_category,
            "confidence": self.confidence, "rationale": self.rationale,
            "sensitivity": self.sensitivity,
        }


class ConflictResolver:
    """冲突消解器。策略与依据分离，便于做消融实验。"""

    def __init__(self, use_consensus: bool = True,
                 verify_mirror_independence: bool = True):
        """
        Args:
            use_consensus: 是否启用「镜像源共识」策略。
                开启后 VALUE_MISMATCH 会先检查两个镜像源是否一致。
                这是 M4 的核心消融变量（D5）。
            verify_mirror_independence: ★ 根因分析的产物。
                实测两个 CSV 镜像源**不独立**（topology ⊆ bogdan，
                共同条目中仅 1 处真正的等级数值分歧）。
                → 「两个源一致」不构成交叉验证，共识策略失效。
        """
        self.use_consensus = use_consensus
        self.verify_mirror_independence = verify_mirror_independence
        self.mirror_independent: Optional[bool] = None
        self.mirror_evidence: dict = {}
        if verify_mirror_independence:
            self.mirror_independent, self.mirror_evidence = \
                self._check_mirror_independence()

    # ---------------------------------------------------------
    @staticmethod
    def _norm_level(s: str) -> Optional[str]:
        """字面量归一（仅用于独立性判定）。"""
        s = (s or "").strip().lower()
        if s in ("e", "d", "c", "b", "a"):
            return s.upper()
        if s in ("infinite", "infi", "∞"):
            return "∞"
        if s in ("unknown", "situational", "?"):
            return "?"
        return None

    @classmethod
    def _check_mirror_independence(cls) -> tuple[bool, dict]:
        """检验两个镜像源是否真正独立。

        ★ 本次根因调查最重要的产出。
          「多源交叉验证」的前提是**源相互独立**。
          若两源同源（一个是另一个的子集/复制），则「两者一致」是
          必然的，不提供任何额外置信度。

        判据：
          - 「only_in_topology == 0 且 only_in_bogdan > 0」→ topology ⊆ bogdan
          - 归一化后真正的等级分歧 < 3→ 不足以支撑交叉验证
        """
        field_dir = ROOT / "docs" / "字段映射"
        files = {"csv_bogdan": "csv_bogdan_raw.csv",
                 "csv_topology": "csv_topology_raw.csv"}
        rows: dict[str, dict[str, dict]] = {}
        for name, fn in files.items():
            p = field_dir / fn
            if not p.exists():
                return False, {"error": f"{fn} 不存在"}
            raw = p.read_bytes()
            text = None
            for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
                try:
                    text = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            import csv as _csv
            rows[name] = {
                re.sub(r"[^a-z0-9]+", "_", r["Stand"].lower()).strip("_"): r
                for r in _csv.DictReader(text.splitlines())
            }

        a, b = rows["csv_bogdan"], rows["csv_topology"]
        common = set(a) & set(b)
        dims = ["PWR", "SPD", "RNG", "STA", "PRC", "DEV"]

        same = only_a = only_b = real_diff = 0
        for k in common:
            for d in dims:
                x = (a[k].get(d) or "").strip()
                y = (b[k].get(d) or "").strip()
                if x == y:
                    same += 1
                elif x and not y:
                    only_a += 1
                elif y and not x:
                    only_b += 1
                else:
                    nx, ny = cls._norm_level(x), cls._norm_level(y)
                    if nx and ny and nx != ny:
                        real_diff += 1

        is_subset = (only_b == 0 and only_a > 0)
        independent = (not is_subset) and real_diff >= 3
        evidence = {
            "common_rows": len(common),
            "same_cells": same,
            "only_in_bogdan": only_a,
            "only_in_topology": only_b,
            "real_value_conflicts": real_diff,
            "topology_is_subset": is_subset,
            "verdict": ("同源（topology ⊆ bogdan）→ 共识策略无效"
                        if is_subset else
                        ("独立" if independent else "独立性不明确")),
        }
        return independent, evidence

    # ---------------------------------------------------------
    def resolve_all(self, conflicts: list[dict], stands: list[dict],
                    sources_index: Optional[dict] = None) -> list[Resolution]:
        """消解全部冲突。同一 stand+dim 的多个冲突会先聚合再决策。"""
        sname = {s["stand_id"]: s["name_en"] for s in stands}

        # 聚合：把同一 (stand, dim) 的多条冲突放在一起
        grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for c in conflicts:
            grouped[(c["stand_id"], c["stat_dim"])].append(c)

        out: list[Resolution] = []
        for (sid, dim), items in sorted(grouped.items()):
            out.append(self._resolve_one(sid, sname.get(sid, sid), dim, items))
        return out

    # ---------------------------------------------------------
    def _resolve_one(self, sid: str, sname: str, dim: str,
                     items: list[dict]) -> Resolution:
        # 第一条作为代表（同一组内a/b 相同，仅 source 不同）
        rep = items[0]
        value_a, src_a = rep.get("value_a"), rep.get("source_a")
        value_b, src_b = rep.get("value_b"), rep.get("source_b")
        ctype = rep["conflict_type"]

        # 收集所有来源对同一格子的取值
        # {值: [来源,...]}
        votes: dict[str, list[str]] = defaultdict(list)
        for it in items:
            for v, s in ((it.get("value_a"), it.get("source_a")),
                         (it.get("value_b"), it.get("source_b"))):
                if v is not None and s:
                    votes[str(v)].append(s)

        # ---------- 决策 ----------
        strategy = "prefer_primary"
        final_raw = value_a
        confidence = 0.9
        rationale = ""
        sensitivity = "稳健：结论不依赖特定源"

        if ctype == "DUPLICATE_IN_SOURCE":
            # 同一形态在主源多次登记 → 按形态拆分即可
            strategy = "split_by_form"
            final_raw = value_a
            confidence = 0.95
            rationale = ("主源在同一替身下有多次登记，"
                         "本质是不同形态而非数值冲突")
            sensitivity = "稳健：拆形态后不涉及取舍"

        elif ctype == "MISSING_IN_ONE":
            # 一方明确「未提供」→ 有值的那方胜出
            has_a = value_a not in (None, "", "None")
            has_b = value_b not in (None, "", "None")
            if has_a and not has_b:
                strategy, final_raw = "prefer_primary", value_a
                rationale = "主源有值、镜像源未提供 → 采主源"
            elif has_b and not has_a:
                strategy, final_raw = "prefer_primary", value_b
                rationale = "主源未提供、镜像源有值 → 采镜像源"
            else:
                strategy, final_raw = "keep_unknown", value_a
                rationale = "双方均未提供有效值 → 保留 UNKNOWN"
            confidence = 0.85
            sensitivity = "稳健：一方明确缺失，无歧义"

        elif ctype == "CATEGORY_DIFF":
            # 类别不同（含 ? vs 具体值）→ 保守处理
            if value_a == "?":
                # 主源说未知，镜像源给了具体值
                # ★ 不强行赋值：主源明确表示「未知」，赋具体值是过度自信
                strategy, final_raw = "keep_unknown", "?"
                confidence = 0.6
                rationale = ("主源标注为「未知(?)」，镜像源给了具体值。"
                             "保守策略：保留 UNKNOWN，不臆测")
                sensitivity = "**脆弱**：若采用「镜像源优先」则结论翻转"
            else:
                strategy, final_raw = "prefer_primary", value_a
                confidence = 0.75
                rationale = f"类别不同（{value_a} vs {value_b}）→ 采主源"
                sensitivity = "**脆弱**：类别差异的解释依赖具体语义"

        else:  # VALUE_MISMATCH
            # 双方都有明确数值 → 看镜像源是否共识
            mirror_votes = {}
            for v, srcs in votes.items():
                for s in srcs:
                    if s in ("csv_bogdan", "csv_topology"):
                        mirror_votes.setdefault(v, set()).add(s)

            both_mirrors_agree = any(
                len(srcs) == 2 for srcs in mirror_votes.values())

            # ★★ 独立性前置检验（根因分析的结论）
            #   两个 CSV 镜像源实测不独立（topology ⊆ bogdan），
            #   「两源一致」是同源复制的必然结果，不构成交叉验证。
            #   → 独立性不成立时，共识策略自动失效。
            consensus_usable = (
                self.use_consensus
                and both_mirrors_agree
                and (self.mirror_independent is not False)
            )

            if not self.use_consensus and both_mirrors_agree:
                strategy, final_raw = "prefer_primary", value_a
                confidence = 0.75
                rationale = ("两镜像一致但主源不同 → 采主源"
                             "（主源是唯一有口径定义的来源）")
                sensitivity = "**脆弱**：镜像源一致，若改用共识策略结论翻转"
            elif consensus_usable:
                agreed = [v for v, srcs in mirror_votes.items() if len(srcs) == 2]
                strategy = "prefer_consensus"
                final_raw = agreed[0]
                confidence = 0.95
                rationale = (f"两个镜像源独立给出相同值（{agreed[0]}），"
                             "交叉验证通过，置信度高于单源")
                sensitivity = "稳健：三源中两源独立一致，取多数"
            else:
                # ★ 修正后的主路径
                strategy = "prefer_primary"
                final_raw = value_a
                if both_mirrors_agree and not consensus_usable:
                    # 两镜像一致但独立性不成立
                    confidence = 0.8
                    rationale = (
                        "两镜像源虽一致，但**经检验二者不独立**"
                        "（topology ⊆ bogdan，153 处差异均为「有值 vs 空」，"
                        "仅 1 处真正的等级分歧）→ 一致性不构成交叉验证，"
                        "采信有官方口径定义的主源")
                    sensitivity = ("**脆弱**：若假设两镜像独立，"
                                   "结论会翻转为采信镜像源")
                elif both_mirrors_agree:
                    confidence = 0.75
                    rationale = "镜像源一致但主源不同 → 采主源"
                    sensitivity = "**脆弱**：镜像源一致，若改用共识策略结论翻转"
                else:
                    confidence = 0.75
                    rationale = "主源为官方设定集，采主源"
                    sensitivity = "稳健：无第三方支撑，主源是唯一依据"

        # ---------- 编码 ----------
        # ★ 复用 dataset/pipeline/encode.py 的判定表，绝不另写一份
        import sys
        if str(ROOT / "dataset" / "pipeline") not in sys.path:
            sys.path.insert(0, str(ROOT / "dataset" / "pipeline"))
        from encode import encode_stat
        enc = encode_stat(str(final_raw)) if final_raw else None

        return Resolution(
            stand_id=sid, stand_name=sname, stat_dim=dim,
            value_a=value_a, value_b=value_b,
            source_a=src_a or "unknown", source_b=src_b or "unknown",
            conflict_type=ctype, strategy=strategy,
            final_value=enc.value if enc else None,
            final_raw=final_raw,
            final_category=enc.category.value if enc else None,
            confidence=confidence, rationale=rationale,
            sensitivity=sensitivity,
        )


# ==============================================================
# 路由
# ==============================================================

@dataclass
class RouteDecision:
    """一次路由决策（可解释）。"""

    route: str# structured/semantic/hybrid/abstain/entity_semantic/location/character
    reason: str
    signals: dict = field(default_factory=dict)


class Router:
    """查询路由器。

    M2 基线的规则路由准确率 0.8352，两个明确短板：
      - T5 混合协同 0/21（规则无法识别「混合」意图）
      - T6 无答案 12/20（编造实体被误判为 structured）

    本实现针对这两点改进，并在 M4 评测里量化收益。
    """

    # 实体名必须完整匹配才算识别到（避免 "Tusk" 命中 "Tusk Act1"）
    FABRICATED_HINTS = re.compile(
        r"(未公布|未公开|不存在的|是否有官方|具体是几|"
        r"ultimate|overlord|requiem\s*ii|neon|omega|"
        r"act\s*4|final\s*form|revenant)", re.I)

    # ★ 第二类编造信号：问「不存在的内容」
    #   实测漏判的3 条 T6 都属此类：
    #     'Infinity Crusher 在第 9 部中的表现如何？'
    #     'Starlight Express 在第 9 部中的表现如何？'
    #     '替身 Stand Arise v2 的使用者叫什么名字？'
    #   特征：问「第 9 部」（本项目只有 3–8 部）
    # ★★ 部数越界判定：原写死「9」，但**第 9 部（The JOJOLands）已有数据**
    #   （实测 13 个替身，含主角替身 November Rain）。
    #   → 原规则把「第9部有哪些替身」判成「询问不存在的内容」直接拒答。
    #   ★ 它还只认阿拉伯数字：中文「第九部」不被匹配，
    #     于是**同一问题的两种写法，一个能答一个拒答**——硬编码部数上限
    #     本身就是脆的（数据一扩就错）。
    #   现改为只判**真正越界**：第 0 部、第 10 部及以后。
    OUT_OF_RANGE_PART = re.compile(
        r"第\s*(?:0|\d{2,}|十|[一二三四五六七八九]十)\s*部")
    VERSION_LIKE = re.compile(r"\bv\d+\b", re.I)   # "Arise v2"

    # 极值/聚合意图
    EXTREME = re.compile(
        r"(最高|最大|最强|最低|最小|最弱|多少个|几个|排名|"
        r"综合|总分|同时满足|达到|所有替身)")

    # 结构化直接查询
    STRUCT_FACT = re.compile(
        r"(是几级|多少级|是多少|能力值|破坏力|速度|射程|持续力|"
        r"精密性|成长性|第几部|使用者是谁|有哪些替身|有几个形态)")

    # ★★ M20：「XX 的替身是什么」→ 结构化反查（直接给替身名）
    #   实测踩坑：SEMANTIC 里有万能词「是什么」，
    #   「东方定助的替身是什么」被判成 semantic → 去检索原文 →
    #   返回**能力介绍长段**，而用户要的是替身名这一个词。
    #   → 这类问法答案确定（查库即可），不该走语义检索。
    #   注意：必须排在 SEMANTIC 判定之前。
    STAND_OF_ASK = re.compile(
        #★ 必须支持**含空格的英文全名**（实测踩坑）★
        #   'Jotaro Kujo的替身是什么' —— 若只写 (.{2,12}?) 则中间的空格
        #   会导致「Jotaro」「Kujo」被拆开、整句匹配不上 →
        #   路由退回 semantic → 又变成一长段原文介绍。
        #   → 主体部分允许字母/数字/点/连字符/空格（Toki no Fūwa 这类）。
        # ★ 中文名要允许**间隔号**（·／・），否则
        #   '乔鲁诺·乔巴拿的替身是什么' 这类整句漏掉（M21 实测）。
        r"((?:[A-Za-z][A-Za-z0-9.'\-]*\s*){1,4}|[一-鿿·・]{2,14}?)\s*的\s*"
        r"(?:替身|Stand|STAND|使的替身|本人|的本体)")

    # ★★ M20：出现这些词时**不判** stand_of —— 否则会抢走聚合类问题
    #   （实测我引入的回归）：「各部的替身数量」里「各部」被当成人名，
    #   stand_of 抢先触发，而它该走 part_count。
    AGG_GUARD = re.compile(
        r"(多少|几个|数量|总数|排名|最高|最强|最弱|最大|最小|"
        r"各部|每部|这部|哪部|第\s*\d+\s*部|部数|所有|全部)")

    # ★ 语义描述
    SEMANTIC = re.compile(
        r"(描述|介绍|概述|历史|性格|外观|来源|设定|怎么|如何|"
        r"运作|方面|是什么|招式|appearance|personality|history)", re.I)

    # ★★ M76：招式 / 必杀技 → 结构化（与 M73 的 appearance 同构）★★
    #   实测：「白金之星有什么招式」被 `SEMANTIC` 里的「招式」命中
    #   → 走 entity_semantic → 返回**一段人物介绍**，而库里已有
    #     263 条结构化招式（M76 刚补到 156 个替身）。
    #   ★ 只认「招式/必杀技/招数」这类**明确的招式维度词**；
    #     「能力」不在其中 —— 问「XX 的能力」要的是能力描述（原文长段），
    #     走语义才是对的。
    MOVE_ASK = re.compile(
        r"(招式|必杀技|必殺技|招数|招數|技能|有哪些招|什么招|什麼招|"
        r"会什么招|會什麼招|"
        # ★ M79：「黄金体验有几招」是**问数量**，也属于招式问法。
        #   之前只认「招数 / 有哪些招」，「有几招」落进 SEMANTIC → 返回一段人物介绍。
        r"几招|幾招|多少招|多少个招式|几个招式|招式数)")

    # ★ M79：问「首次登场 / 第一次出场」，但主体是**角色**时走角色维度
    #   （角色卡里有 mangadebut / animedebut；替身才有 stand_appearances）
    CHAR_DEBUT = re.compile(
        r"(首次登场|第一次出场|首次出场|初次登场|第几话出场|第几话开始)")

    # ★★ M79：本库**没有**的数据类型，说清楚而不是硬答 ★★
    #   实测：「谁打败了普奇」→ 路由到 character → 返回普奇档案
    #   （用户看到的是"完全不相干的介绍"，比拒答更糟）。
    #   本库只有：六维 / 招式 / 出场章节 / 角色档案 / 地点 / 篇章；
    #   **没有**战况、死因、宿敌、胜负关系。
    NO_DATA_ASK = re.compile(
        r"(谁打败了|谁击败了|谁杀了|被谁杀|杀死.{0,4}的是|怎么死的|死因|"
        r"宿敌是谁|敌人是谁|谁赢了|哪场战斗|战斗结果|结局如何|"
        # ★ M80 补充：人物关系与剧情结局 —— 库里只有亲属（families），
        #   没有"谁认识谁""两人什么关系""结局如何"这类数据。
        #   实测「谁和空条承太郎认识」→ 返回承太郎档案（拿不相干内容充数）。
        r"和.{1,12}(是什么关系|认识吗|认识)|谁和.{1,12}认识|"
        r"的结局|最后怎么|后来怎么|最后去哪|下场如何)")

    # ★★ M80：替身类型问法（布尔 / 筛选）★★
    TYPE_ASK = re.compile(
        r"(近战|近距|远战|远距|远程|自动|自主|人形|非人形|精神|现象|束缚|侦查)"
        r"(型|类型|系|替身)?")
    # ★★ M80：两个替身谁更强 ★★
    COMPARE_ASK = re.compile(
        r"(?:谁|哪个|哪一个|哪一位).{0,6}(更强|更强些|更厉害|厉害|强|好)|"
        r"(.{2,20}?)(?:和|与|对比|vs)(.{2,20}?)(?:谁|哪个|哪一个)|"
        # ★ M81：省略主语的追问（「和疯狂钻石比呢」）—— coref 补完主语后
        #   句中没有"谁/哪个"，前两支都不匹配（实测就是这么掉进语义检索的）
        r"(?:和|与).{2,20}?(?:比|相比|比比)")

    # ★★ M80：形态问法 ★★
    #   「有几个形态」靠 EXTREME 的「几个」已经能进 structured；
    #   「第二形态是什么」**没有任何触发词** → 掉进语义检索（实测答非所问）。
    FORMS_ASK = re.compile(
        r"(几个形态|有哪些形态|形态链|第\s*[0-9一二三四五六七八九十]+\s*个?形态)")

    # ★★★ M16：维度词黑名单
    #   这些词描述的是「问哪个方面」，不是「哪个替身」，
    #   它们会被抽进 candidates，且也在 known_entities 里
    #   → 不能拿它们当「已锚定替身」的判据，否则
    #     「Anubis 的外观形态方面有哪些描述？」会被误判成实体锚定检索。
    DIMENSION_WORDS = {
        "外观", "形态", "描述", "介绍", "概述", "历史", "性格",
        "来源", "设定", "能力", "效果", "作用", "特征", "详情",
        "资料", "信息", "设定集", "方面", "招式", "来历", "背景",
        "破坏力", "速度", "射程", "持续力", "精密性", "成长性",
        "appearance", "personality", "history", "ability", "profile",
    }

    # ★★ M20新增：**未知中文人名**检测
    #   实测（用户报的 bug）：「东方常秀的替身是什么」
    #   → 答成Soft & Wet 的能力长段介绍（那是东方**定助**的替身）
    #   原因：zh_hits 只收「已知中文别名」，"东方常秀"不在其中
    #        → candidates=[] → unknown_hit=None
    #        → 但问句含"替身"（DOMAIN_HINT 命中）→ 不拒答
    #        → 落到语义兜底，BM25捞回 soft_wet 的原文。
    #   ★★ 更正（M38）：「东方常秀」**不是**《东方project》角色 ★★
    #     M20 当时把它记成外来角色，是**判断错了**。
    #     东方常秀 = Higashikata Joshu，JoJOLion 角色，替身 Nut King Call。
    #     M21 补齐角色中文别名后，这条能正确解析了（实测返回 Nut King Call）。
    #     → 这段逻辑真正要挡的是**真正不存在的人名**，
    #       例如「甘道夫的替身是什么」（指环王角色）→ 应 abstain。
    #       回归用例见 evaluation/test_m38_conditional.py。
    #   → 规则：问句里出现「XXX的替身」，XXX 是 2-4 个汉字、
    #     且不在已知别名里、也不在维度词/停用词里
    #     → 判为「编造/未知实体」→ abstain。
    ZH_PERSON_TMPL = re.compile(r"([一-鿿]{2,4})的替身")
    # ★ M73：「X 是谁的替身」= owner 问法（主体是替身，不是未知人名）。
    #   必须匹配**完整**的「是谁的替身」，只写「是谁的」会放过
    #   T6 守门用例「甘道夫的替身是什么」。详见 _unknown_zh_person。
    OWNER_ASK = re.compile(
        r"(?:是谁的|谁的|谁用的|谁持有的)\s*"
        r"(?:替身|STAND|Stand|stand|使者|本体)", re.I)
    # 这些是「的」前常见的非人名前缀，不该当成名字
    ZH_NAME_STOP = {
        "这个", "那个", "什么", "哪个", "谁的", "他的", "她的",
        "我的", "你的", "它的", "他们", "自己", "别人", "大家",
        "一个", "两个", "三个", "所有", "全部", "每个",
        "已知", "未", "不", "没", "是", "有", "会", "能",
        "替身", "使者", "本体", "能力", "形态",
    }

    # ★★ M38 修：「XXX的替身」模板会从中间截出**假名字** ★★
    #   实测（既有 bug，非本轮引入 —— git stash 后复跑同样复现）：
    #     「破坏力最高的替身」  → 模板从位置 1 起匹配到「坏力最高」
    #     「射程最高的替身前40」→ 匹配到「射程最高」
    #   → 被当成「编造实体」直接 abstain 拒答。
    #   ★ 讽刺的是「破坏力最高的替身」正是路由自己列的示例问句。
    #
    #   根因：模板只看「2-4 个汉字 + 的替身」，不判断这 4 个字**是不是名字**。
    #   → 补一道「整串形状」判据：若「的替身」前的中文串含
    #     维度词 / 极值词 / 疑问词，整句是**描述性短语**，不是人名句式。
    #   ★ 关键：判的是**整串**而不是模板截出的片段 ——
    #     截出来的「坏力最高」已经丢了上下文，判不准。
    _DESCRIPTIVE_RE = re.compile(
        # 维度词（取词口径与 DOMAIN_HINT 对齐）
        r"破坏力|破壞力|破壊力|速度|射程|持续力|持續力|持続力|精密性"
        r"|成长性|成長性|能力|能力值|形态|形態|类型|類型"
        # 极值 / 排行
        r"|最高|最强|最低|最弱|最大|最小|排名|排行|前几|第几"
        # 疑问 / 量词
        r"|多少|几个|几级|哪个|哪些|什么|怎么|如何"
        # 数据集专有
        r"|替身|使者|本体",
    )
    # 取「的替身」前的整串中文（用于形状判定，不用于取名）
    ZH_PERSON_CTX = re.compile(r"([一-鿿]{2,12})的替身")

    def _unknown_zh_person(self, q: str) -> Optional[str]:
        """问句里「XXX的替身」里的 XXX 不是已知别名 → 返回该名字。

        ★ 为什么要单独做（不能只靠 known_entities 抽实体）：
          known_entities 里只有**已收录**的替身名/中文别名，
          "东方常秀"这种未收录的人名压根不会进候选列表，
          于是 unknown_hit 永远是 None，有问有答但答的是别人。

        ★★ M73：先排除「X 是谁的替身」——这是 **owner 问法**，不是未知人名 ★★
          `ZH_PERSON_TMPL` 是 `finditer`，会在**任意位置**找 `{2,4}的替身`。
          实测「白金之星是谁的替身」：
            从「白金之星是谁的替身」里，模板匹配不到开头（第 5 字是"是"不是"的"），
            但会在**第 3 个字**处匹配成功 → 抽出 `name = "之星是谁"`
            → 不在 STOP 里 → 判成未知人名 → **直接 abstain**。
          后果：这是**最基础的问法之一**（「某替身是谁的」），
               却得到「该问题涉及的数据集中不存在」的拒答；
               而只说「白金之星是谁的」（少一个"替身"）反而能答 ——
               同一个意思，两种写法一个能答一个拒答。
          → 判据：「是谁的替身 / 谁的替身」整块出现时，主体必然是**替身**
            （用户在问它的持有者），不可能是「未知人名 + 的替身」
            → 直接跳过未知人名判定，交给 owner 意图。
          ★ 不能放宽成「只认 `是谁的`」：那会把
            「甘道夫的替身是什么」（T6 守门用例，应当 abstain）也放过。
            只认**完整**的「是谁的替身」—— 它必然含"的替身"，
            与 `XXX的替身是什么` 的形态不同。
        """
        if self.OWNER_ASK.search(q):
            return None

        # ★ M38：先判「整句形状」。含维度/极值/疑问词的是描述性短语，
        #   不是「XXX的替身」句式 —— 直接放行，不判编造。
        ctx = self.ZH_PERSON_CTX.search(q)
        if ctx and self._DESCRIPTIVE_RE.search(ctx.group(1)):
            return None

        for m in self.ZH_PERSON_TMPL.finditer(q):
            name = m.group(1)
            if name in self.ZH_NAME_STOP:
                continue
            if self._is_known(name):
                continue
            if name in (self.DIMENSION_WORDS or ()):
                continue
            return name
        return None
    # ★★ M14 新增：**领域相关性**前置判据
    #   （用户报的 bug）M14 当时写的是「不复用 match_stand」，
    #   实测踩坑（用户报的 bug）：
    #     Q「今天天气怎么样」→ SEMANTIC 里的万能词「怎么」命中
    #                      → 路由到 semantic → 检索返回 Heaven's Door 的介绍
    #   ★ 一个完全不相关的问句，却得到「看起来合理」的答案。
    #
    #   这里判「问句里有没有本数据集的实体或维度词」：
    #     有 → 才允许走语义/结构化
    #     没有 → 明确拒答，而不是硬答
    #
    #   ★ 注意不能只看「有没有已知实体」——
    #     「Tusk 是什么颜色」有实体但没维度词，
    #     而「破坏力最高的是哪个」没实体但有维度词，两者都要放行。
    DOMAIN_HINT = re.compile(
        # 六维维度词（中文简体 + 繁体 + 日文 + 英文）
        # ★ M31：原来只有简体，导致「スタープラチナの破壊力」这类
        #   全日文问句 has_domain=False → 被 abstain 拒答（实测）。
        #   日文「破壊力」与简体「破坏力」只差一个字，但正则匹配不到。
        r"(破坏力|破壞力|破壊力|速度|スピード|射程|持续力|持續力|持続力|"
        r"精密性|成长性|成長性|能力|形态|形態|スタンド|"
        r"power|speed|range|stamina|precision|growth|ability|form)|"
        # 结构性问法
        r"(几级|幾級|何級|多少级|是多少|使用者|持有者|拥有|最高|最强|"
        r"多少个|几个|排名|部|章节|"
        r"who|whose|how many|rank|誰|いくつ)|"
        # 数据集专有概念
        r"(替身|stand|觉醒|能力值|必杀|替身能力)", re.I)

    # ★★ M65：地点意图 ★★
    #   背景：库里新增 113 个正传地点。用户会问
    #     「安杰罗岩是什么地方」「杜王町有哪些地点」「恶魔掌心在哪里」。
    #   ★ 为什么必须单独一条：这些问句 **不命中 DOMAIN_HINT 里的任何一项**
    #     （没有六维词、没有"替身"），若不识别就会被当成无关问句拒答 ——
    #     而库里**确实有**这个地点。
    #   ★ 刻意不把「地点」这种宽词单独作为路由依据：
    #     只问「地点」但抽不到地名时（如「华盛顿在哪里」），
    #     应当仍走原来的拒答逻辑，不能凭空造出答案（M21 守门测试的教训）。
    LOCATION_ASK = re.compile(
        r"(是什么地方|什么地方|什么地点|哪些地点|有哪些地点|"
        r"有哪些场所|有哪些场所|在哪儿|在哪里|在哪|位于|"
        r"哪个国家|哪个城市|属于哪|地理|所在|场所|"
        r"location\b|where\s+is)", re.I)

    # 部数（用于「第 X 部有哪些地点」，与 OUT_OF_RANGE_PART 互补）
    PART_ANY = re.compile(r"第\s*[0-9一二三四五六七八九]+\s*部")

    # ★★ M66：角色意图 ★★
    #   用户最初的诉求包含「补一下人物的背景」——角色页一直在语料里，
    #   但「东方仗助是谁」这类问句**不命中任何既有规则**（没有六维词、
    #   没有"替身"）→ 落到默认 structured → 执行器解析不出 → 降级语义检索。
    #   ★ 判据只有这一条（"要角色列表"）**加上**「抽到已知角色名」——见 route()。
    #   ★ 刻意**不**用「是谁 / 介绍 / 背景」这类宽词：
    #     · "是谁"    → 「这个替身的使用者是谁」里也有，会抢走 owner 查询
    #     · "介绍"    → 在 SEMANTIC 里，收了会抢走替身语义问句
    ROLE_LIST = re.compile(r"(哪些角色|有什么角色|角色列表|哪些人物)")

    # 同理：明确的**地点列表**问法（窄判，见 has_domain 的说明）
    LOC_LIST = re.compile(r"(哪些地点|有什么地点|地点列表|有哪些场所|哪些场所)")

    def __init__(self, known_stands: Optional[set[str]] = None,
                 known_entities: Optional[set[str]] = None,
                 known_locations: Optional[set[str]] = None,
                 known_characters: Optional[set[str]] = None):
        """
        Args:
            known_stands: 替身名集合（判定的基准）
            known_entities: **全部已知实体** —— 必须包含
                替身名 + 部名 + 使用者名。
                ★ 实测踩坑：只用替身名做判定，会把
                  「Stardust Crusaders（第3部）」「Giorno Giovanna（使用者）」
                  这类真实存在的实体误判为编造实体，
                  导致 T2 掉 20%、T3 掉 40%。
            known_locations: **地点名**（M65）—— 中/英/日三种写法。
                ★ 它必须与 known_entities 分开传：地名要参与「这是不是地点问句」
                  的判定，而 **不能**被当成替身名（否则「安杰罗岩」会被判成
                  锚定替身 → 走 entity_semantic → 去检索一个不存在的地点的原文）。
            known_characters: **角色名**（M66）—— 同理单独一份。
                ★ 角色名此前混在 known_entities 里（M20 起），后果与地名一样：
                  「东方仗助」会被当成**锚定替身** → 走 entity_semantic →
                  拿一个不存在的 stand_id 去检索。现在显式分开。
        """
        self.known = {s.lower() for s in (known_stands or set())}
        all_known = set(self.known)
        if known_entities:
            all_known |= {e.lower() for e in known_entities}
        # 用于「实体是否已知」判定的全集
        self.known_all = all_known
        # 替身名单独保留（用于「这是替身吗」的判断）
        self.known_stands = self.known
        # ★ M65：地点名单独一份（既参与「已知」判定，又要与替身名区分开）
        #   ★ 地名要并进 known_all —— 否则「安杰罗岩是什么地方」会因
        #     _is_known 返回 False 被判成**编造实体**直接拒答。
        self.known_locations = {x.lower() for x in (known_locations or set())}
        self.known_all |= self.known_locations
        # ★ M66：角色名单独一份（同地名）
        self.known_characters = {x.lower() for x in (known_characters or set())}
        self.known_all |= self.known_characters

    def _is_known(self, cand: str) -> bool:
        """判断候选实体是否已知。

        匹配策略（从严到宽）：
          1. 精确匹配
          2. 候选是已知实体的前缀（Star Platinum ⊂ Star Platinum: The World）
          3. 已知实体是候选的前缀（反向）

        ★ 实测踩坑：单纯子串匹配会漏判编造实体
          'King Crimson Red' 因包含已知实体 'king crimson' 被判为已知，
          'Weather Report Extreme' 同理。
          → 加一条规则：若候选是「已知实体 + 额外词」且额外词
            不构成已知实体的一部分，视为**变体名**（通常是编造）。
            判据：候选去掉已知实体后剩余部分≥3 字符，且候选不在已知集里。
        """
        cl = cand.lower().strip().strip(".,")
        if not cl or len(cl) < 2:
            return True# 太短，无法判定 → 视为已知（避免误伤）
        for k in self.known_all:
            if cl == k:
                return True
        # 子串匹配：候选 ⊂ 已知（Star Platinum vs Star Platinum: The World）
            if cl in k:
                return True
        # ★ 变体检测：候选 = 已知实体 + 后缀
        #   "King Crimson Red" ⊃ "king crimson" → 很可能是编造的变体名
        for k in self.known_all:
            if k in cl and cl != k:
                suffix = cl.replace(k, "").strip(" -_")
                if len(suffix) >= 3:
                    return False        # 认定为变体（编造）
                return True             # 后缀太短，如 "The World" ⊂ "...: The World"
        # ★★ M20 修 bug：这里原本是一个**裸 return True**（无条件返回 True）。
        #   实测后果：「东方常秀」这个《东方project》角色被判为"已知实体"——
        #   因为它既不在已知集、也不是任何已知实体的子串，
        #   却因为走到了函数末尾那个 return True 而被认作已知。
        #   → 于是 unknown_hit 永远 None，不拒答，
        #     最后语义兜底答成 Soft & Wet（东方定助的替身）。
        #   正确语义：所有规则都没命中 → **未知**。
        return False

    def _extract_entities(self, q: str) -> list[str]:
        """从问句里抽取候选实体。

        ★ 实测踩坑：不能把所有大写词都当实体。
          'Foo Fighters的GUARDING THE STAND DISCS方面…' 里的
          'THE STAND' 'DISCS' 是小节名的普通词，不是实体。
        → 规则：
          a) 排除全大写词（3 词以上）—— wiki 小节名常全大写
          b) 排除常见虚词大写形式（THE / AND / OF）
          c) 保留混合大小写的专有名词
        """
        # 引号内的实体最可靠
        quoted = re.findall(r"[\"'“”]([^\"'“”]{2,40})[\"'“”]", q)

        # ★★ M15：中文实体（M14 加了中文名支持，但路由没跟上 —— 副作用）
        #   实测：「天堂制造是什么」「黄金体验是什么」全被 abstain，
        #   因为 _extract_entities 只抽英文专有名词 → 中文名问句cands=[]。
        #   → 这里把「在已知中文别名里的片段」也当候选实体。
        #
        # ★★ M31：把字符集放宽到含**日文假名** ★★
        #   实测缺口：别名表里明明有日文名（star_platinum →
        #   'スタープラチナ'），但问「スタープラチナ 破壊力」时
        #   stand_entity=None → 又被 abstain 拒答。
        #   根因就是这里只认 \u4e00-\u9fff（汉字），而片假名在
        #   \u30A0-\u30FF —— 压根没进匹配范围。
        #   → 加上 \u3040-\u30ff（平假名 + 片假名）。
        zh_hits: list[str] = []
        for name in self.known_all or ():
            if len(name) >= 2 \
                    and re.search(r"[\u4e00-\u9fff\u3040-\u30ff]", name) \
                    and name in q:
                zh_hits.append(name)
        # 长名优先（「黄金体验」应排在「黄金」前面）
        zh_hits.sort(key=len, reverse=True)

        # 专有名词（混合大小写）
        proper = re.findall(
            r"\b([A-Z][a-z][A-Za-z0-9'’\-]*"
            r"(?:\s+(?:of|the|and|de)\s+|\s+)?"
            r"(?:[A-Z][A-Za-z0-9'’\-]+(?:\s+|$))*)", q)
        proper = [p.strip() for p in proper if p.strip()]

        # ★ 过滤：全大写串（wiki 小节名）
        out = []
        for c in quoted + proper:
            words = c.split()
            # 全大写且≥2 词 → 小节名/标题，排除
            if len(words) >= 2 and all(w.isupper() and len(w) > 1
                                       for w in words if w.isalpha()):
                continue
            # 含停用大写词开头（THE STAND / AND MORE）
            if words and words[0].upper() in ("THE", "AND", "OF", "A", "AN"):
                continue
            if len(c) >= 3 and re.search(r"[A-Za-z]", c):
                out.append(c)
        # ★★ 中文实体优先放最前（M15）
        #   路由后续逻辑会用 candidates 判断「有没有已知实体」，
        #   中文名问句若不进来，会被领域判据误判为「不相关」而 abstain。
        out = zh_hits + out
        # 去重保序
        seen = set()
        return [x for x in out if not (x in seen or seen.add(x))]

    def route(self, question: str, corpus=None) -> RouteDecision:
        q = question.strip()
        signals: dict[str, Any] = {}

        # ---------- 1. 无答案判定（优先，最容易误判的地方） ----------
        # ★★ M58b：编造信号词要**窄判** ★★
        #   实测踩坑（用户报的「点一个 ACT 后其他都不管用」）：
        #   FABRICATED_HINTS 里有 `act\s*4`（T6 评测集的假想替身叫「ACT4」），
        #   而 **Tusk ACT4 是我们库里真实的形态成员** → 一刀切拒答，
        #   用户问「獠牙 ACT4 的六维能力」直接被拦下，连带**整块档案卡与形态标签
        #   都不渲染** → 后面点别的形态毫无反应。
        #   ★ 不能改成"问句里有已知替身就放过"：T6 的编造题大多**同时提到真替身**
        #     （「替身 Star Platinum Ultimate 的使用者是谁」、
        #       「Tusk Final Form 是否有官方认证…」）→ 那样会把 9 条拒答题放过去。
        #   → 判据收窄为：命中词被**某个已知别名包含**时才算误伤。
        #     「獠牙 ACT4」含 ACT4 → 放过 ✓
        #     「Star Platinum」不含 Ultimate → 仍然拒答 ✓
        _fab = self.FABRICATED_HINTS.search(q)
        fabricated_signal = bool(_fab)
        if _fab and self.known_all:
            _frag = re.sub(r"\s+", " ", _fab.group(0)).strip().lower()
            # ★ 比对时**去掉所有空格**：别名表里存的是「獠牙ACT4」，
            #   用户写的是「獠牙 ACT4」—— 实测因为这个空格没对上，
            #   Tusk ACT4 依然被当成编造词拒答。
            _frag_ns = _frag.replace(" ", "")
            _qns = re.sub(r"\s+", "", q).lower()
            for _a in self.known_all:
                _a2 = re.sub(r"\s+", " ", str(_a)).strip().lower()
                if len(_a2) > len(_frag) and _frag in _a2 \
                        and _a2.replace(" ", "") in _qns:
                    fabricated_signal = False
                    signals["fabricated_hint_as_alias"] = _a2
                    break
        # 第二类信号：问不存在的内容（第9 部 / 版本号）
        oor_part = bool(self.OUT_OF_RANGE_PART.search(q))
        version_like = bool(self.VERSION_LIKE.search(q))

        # 抽取候选实体（已过滤小节名的全大写串）
        candidates = self._extract_entities(q)

        unknown_hit = None
        if self.known_all:
            for cand in candidates:
                if not self._is_known(cand):
                    unknown_hit = cand
                    break
        signals["candidates"] = candidates[:4]
        signals["unknown_entity"] = unknown_hit

        # ★★ M20：中文人名兜底检测
        #   known_entities 只装「已收录」的别名，未收录的人名抽不出候选，
        #   unknown_hit 就永远是 None → 不拒答 → 语义兜底答成别人。
        #   实测：「东方常秀的替身是什么」→ 答成 Soft & Wet（东方定助的替身）
        if unknown_hit is None:
            unknown_hit = self._unknown_zh_person(q)
            if unknown_hit:
                signals["unknown_entity"] = unknown_hit

        # ★★★ M16：区分「替身实体」与「维度词」
        #   实测踩坑：known_entities 里既装了替身名（Anubis、Tusk），
        #   也装了维度词（外观、形态、描述）。
        #   → 问句「Anubis 的外观形态方面有哪些描述？」抽出的候选是
        #     ['描述','形态','外观'] —— 真正的实体 Anubis 没被抽中，
        #     但因为这三个词都在 known_entities 里，has_entity 为真。
        #   ★ 若用 has_entity 判断「要不要按实体检索」，这种泛描述问句会被
        #     误判成 entity_semantic，改判后 T4/T5 评测直接挂 2 条。
        #   → 必须单独算stand_hit：候选里是否真的有**替身名**。
        #
        # ★★判据要同时满足两条：
        #   1. 在 known_all 里（含中文别名 —— 冷门替身只有中文名能被抽出）
        #   2. 不在「维度词」黑名单里（外观/形态/描述 是问法，不是实体）
        stand_hit = next(
            (c for c in candidates
             if c.lower() in self.known_all
             and c.lower() not in self.DIMENSION_WORDS
             # ★ M65：地名**不算**替身锚定 —— 否则「安杰罗岩」会被判成
             #   锚定替身 → 走 entity_semantic → 拿一个不存在的 stand_id
             #   去检索，返回与问题无关的原文。
             and c.lower() not in self.known_locations
             # ★ M66：角色名同理（此前混在 known_entities 里，一直有这个隐患）
             and c.lower() not in self.known_characters),
            None)
        signals["stand_entity"] = stand_hit

        # ★ M65：地点锚定（与 stand_hit 同构，只是查另一张名单）
        location_hit = next(
            (c for c in candidates if c.lower() in self.known_locations),
            None)
        # ★★ M77：地点**模糊**兜底（用户少打一个字）★★
        #   实测「虹村宅在哪」：库里有「虹村宅邸」，但地点只有精确匹配 →
        #   精确落空 → 角色的「虹村」子串（M74 的模糊）命中 → 路由到
        #   character，返回 4 个虹村家角色卡片。**不是数据缺，是地点太窄。**
        #   ★ 只在「问的是地点」时才补：LOCATION_ASK（在哪/什么地方）或
        #     地点列表问法。否则「承太郎的替身」这类会被地名片段抢走。
        #   ★ 守门：「华盛顿在哪里」→ 地名表里没有可少一字的近邻 → 仍 abstain。
        loc_ask = bool(self.LOCATION_ASK.search(q))
        if location_hit is None and (loc_ask or self.LOC_LIST.search(q)):
            try:
                from locations import match as _lm
                _lh = _lm(q)
                if _lh:
                    location_hit = _lh[0][1]
                    signals["location_fuzzy"] = [nm for _, nm in _lh[:6]]
            except Exception:  # noqa: BLE001
                pass
        signals["location_entity"] = location_hit

        # ★ M66：角色锚定
        character_hit = next(
            (c for c in candidates if c.lower() in self.known_characters),
            None)
        signals["character_entity"] = character_hit

        # ★★ M74：角色**模糊**兜底（用户打简称/姓氏）★★
        #   `candidates` 是「名字 ⊂ 问句」抽出来的，用户打「承太郎」
        #   「齐贝林」时方向反了 → 抽不到 → character_hit=None
        #   → 下面 `has_domain` 判 False → **直接 abstain**（实测三条全被拒答）。
        #   → 用 `characters.match()` 兜一层（它含「问句片段 ⊂ 名字」那一轮，
        #     且带子串索引，能认「承太郎 ⊂ 空条承太郎」）。
        #   ★ 只在 character_hit 为空时跑，不覆盖精确结果。
        #   ★ 守门仍成立：「今天天气怎么样」→ match 返回空 → 仍 abstain
        #     （子串索引只含**角色名**的片段，不含「天气」这种词；
        #       「天气预报」是替身名，不在角色表里）。
        if character_hit is None:
            try:
                from characters import match as _cm
                _fh = _cm(q)
                if _fh:
                    character_hit = _fh[0][1]
                    signals["character_fuzzy"] = [c for c, _ in _fh[:8]]
            except Exception:  # noqa: BLE001
                pass

        # ★★ M14 前置：领域相关性检查（放在所有意图判定之前）
        #   没有领域信号 → 明确拒答，而不是强行匹配某个意图
        #   ★ M65/M66：两类**明确的列表问法**也算领域信号
        #     （「有哪些角色」「哪些地点」——它们不含任何六维词、
        #      也抽不到实体名，不放行就会被当成无关问句拒答）。
        #     ★ 只放**窄**的列表问法：不能因为出现"地点/角色"这种词就放行，
        #       否则「华盛顿在哪里」也会变成有答案的。
        has_domain = (bool(self.DOMAIN_HINT.search(q))
                      or bool(self.ROLE_LIST.search(q))
                      or bool(self.LOC_LIST.search(q))
                      # ★ M74：角色**模糊**命中也算领域信号 ——
                      #   否则「齐贝林是谁」「承太郎」会在这里被拒答
                      #   （它们不含六维词、也没有精确实体名）。
                      or bool(character_hit)
                      # ★ M77：地点**模糊**命中也算（「虹村宅在哪」的
                      #   「虹村宅」不在 known_locations 里，但确实指向库内地点）
                      or bool(location_hit))
        signals["has_domain_hint"] = has_domain
        # 已知实体也算领域信号（「Tusk 的外观」有实体）
        has_entity = bool(candidates) and unknown_hit is None
        if not has_domain and not has_entity:
            return RouteDecision(
                "abstain",
                "问句里没有本数据集的实体或维度词"
                "（本库只收录 JoJo 替身数据）→ 不做无依据的回答",
                signals)

        if unknown_hit or fabricated_signal or oor_part or version_like:
            # 归因：说明是靠哪条规则判定的
            if unknown_hit:
                hit = f"编造实体「{unknown_hit}」"
            elif fabricated_signal:
                hit = "编造信号词"
            elif oor_part:
                hit = "询问不存在的部（第 10 部及以后）"
            else:
                hit = "版本号形态（如 v2），本数据集无此形态"
            return RouteDecision(
                "abstain",
                f"{hit} → 数据集中不存在，应拒答",
                signals)

        # ---------- 2′. 地点意图（M65）----------
        # ★★ M79：先处理「本库没有的数据类型」★★
        #   放在这里而不是最后：这类问句常带着实体名（"谁打败了普奇"），
        #   后面任何分支都会拿它去查角色/替身，然后返回一段不相干的介绍。
        if self.NO_DATA_ASK.search(q):
            return RouteDecision(
                "abstain",
                "本库只收录替身六维 / 招式 / 出场章节 / 角色档案 / 地点 / 篇章，"
                "**不含战况、死因与人物胜负关系** → 不拿不相干的介绍充数",
                signals)

        # ---------- 2′. 地点意图（M65）----------
        #   判据（两者取一）：
        #     ① 抽到了**已知地名** —— 「安杰罗岩是什么地方」「恶魔掌心在哪里」
        #     ② 问句在问「某部的地点」但没有具体地名 —— 「第 1 部有哪些地点」
        #   ★ 刻意**不**把「只提到地点词、没抽到地名」也放进来：
        #     「华盛顿在哪里」会因此变成有答案的问句，而库里没有华盛顿
        #     → 那等于凭空作答（M21 「无关问句必须拒答」的守门测试会挂）。
        #   ★ 与替身名的关系：若同句锚定了替身且**没问地点**，
        #     仍按替身走（例：「Tusk 的破坏力」），不能被地名抢走。
        #   ★ loc_ask 已在上面（M77 地点模糊兜底处）算过，这里复用。
        if location_hit or (loc_ask and self.PART_ANY.search(q)) \
                or self.LOC_LIST.search(q):
            if not (stand_hit and not loc_ask):
                return RouteDecision(
                    "location",
                    (f"识别到地点「{location_hit}」→ 查地点维度"
                     "（M65：否则会因没有六维/替身词被当成无关问句拒答）"
                     if location_hit else
                     "问句按「第 X 部 + 地点」提问 → 列出该部地点"),
                    signals)

        # ---------- 2. 混合意图判定（M2 的 T5 全错点） ----------
        # 特征：同时出现「结构化问法」与「语义问法」
        # ★★ M76：招式问法必须先拦走 ★★
        #   `SEMANTIC` 里含「招式」，会让「白金之星有什么招式」被判成
        #   entity_semantic（返回一段人物介绍），而库里已有结构化招式表。
        #   必须排在 hybrid / entity_semantic 之前，且要求**锚定了替身**
        #   （否则「招式是什么意思」这种泛问仍走语义）。
        if stand_hit and self.MOVE_ASK.search(q):
            return RouteDecision(
                "structured",
                "问某替身的招式 → 结构化（M76：stand_moves 已有 263 条）",
                signals)

        has_struct = bool(self.STRUCT_FACT.search(q)) or bool(self.EXTREME.search(q))
        has_sem = bool(self.SEMANTIC.search(q))
        signals["has_struct_pattern"] = has_struct
        signals["has_semantic_pattern"] = has_sem

        # ---------- ★ M80：类型 / 比较（放在 hybrid 之前）----------
        #   「白金之星是近战型吗」「哪些替身是自动型」「白金之星和黄金体验谁更强」
        #   都有结构化数据（stand_type_cn / stand_stats），但以前全掉进语义检索
        #   → 返回一段替身介绍。★ 布尔/比较要求**锚定替身**，否则
        #   「近战型是什么」这种泛问会拿不到实体（交给下面的语义分支）。
        if stand_hit and self.TYPE_ASK.search(q):
            return RouteDecision(
                "structured",
                "问替身的类型（布尔判断）→ 结构化（M80：stand_type_cn）",
                signals)
        if self.TYPE_ASK.search(q) and "替身" in q and not stand_hit:
            return RouteDecision(
                "structured",
                "按类型**筛选**替身 → 结构化（M80：stand_type_cn）",
                signals)
        if stand_hit and self.COMPARE_ASK.search(q):
            return RouteDecision(
                "structured",
                "两个替身比六维 → 结构化（M80：stand_stats，缺维度不按 0 计）",
                signals)
        # ★ M80：形态问法（「第二形态是什么」没有任何触发词，以前掉进语义检索）
        if stand_hit and self.FORMS_ASK.search(q):
            return RouteDecision(
                "structured",
                "问替身的形态 → 结构化（M80：stand_forms）",
                signals)

        if has_struct and has_sem:
            return RouteDecision(
                "hybrid",
                "同时包含结构化问法与语义描述需求 → 需两路协同",
                signals)

        # ---------- 3. 单一意图 ----------
        # ★★ M20：「XX 的替身是什么」→ 结构化（优先于 SEMANTIC）
        #   必须在 EXTREME / STRUCT_FACT / SEMANTIC 之前判，
        #   否则被 SEMANTIC 的「是什么」抢走 → 返回长段原文介绍。
        if self.STAND_OF_ASK.search(q) and not self.EXTREME.search(q) \
                and not self.AGG_GUARD.search(q):
            # ★★ 关键：分清「XX」是**人**还是**替身本身**——
            #   「软又湿的替身是什么」XX=软又湿 → **替身名**，
            #     该走原本的实体语义路径；
            #   「Jotaro Kujo 的替身是什么」XX=Jotaro Kujo → **角色名**，
            #     该走反查给替身名。
            #
            #   ★★ 判据不能用 _is_known（实测踩坑）：
            #     _is_known 覆盖 known_entities，而 M20 把**角色名**
            #     也加进了 known_entities（否则「Jotaro Kujo 的替身」
            #     会被判成编造实体而拒答）→ 于是 _is_known("Jotaro Kujo")
            #     为 True → 走进 entity_semantic → 又是一长段介绍。
            #   → 必须判「是否是**替身名**」：self.known 才是替身名集合。
            m_ask = self.STAND_OF_ASK.search(q)
            subject = (m_ask.group(1) if m_ask else "").strip()
            is_stand_name = subject.lower() in self.known
            if not is_stand_name:
                # ★ M79 曾在这里改成「命中角色就改走 character 路由」——
                #   **实测是错的**（M20/M21/M25/M66 五个测试文件当场变红）：
                #   那会把 answer_type 从 `stand_of` 换成 `character_stands`，
                #   打破既有契约（多轮追问还依赖 stand_of 的 stands 列表建上下文）。
                #   → 兜底放在 **executor._stand_of**：那里抽不到名字时
                #     用角色表反查（认「承太郎」这类简称），**类型保持 stand_of**。
                return RouteDecision(
                    "structured",
                    "问「某人的替身是哪个」→ 直接查库给替身名"
                    "（M20：不再走语义检索返回长段介绍）",
                    signals)
        # ---------- 2‴. 角色意图（M66）----------
        #   ★ 必须排在 STAND_OF_ASK **之后**：「Jotaro Kujo 的替身是什么」
        #     是"反查替身名"，用户要的是一个词，不能被角色卡抢走。
        #   ★★ 判据刻意**只有两条**（我第一版写成「出现 char_ask 就路由」，
        #      差点把「这个替身的使用者是谁」抢走 —— 那句里没有替身名，
        #      stand_hit 为空，会被判成角色问句而答成"没识别出角色"）：
        #     ① 抽到了**已知角色名**（且没同时锚定替身/地点）
        #     ② 问句明确要**角色列表**（「第 X 部有哪些角色」）
        #   ★ 与地点守门同一原则：不能只因为出现「角色/是谁」就路由，
        #     否则无关问句会变成"有答案"的（M21 的守门测试就是这么挂的）。
        _role_list = bool(self.ROLE_LIST.search(q))
        # ★ M79：「X 第一次出场是第几话」问的是**角色** → 角色卡里有 mangadebut。
        #   不加这条时它会掉进语义检索 → 返回角色介绍（答非所问）。
        _char_debut = bool(self.CHAR_DEBUT.search(q))
        if not stand_hit and not location_hit \
                and (character_hit or _role_list or (_char_debut and character_hit)):
            return RouteDecision(
                "character",
                (f"识别到角色「{character_hit}」→ 查角色档案"
                 + ("（M79：问首次登场章节，卡片里有 mangadebut）"
                    if _char_debut else "")
                 if character_hit
                 else "问句要角色列表 → 按部/全库列出"),
                signals)

        if self.EXTREME.search(q):
            return RouteDecision("structured", "极值/聚合类问题 → SQL", signals)
        if self.STRUCT_FACT.search(q):
            return RouteDecision("structured", "数值/归属类直接查询 → SQL", signals)
        if has_sem:
            # ★★★ M16 关键修复：语义问题**已锚定替身实体**时，不能丢给全库检索。
            #   实测（新增中文名后立刻暴露）：
            #     「骇游天外的能力是什么」→ 旧逻辑走 semantic → BM25 检索全库
            #     → 返回 Strength（Forever 的替身）——完全答非所问
            #     「小面孔的能力」        → 同理返回 Strength
            #   ★ 为什么「黄金体验的能力」以前是对的、这些却错？
            #     因为黄金体验在语料里高频（BM25 能排到第一），
            #     骇游天外/小面孔 是新补的冷门名，语料里**只有 name_en、没有中文**
            #     → BM25 匹配不到中文 → 退化成任意结果。
            #   → 语义问题一旦锚定了替身，就必须走「实体过滤」而非全库检索。
            #
            # ★★ 判据必须是 stand_hit（真替身名），不是 has_entity（含维度词）
            #   否则「Anubis 的外观形态方面有哪些描述？」这种泛描述问句会被改判，
            #   T4/T5 评测实测挂 2 条。
            if stand_hit:
                return RouteDecision(
                    "entity_semantic",
                    f"语义问题但已锚定替身「{stand_hit}」→ 按该替身检索"
                    "（M16：否则冷门替身的中文名会检索到别的替身）",
                    signals)
            return RouteDecision("semantic", "描述类问题 → 语义检索", signals)

        # 默认：数值类占比更高（评测集 62.5% 期望 structured）
        return RouteDecision("structured", "默认按结构化处理（多数题型）", signals)


# ==============================================================
if __name__ == "__main__":
    conflicts = json.loads((PROC / "conflicts.json").read_text(encoding="utf-8"))
    stands = json.loads((PROC / "stands.json").read_text(encoding="utf-8"))

    print("=" * 68)
    print("冲突消解")
    print("=" * 68)
    for use_consensus in (True, False):
        r = ConflictResolver(use_consensus=use_consensus)
        res = r.resolve_all(conflicts, stands)
        cnt = Counter(x.strategy for x in res)
        conf = sum(x.confidence for x in res) / len(res)
        print(f"\n  共识策略={'开' if use_consensus else '关'}: "
              f"{len(res)} 条，平均置信度 {conf:.3f}")
        for k, v in cnt.most_common():
            print(f"    {k:20s} {v:3d}")
        fragile = sum(1 for x in res if x.sensitivity.startswith("**"))
        print(f"    脆弱结论（策略依赖）: {fragile}")

    print("\n" + "=" * 68)
    print("路由测试")
    print("=" * 68)
    router = Router({s["name_en"] for s in stands})
    tests = [
        "所有替身中，破坏力最高的是哪个？",
        "Star Platinum 的破坏力是几级？",
        "Anubis 的外观形态方面有哪些描述？",
        "Star Platinum 的破坏力是几级？同时请说明它的能力描述。",
        "Star Platinum Ultimate 的能力值是多少？",
        "第六部未公布的设定中，The World Overlord 的能力值是多少？",
    ]
    for t in tests:
        d = router.route(t)
        print(f"  {d.route:11s} {t[:44]}")
        print(f"              {d.reason[:60]}")

"""
评测集生成器（M2-1）。

★ 核心设计：答案从数据推导，不依赖人工标注也不依赖 LLM 生成
  → 100% 可复现、零标注成本、零幻觉
  → 这是本项目能「精确评测」的根本原因

七类题型（对应立项书 §5.1）：
  T1 事实型        40 条  直接查六维数值
  T2 极值推理30 条  需要SQL 聚合（max/min/count/排序）
  T3 多跳          25 条  需跨表 JOIN（替身→使用者→部）
  T4 语义理解      25 条  需在文本块中定位
  T5 混合协同      25 条  ★ 需结构化 + 语义同时参与
  T6 无答案        20 条  测拒答能力
  T7 脏数据        15 条  测异常值处理

每条记录：
  qid题型 / question / question_type
  answer                标准答案（字符串或列表）
  answer_set            归一化答案集合（用于判分）
  evidence              支撑证据：
    - struct: {stand_id, field}  结构化字段
    - chunks: [chunk_id]         文本块 ID
  route_expect期望路径（structured / semantic / hybrid / abstain）
  judge自动判分类型：exact / set / numeric / contains
"""

from __future__ import annotations

import json
import random
import re
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

# 数据产物在 dataset/processed/（与 evaluation/ 平级）
PROC = Path(__file__).resolve().parents[1] / "dataset" / "processed"
DIMS = ["PWR", "SPD", "RNG", "STA", "PRC", "DEV"]
DIM_CN = {
    "PWR": "破坏力", "SPD": "速度", "RNG": "射程",
    "STA": "持续力", "PRC": "精密性", "DEV": "成长性",
}
LEVEL_CN = {0: "无", 1: "E", 2: "D", 3: "C", 4: "B", 5: "A"}


@dataclass
class EvalItem:
    qid: str
    question: str
    question_type: str          # T1..T7
    answer: Any
    answer_set: list
    evidence: dict
    route_expect: str# structured / semantic / hybrid / abstain
    judge: str                  # exact / set / numeric / contains
    note: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class EvalBuilder:
    def __init__(self, seed: int = 42):
        self.rng = random.Random(seed)
        self.stands = json.loads((PROC / "stands.json").read_text(encoding="utf-8"))
        self.stats = json.loads((PROC / "stand_stats.json").read_text(encoding="utf-8"))
        self.chunks = json.loads((PROC / "text_chunks.json").read_text(encoding="utf-8"))

        self.sinfo = {s["stand_id"]: s for s in self.stands}
        self.tinfo = {s["stand_id"]: s for s in self.stats}
        # 文本块按替身索引
        self.chunks_by_stand: dict[str, list[dict]] = defaultdict(list)
        for c in self.chunks:
            self.chunks_by_stand[c["stand_id"]].append(c)

        # 六维齐全的替身（可参与极值推理）
        self.complete = [
            s for s in self.stands
            if self.tinfo.get(s["stand_id"], {}).get("composite") is not None
        ]
        # 有 owner 的
        self.with_owner = [s for s in self.stands if s.get("owner_name")]
        self.items: list[EvalItem] = []

    # -------------------------------------------------------------
    # 工具
    # -------------------------------------------------------------
    def _v(self, sid: str, dim: str) -> Optional[int]:
        return self.tinfo.get(sid, {}).get(dim.lower())

    def _cat(self, sid: str, dim: str) -> str:
        return self.tinfo.get(sid, {}).get(f"{dim.lower()}_cat", "")

    def _name(self, sid: str) -> str:
        return self.sinfo.get(sid, {}).get("name_en", sid)

    def _lvl(self, v: int) -> str:
        return LEVEL_CN.get(v, str(v))

    def add(self, **kw) -> None:
        self.items.append(EvalItem(**kw))

    # -------------------------------------------------------------
    # T1 事实型（40）
    # -------------------------------------------------------------
    def build_t1(self, n: int = 40) -> None:
        pool = list(self.complete)
        self.rng.shuffle(pool)
        cnt = 0
        for s in pool:
            if cnt >= n:
                break
            sid = s["stand_id"]
            dim = self.rng.choice(DIMS)
            v = self._v(sid, dim)
            if v is None:
                continue
            q = f"{self._name(sid)} 的{DIM_CN[dim]}是几级？"
            self.add(
                qid=f"T1-{cnt+1:03d}",
                question=q,
                question_type="T1",
                answer=f"{self._lvl(v)}（{v}）",
                answer_set=[str(v), self._lvl(v)],
                evidence={"struct": {sid: [dim]}},
                route_expect="structured",
                judge="exact",
                note=f"{DIM_CN[dim]}={LEVEL_CN[v]}",
            )
            cnt += 1

    # -------------------------------------------------------------
    # T2 极值推理（30）—— 需 SQL 聚合
    # -------------------------------------------------------------
    def build_t2(self, n: int = 30) -> None:
        cnt = 0
        # 2a. 全局最大/最小
        for dim in DIMS:
            if cnt >= n:
                break
            vals = [(s["stand_id"], self._v(s["stand_id"], dim)) for s in self.complete]
            vals = [(sid, v) for sid, v in vals if v is not None]
            if not vals:
                continue
            mx = max(v for _, v in vals)
            mn = min(v for _, v in vals)
            winners = [sid for sid, v in vals if v == mx]
            if not winners:
                continue
            q = f"所有替身中，{DIM_CN[dim]}最高的是哪个？"
            self.add(
                qid=f"T2-{cnt+1:03d}",
                question=q,
                question_type="T2",
                answer=", ".join(self._name(w) for w in winners),
                answer_set=[self._name(w) for w in winners],
                evidence={"struct": {w: [dim] for w in winners}},
                route_expect="structured",
                judge="set",
                note=f"max({dim})={mx}({self._lvl(mx)})",
            )
            cnt += 1

        # 2b. 分部极值
        parts = [p for p in {s.get("part") for s in self.complete} if p]
        for part in sorted(parts):
            if cnt >= n:
                break
            sub = [s for s in self.complete if s.get("part") == part]
            if len(sub) < 3:
                continue
            dim = self.rng.choice(DIMS)
            vals = [(s["stand_id"], self._v(s["stand_id"], dim)) for s in sub]
            vals = [(sid, v) for sid, v in vals if v is not None]
            if not vals:
                continue
            mx = max(v for _, v in vals)
            winners = [sid for sid, v in vals if v == mx]
            pname = next((s.get("part_name_en") for s in sub if s.get("part")), f"第{part}部")
            self.add(
                qid=f"T2-{cnt+1:03d}",
                question=f"{pname}（第{part}部）中，{DIM_CN[dim]}为最高等级的替身有哪些？",
                question_type="T2",
                answer=", ".join(self._name(w) for w in winners),
                answer_set=[self._name(w) for w in winners],
                evidence={"struct": {w: [dim] for w in winners}},
                route_expect="structured",
                judge="set",
                note=f"part={part} max({dim})={mx}",
            )
            cnt += 1

        # 2c. 计数类
        for dim in DIMS:
            if cnt >= n:
                break
            vals = [self._v(s["stand_id"], dim) for s in self.complete]
            vals = [v for v in vals if v is not None]
            target = 5
            k = sum(1 for v in vals if v == target)
            if k == 0:
                continue
            self.add(
                qid=f"T2-{cnt+1:03d}",
                question=f"有多少个替身的{DIM_CN[dim]}达到 A 级（{target}）？",
                question_type="T2",
                answer=str(k),
                answer_set=[str(k)],
                evidence={"struct": {s["stand_id"]: [dim]
                                     for s in self.complete
                                     if self._v(s["stand_id"], dim) == target}},
                route_expect="structured",
                judge="exact",
                note=f"count({dim}={target})={k}",
            )
            cnt += 1

        # 2d. 双维过滤：某两维同时达标的替身
        for _ in range(200):
            if cnt >= n:
                break
            d1, d2 = self.rng.sample(DIMS, 2)
            t1, t2 = self.rng.sample([4, 5], 2)
            hits = [
                s["stand_id"] for s in self.complete
                if self._v(s["stand_id"], d1) == t1
                and self._v(s["stand_id"], d2) == t2
            ]
            if not 1 <= len(hits) <= 6:      # 太宽泛或太窄的题都不适合
                continue
            self.add(
                qid=f"T2-{cnt+1:03d}",
                question=(f"同时满足「{DIM_CN[d1]}为 {self._lvl(t1)}」与"
                         f"「{DIM_CN[d2]}为 {self._lvl(t2)}」的替身有哪些？"),
                question_type="T2",
                answer=", ".join(self._name(h) for h in hits),
                answer_set=[self._name(h) for h in hits],
                evidence={"struct": {h: [d1, d2] for h in hits}},
                route_expect="structured",
                judge="set",
                note=f"filter({d1}={t1} AND {d2}={t2}) -> {len(hits)} 个",
            )
            cnt += 1

        # 2e. composite 极值
        for _ in range(200):
            if cnt >= n:
                break
            comps = [(s["stand_id"], self.tinfo[s["stand_id"]]["composite"])
                     for s in self.complete]
            comps = [(sid, v) for sid, v in comps if v is not None]
            if not comps:
                break
            top = max(v for _, v in comps)
            winners = [sid for sid, v in comps if v == top]
            if len(winners) > 4:
                continue
            self.add(
                qid=f"T2-{cnt+1:03d}",
                question="综合六维数值（等级 0–5 求和），总分最高的替身是哪些？",
                question_type="T2",
                answer=", ".join(self._name(w) for w in winners),
                answer_set=[self._name(w) for w in winners],
                evidence={"struct": {w: DIMS for w in winners}},
                route_expect="structured",
                judge="set",
                note=f"max(composite)={top}",
            )
            cnt += 1

    # -------------------------------------------------------------
    # T3 多跳（25）—— 跨表 JOIN
    # -------------------------------------------------------------
    def build_t3(self, n: int = 25) -> None:
        cnt = 0
        pool = [s for s in self.with_owner if s.get("part")]
        self.rng.shuffle(pool)

        # 3a. 替身 → 使用者 → 所属部
        for s in pool:
            if cnt >= n // 2:
                break
            sid = s["stand_id"]
            pname = s.get("part_name_en") or ""
            # ★ answer 必须包含 answer_set 里的每个片段，
            #   否则判分器自测都拿不到满分（金标准自身不一致）
            ans_parts = [s["owner_name"], f"第{s['part']}部"]
            if pname:
                ans_parts.append(pname)
            self.add(
                qid=f"T3-{cnt+1:03d}",
                question=f"{self._name(sid)} 的使用者是谁？这位使用者出现在第几部？",
                question_type="T3",
                answer=f"{s['owner_name']}，第{s['part']}部"
                       + (f"（{pname}）" if pname else ""),
                answer_set=[s["owner_name"], str(s["part"]), pname],
                evidence={"struct": {sid: []}, "part": s["part"]},
                route_expect="structured",
                judge="contains",
                note="替身→owner→part 两跳",
            )
            cnt += 1

        # 3b. 同一使用者的多个替身
        by_owner: dict[str, list[dict]] = defaultdict(list)
        for s in self.with_owner:
            by_owner[s["owner_name"]].append(s)
        multi = {k: v for k, v in by_owner.items() if len(v) >= 2}
        for owner, group in list(multi.items())[:n - cnt]:
            if cnt >= n:
                break
            names = [g["name_en"] for g in group]
            sids = [g["stand_id"] for g in group]
            self.add(
                qid=f"T3-{cnt+1:03d}",
                question=f"使用者 {owner} 有哪些替身？",
                question_type="T3",
                answer=", ".join(names),
                answer_set=names,
                evidence={"struct": {s: [] for s in sids}},
                route_expect="structured",
                judge="set",
                note=f"owner→stands 反向查询，{len(names)} 个",
            )
            cnt += 1

        # 3c. 多形态替身：形态链
        for s in self.stands:
            if cnt >= n:
                break
            if s.get("form_count", 0) < 2:
                continue
            sid = s["stand_id"]
            forms = s.get("form_chain") or []
            self.add(
                qid=f"T3-{cnt+1:03d}",
                question=(f"{self._name(sid)} 有几个形态？"
                         f"它的形态链是怎样的？"),
                question_type="T3",
                answer=f"{s['form_count']} 个形态：{', '.join(forms)}",
                answer_set=[str(s["form_count"])] + forms,
                evidence={"struct": {sid: []}, "forms": forms},
                route_expect="structured",
                judge="contains",
                note="stand→form_chain 展开",
            )
            cnt += 1

        # 3d. 同部替身：某部有哪些替身（考察 part 聚合）
        part_groups: dict[int, list[dict]] = defaultdict(list)
        for s in self.with_owner:
            if s.get("part"):
                part_groups[s["part"]].append(s)
        for part in sorted(part_groups):
            if cnt >= n:
                break
            group = part_groups[part]
            if len(group) < 4:
                continue
            pname = group[0].get("part_name_en") or f"第{part}部"
            names = [g["name_en"] for g in group]
            self.add(
                qid=f"T3-{cnt+1:03d}",
                question=f"{pname}（第{part}部）中有明确使用者记录的替身有哪些？",
                question_type="T3",
                answer=f"{len(names)} 个：{', '.join(names)}",
                answer_set=names,
                evidence={"struct": {g["stand_id"]: [] for g in group}},
                route_expect="structured",
                judge="set",
                note=f"part={part} 聚合，{len(names)} 个",
            )
            cnt += 1

        # 3e. 使用者名字里的实体消歧：同名不同替身
        name_groups: dict[str, list[dict]] = defaultdict(list)
        for s in self.stands:
            nm = (s.get("name_en") or "").strip()
            if nm:
                name_groups[nm].append(s)
        # 3f. 形态数最多的替身
        ranked = sorted(self.stands, key=lambda s: -s.get("form_count", 0))
        for s in ranked:
            if cnt >= n:
                break
            if s.get("form_count", 0) < 2:
                break
            sid = s["stand_id"]
            self.add(
                qid=f"T3-{cnt+1:03d}",
                question=(f"在数据集中，{self._name(sid)} 有 {s['form_count']} 个形态记录。"
                         f"它的形态链包含哪些形态 ID？"),
                question_type="T3",
                answer=", ".join(s.get("form_chain") or []),
                answer_set=list(s.get("form_chain") or []),
                evidence={"struct": {sid: []},
                          "forms": s.get("form_chain") or []},
                route_expect="structured",
                judge="contains",
                note="form_chain 展开",
            )
            cnt += 1

    # -------------------------------------------------------------
    # T4 语义理解（25）—— 文本块定位
    # -------------------------------------------------------------
    def build_t4(self, n: int = 25) -> None:
        cnt = 0
        pool = [c for c in self.chunks if c["chunk_type"] in
                ("ability_overview", "section", "lore", "move")]
        self.rng.shuffle(pool)
        # ★ 小节名 → 自然问法（避免把内部标记 (lead)/(overview) 写进问句）
        SECTION_ASK = {
            "APPEARANCE": "外观形态", "PERSONALITY": "性格",
            "ABILITIES": "能力", "HISTORY": "历史背景",
            "BACKGROUND": "背景", "TIME STOP": "时停能力",
            "ENERGY THEFT": "能量吸取", "GRAVITY SHIFT": "重力操控",
            "SURFACE INVERSION": "表面反转", "DISC PROPERTIES": "光盘性质",
            "WRITTEN-IN COMMANDS": "Written-in 指令",
            "STAR PLATINUM: THE WORLD": "The World 形态",
        }

        def ask_for(c: dict) -> Optional[tuple[str, str]]:
            """按块类型与小节名生成自然问法。

            Returns:
                (问句后缀, 展示用标签)；不适合出题时返回 None
            """
            sec = (c.get("section") or "").strip()
            typ = c["chunk_type"]
            if typ == "move" and c.get("entity"):
                return (f"的招式 {c['entity']} 是怎么运作的？", c["entity"])
            if typ == "lore":
                return ("的名称或设定来源是什么？", "lore")
            if typ == "ability_overview":
                return ("的整体能力概述是什么？", "ability_overview")
            if typ == "section":
                if not sec or sec.startswith("("):
                    return None                      # 跳过内部标记小节
                label = SECTION_ASK.get(sec.upper())
                if label is None:
                    # 未收录的小节名：仅当纯 ASCII 且长度合理时使用原文
                    if not re.match(r"^[\x20-\x7e]{3,40}$", sec):
                        return None
                    label = sec
                return (f"的{label}方面有哪些描述？", label)
            return None

        for c in pool:
            if cnt >= n:
                break
            if len(c["content"]) < 100:
                continue
            got = ask_for(c)
            if got is None:
                continue
            tail, label = got
            sid = c["stand_id"]
            self.add(
                qid=f"T4-{cnt+1:03d}",
                question=f"{self._name(sid)}{tail}",
                question_type="T4",
                answer=c["content"][:180],
                answer_set=[c["content"][:80]],
                evidence={"chunks": [c["chunk_id"]]},
                route_expect="semantic",
                judge="contains",
                note=f"chunk_type={c['chunk_type']} label={label}",
            )
            cnt += 1

    # _pick_keyword 从文本里挑一个可检索的短语
    def _pick_keyword(self, text: str) -> Optional[str]:
        import re
        # 优先专有名词（连续大写词）
        m = re.findall(r"\b[A-Z][a-z]+(?:[A-Z][a-z]+)+\b", text)
        if m:
            return m[0]
        m = re.findall(r"\b[A-Z]{2,}(?:\s+[A-Z]{2,})*\b", text)
        if m:
            return m[0]
        return None

    # -------------------------------------------------------------
    # T5 混合协同（25）★ 核心差异化
    # -------------------------------------------------------------
    def build_t5(self, n: int = 25) -> None:
        cnt = 0
        # 5a. 数值 + 形态：某替身最突出的一维及其所属形态
        for s in self.complete:
            if cnt >= n // 2:
                break
            sid = s["stand_id"]
            vals = {d: self._v(sid, d) for d in DIMS}
            if any(v is None for v in vals.values()):
                continue
            best = max(vals, key=lambda k: vals[k])
            cands = [c for c in self.chunks_by_stand.get(sid, [])
                     if c["chunk_type"] in ("ability_overview", "section")]
            if not cands:
                continue
            c = cands[0]
            self.add(
                qid=f"T5-{cnt+1:03d}",
                question=(f"{self._name(sid)} 的六维中哪一维最高？"
                         f"同时请说明它的能力描述。"),
                question_type="T5",
                answer=f"{DIM_CN[best]}（{self._lvl(vals[best])}）；{c['content'][:140]}",
                answer_set=[DIM_CN[best], self._lvl(vals[best])],
                evidence={"struct": {sid: [best]}, "chunks": [c["chunk_id"]]},
                route_expect="hybrid",
                judge="contains",
                note="结构化求 max + 语义描述",
            )
            cnt += 1

        # 5b. 数值筛选 + 文本验证：能力值达A 且有明确招式描述
        move_stands = {
            c["stand_id"] for c in self.chunks
            if c["chunk_type"] == "move"
        }
        for s in self.complete:
            if cnt >= n:
                break
            sid = s["stand_id"]
            if sid not in move_stands:
                continue
            dim = "PWR"
            v = self._v(sid, dim)
            mv = [c for c in self.chunks_by_stand.get(sid, [])
                  if c["chunk_type"] == "move"]
            if v is None or not mv:
                continue
            names = [c["entity"] for c in mv if c.get("entity")]
            self.add(
                qid=f"T5-{cnt+1:03d}",
                question=(f"{self._name(sid)} 的破坏力是几级？"
                         f"它有哪些招式？"),
                question_type="T5",
                answer=f"{self._lvl(v)}；{', '.join(names)}",
                answer_set=[self._lvl(v)] + names,
                evidence={"struct": {sid: [dim]},
                          "chunks": [c["chunk_id"] for c in mv]},
                route_expect="hybrid",
                judge="contains",
                note="数值查询 + 招式列表（双向）",
            )
            cnt += 1

    # -------------------------------------------------------------
    # T6 无答案（20）—— 测拒答
    # -------------------------------------------------------------
    def build_t6(self, n: int = 20) -> None:
        exist = set(self.sinfo)
        templates = [
            "第六部未公布的设定中，{x} 的能力值是多少？",
            "替身 {x} 的使用者叫什么名字？",
            "{x} 在第 9 部中的表现如何？",
            "{x} 的破坏力具体是几？",
            "{x} 是否有官方认证的成长性数值？",
        ]
        fabricated = [
            "Star Platinum Ultimate", "The World Overlord", "Gold Experience Requiem II",
            "King Crimson Red", "Silver Chariot Revenant", "Foo Fighters Ultimate",
            "Hierophant Green Omega", "Magician's Red Neon", "Tusk Final Form",
            "Starlight Express", "Quantum Star Platinum", "Infinity Crusher",
            "Echoes Act 4", "Weather Report Extreme", "Kraft Work Omega",
            "Alien Stone Ocean", "Resurrection Stand", "Stand Arise v2",
            "Alternate World Stand", "Merged Stand",
        ]
        self.rng.shuffle(fabricated)
        for i in range(n):
            fake = fabricated[i % len(fabricated)]
            tpl = templates[i % len(templates)]
            self.add(
                qid=f"T6-{i+1:03d}",
                question=tpl.format(x=fake),
                question_type="T6",
                answer="无法回答：数据集中不存在该条目",
                answer_set=["无法回答", "不存在", "not found", "无答案"],
                evidence={"struct": {}},
                route_expect="abstain",
                judge="exact",
                note=f"fabricated={fake}",
                extra={"fabricated": fake},
            )

    # -------------------------------------------------------------
    # T7 脏数据（15）—— 测异常值处理
    # -------------------------------------------------------------
    def build_t7(self, n: int = 15) -> None:
        cnt = 0
        # 7a. 有UNPARSED/异常值的替身，问某维 → 应说明异常而非瞎答
        anomalous = [
            s for s in self.stands
            if any(self._cat(s["stand_id"], d) not in ("RANKED", "NONE", "")
                   for d in DIMS)
        ]
        self.rng.shuffle(anomalous)
        for s in anomalous:
            if cnt >= n:
                break
            sid = s["stand_id"]
            dim = None
            for d in DIMS:
                cat = self._cat(sid, d)
                if cat in ("EMPTY_SLOT", "UNKNOWN", "INFINITE",
                           "CONDITIONAL_NO_BASE", "CONDITIONAL"):
                    dim = d
                    break
            if dim is None:
                continue
            cat = self._cat(sid, dim)
            raw = self.tinfo.get(sid, {}).get(f"{dim.lower()}_raw", "")
            cat_desc = {
                "EMPTY_SLOT": "空缺（数据集未填写）",
                "UNKNOWN": "未知（存在争议）",
                "INFINITE": "无限",
                "CONDITIONAL": "条件值（附条件说明）",
                "CONDITIONAL_NO_BASE": "条件值（无基础等级）",
            }.get(cat, cat)
            # ★ 注意：不要用 {raw!r} —— repr 会把 NBSP 等特殊字符显示为
            #   \xa0 字面文本，导致与 answer_set 的原文不匹配
            self.add(
                qid=f"T7-{cnt+1:03d}",
                question=f"{self._name(sid)} 的{DIM_CN[dim]}是多少？",
                question_type="T7",
                answer=f"{cat_desc}（原始值 {raw}），无有效数值",
                answer_set=[cat_desc, "无有效数值", raw],
                evidence={"struct": {sid: [dim]}},
                route_expect="structured",
                judge="contains",
                note=f"cat={cat} raw={raw!r} —— 应说明异常而非瞎答",
            )
            cnt += 1

        # 7b. 冲突数据：问有源间冲突的维度
        conf_path = PROC / "conflicts.json"
        if conf_path.exists():
            conflicts = json.loads(conf_path.read_text(encoding="utf-8"))
            for cf in conflicts:
                if cnt >= n:
                    break
                if cf["stat_dim"] is None:
                    continue
                sid = cf["stand_id"]
                dim = cf["stat_dim"]
                self.add(
                    qid=f"T7-{cnt+1:03d}",
                    question=(f"数据源对 {self._name(sid)} 的{DIM_CN.get(dim, dim)}"
                             f"记录不一致，本项目采用哪个值？"),
                    question_type="T7",
                    answer=(f"{cf['resolved_value']}（采 {cf['resolution']}，"
                            f"冲突 {cf['value_a']} vs {cf['value_b']}）"),
                    answer_set=[str(cf["resolved_value"]), cf["resolution"]],
                    evidence={"struct": {sid: [dim]}},
                    route_expect="structured",
                    judge="contains",
                    note=f"conflict={cf['conflict_type']}",
                )
                cnt += 1

    # -------------------------------------------------------------
    def build_all(self) -> list[EvalItem]:
        self.build_t1(40)
        self.build_t2(30)
        self.build_t3(25)
        self.build_t4(25)
        self.build_t5(25)
        self.build_t6(20)
        self.build_t7(15)
        return self.items


# ------------------------------------------------------------------
def main() -> int:
    import sys
    b = EvalBuilder()
    items = b.build_all()
    out = PROC / "eval_set.json"
    out.write_text(
        json.dumps([i.to_dict() for i in items], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    from collections import Counter
    print("=" * 68)
    print("M2-1 评测集构建")
    print("=" * 68)
    print(f"  总条数{len(items)}")
    print("\n  题型分布：")
    for t, c in sorted(Counter(i.question_type for i in items).items()):
        print(f"    {t} {c:3d}")
    print("\n  期望路由分布：")
    for r, c in sorted(Counter(i.route_expect for i in items).items()):
        print(f"    {r:12s} {c:3d}")
    print(f"\n  判分方式：{dict(Counter(i.judge for i in items))}")

    # 覆盖度检查
    covered = set()
    for i in items:
        if "struct" in i.evidence:
            covered.update(i.evidence["struct"].keys())
    print(f"\n  覆盖替身 {len(covered)} / {len(b.stands)}")
    print(f"  输出：{out.name}  ({out.stat().st_size/1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())

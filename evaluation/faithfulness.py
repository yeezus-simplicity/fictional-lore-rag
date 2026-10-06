"""
忠实度评测（M6）。

★ 指标设计见 `faithfulness_design.md` —— 本文件是它的实现。
  设计原则：**零 LLM 参与**，全部用可机械核对的操作。
  （不用 LLM-as-judge：1.5B 做评审员不可靠，换强模型又引入新问题）

四个配置（A/B/C/D）对照：
  A  生成式 + 真实证据   ← 主指标
  B  抽取式（直接抄）    ← 忠实度上界
  C  生成式 + 空证据     ← ★ 关键：测模型编造程度
  D  生成式 + 乱码证据   ← 敏感度检验
"""

from __future__ import annotations

import json
import random
import re
import string
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
PROC = ROOT / "dataset" / "processed"


# ==================================================================
# 分句与归一
# ==================================================================

def split_sentences(text: str) -> list[str]:
    """中英混排分句。

    ★ 不用简单的 `.split('.')` —— 中文用 `。`，
      英文缩写（Mr. / ACT1.）会产生误切。
    """
    if not text:
        return []
    # 先按中文标点切
    parts = re.split(r"(?<=[。！？；])\s*", text)
    out: list[str] = []
    for p in parts:
        if not p.strip():
            continue
        # 再按英文句号切，但要避免常见缩写
        for q in re.split(r"(?<=[.!?])\s+(?=[A-Z(])", p):
            q = q.strip()
            if len(q) >= 2:
                out.append(q)
    return out or [text.strip()]


def normalize(s: str) -> str:
    """归一化：去掉标点空白、统一小写。"""
    s = s.lower()
    s = re.sub(r"[\s]+", " ", s)
    s = re.sub(r"[，,。.；;：:！!？?（）()\[\]【】\"']", "", s)
    return s.strip()


# 英文停用词（短语匹配时忽略）
STOP = set("""
a an the of to in for and or is are was were be been being with as at by on
it its this that these those he she they them his her their there here
what which who whom whose when where why how
stand stands ability value level part form
""".split())


# ==================================================================
# 指标 1：句级溯源
# ==================================================================

def _content_tokens(s: str) -> list[str]:
    """提取有信息量的 token（英文词 + 数字 + 等级字母）。"""
    s = normalize(s)
    words = re.findall(r"[a-z][a-z0-9'’]{1,}", s)
    nums = re.findall(r"\b\d+(?:\.\d+)?\b", s)
    grades = re.findall(r"\b(?:[a-e]|∞)\b", s)
    toks = [w for w in words if w not in STOP] + nums + grades
    return toks


def _evidence_token_set(evidence_texts: list[str]) -> set[str]:
    """证据的 token 集合（**用词边界精确匹配**）。

    ★★ 实测踩坑（第 1 次）：最初用 `t in ev_norm`（子串匹配），
      `"a"` 在 `"rated"` 里也能匹配 → 乱码证据也命中，指标完全无效。
      → 先切词建集合，再做成员判断。
    """
    toks: set[str] = set()
    for t in evidence_texts:
        n = normalize(t)
        toks.update(re.findall(r"[a-z][a-z0-9'’]{1,}", n))
        toks.update(re.findall(r"\d+(?:\.\d+)?", n))
        toks.update(re.findall(r"\b[a-e]\b", n))
    return toks


def _evidence_phrases(evidence_texts: list[str],
                      min_n: int = 2, max_n: int = 4) -> set[str]:
    """证据的连续 n-gram 集合。

    ★★ 实测踩坑（第 2 次，更隐蔽）：
      改成词袋精确匹配后，「打乱顺序的乱码证据」下溯源率**仍是 1.00**。
      根因：**词袋模型下，打乱顺序不改变 token 集合**。
      → 必须加入**连续短语**要求。词袋能测「用词」，
        n-gram 才能测「表述是否真的来自证据」。
    """
    grams: set[str] = set()
    for t in evidence_texts:
        words = re.findall(r"[a-z][a-z0-9'’]*|\d+(?:\.\d+)?|[a-e]", normalize(t))
        for n in range(min_n, max_n + 1):
            for i in range(len(words) - n + 1):
                grams.add(" ".join(words[i:i + n]))
    return grams


def sentence_supported(sentence: str, evidence_texts: list[str],
                       min_ratio: float = 0.6,
                       require_phrase: bool = True
                       ) -> tuple[bool, float]:
    """判断单句是否能被证据支持。

    判据（两级）：
      1. **词级**：信息 token 命中率 >= min_ratio
      2. **短语级**：★ 至少有一个 2-gram 连续短语出现在证据中
         —— 防止「用词都对但都是打乱来的」

    Returns:
        (supported, coverage)
    """
    toks = _content_tokens(sentence)
    if not toks:
        return True, 1.0        # 纯停用词的句子不算幻觉
    ev_set = _evidence_token_set(evidence_texts)
    hit = sum(1 for t in toks if t in ev_set)
    ratio = hit / len(toks)
    if ratio < min_ratio:
        return False, ratio

    if require_phrase and len(toks) >= 2:
        # 答案里的连续英文词对必须在证据里出现
        words = re.findall(r"[a-z][a-z0-9'’]*|\d+(?:\.\d+)?|[a-e]",
                           normalize(sentence))
        ev_phr = _evidence_phrases(evidence_texts, min_n=2, max_n=2)
        has_phr = any(" ".join(words[i:i + 2]) in ev_phr
                      for i in range(len(words) - 1))
        if not has_phr:
            return False, ratio
    return True, ratio


def trace_coverage(answer: str, evidence_texts: list[str]) -> dict:
    """句级溯源统计。"""
    sents = split_sentences(answer)
    if not sents:
        return {"n_sentences": 0, "supported": 0, "ratio": float("nan"),
                "unsupported_sents": []}
    supported = 0
    unsupported: list[str] = []
    ratios: list[float] = []
    for s in sents:
        ok, r = sentence_supported(s, evidence_texts)
        ratios.append(r)
        if ok:
            supported += 1
        else:
            unsupported.append(s)
    return {
        "n_sentences": len(sents),
        "supported": supported,
        "ratio": supported / len(sents),
        "avg_token_coverage": sum(ratios) / len(ratios),
        "unsupported_sents": unsupported,
    }


# ==================================================================
# 指标 2：数值正确率（针对事实型断言）
# ==================================================================

# 数值型断言的模式：「破坏力是 A」「射程为 E」「速度= C」
# ★注意否定：维度名里不能吞掉「不」，
#   否则「破坏力不是 A 级」会被解析成「维度=破坏力不，值=A」→ 判断反向
# ★ 数字必须用 \d+ （实测踩坑：原写 \d(?:\.\d)? 只匹配 1-2 位，
#   「射程是 300 米」被截成「3」→ 拿单个数字去比对，误判风险极高）
# ★ 等级字母必须**独立**出现（前后不接拉丁字母）
#   实测踩坑：「被描绘为 Diavolo 通过…」里的 D 被当成「破坏力=D」
#   —— Diavolo 是人名，D 只是名字的一部分。
#   → 加前后向断言(?<![A-Za-z])([ABCDE])(?![A-Za-z])
_NUM_ASSERT = re.compile(
    r"([一-鿿]{2,4}?)\s*(?:是|为|＝|=|：|:)\s*"
    r"((?<![A-Za-z])[ABCDE](?![A-Za-z])|\d+(?:\.\d+)?|∞|无|未知|不存在)"
)
# 独立出现的等级
_GRADE_RE = re.compile(r"\b([ABCDE])\s*级\b")


# ★ 维度名归一：抽出标准中文维度名
#   实测踩坑：「可以将 Tower of Gray 的破坏力等级定为 5 级」
#   非贪婪匹配把维度抽成「力等级定」—— 「破坏力」+「等级定为」的混合碎片
_DIM_CANON = {
    "破坏力": ["破坏力", "力量", "威力", "力"],
    "速度": ["速度", "速"],
    "射程": ["射程", "范围", "距离"],
    "持续力": ["持续力", "耐久", "持久"],
    "精密性": ["精密性", "精度"],
    "成长性": ["成长性", "成长"],
}


def _canon_dim(raw: str) -> Optional[str]:
    """把片段归一到标准维度名（取最长匹配）。

    ★ 关键：别名表要能覆盖「部分词被切掉」的情况。
      正则可能只切到「力等级定」，此时「破坏力」匹配不上，
      但若别名含「力」也能命中 → 故别名表包含单字兜底。
    """
    best = None
    for std, aliases in _DIM_CANON.items():
        for a in aliases:
            if a in raw:
                if best is None or len(a) > len(best[1]):
                    best = (std, a)
    return best[0] if best else None


def _is_list_index(context: str, m, val: str) -> bool:
    """判断这个数字是不是「列表序号」而非事实断言。

    ★★ 实测踩坑：「破坏力主要体现在以下几个方面：1. 缩小敌人」里的 1
      被抽成「破坏力=1」—— 凭空的假断言拉低了整个 numeric 指标。
      正确做法：它其实是**列表序号**，不是维度取值。

    判据（命中任一即视为序号）：
      - 数值前面是「：」「、」或列表符号（1. 2. 3.）
      - 数值后面紧跟「.」「、」（如「1. 缩小」）
      - 数值前 8 字内有「以下」「方面」「几点」「如下」
    """
    i = m.start(2)                       # 数值在 part 中的位置
    before = context[max(0, i - 10):i]
    after = context[m.end(2):m.end(2) + 3]
    # 「1. xxx」形式
    if re.match(r"^\s*[.、）)]", after):
        return True
    # 「以下方面：1」形式
    if re.search(r"(以下|如下|方面|几点|下列)[^。；]{0,6}$", before):
        return True
    return False


def _extract_asserts(answer: str) -> list[dict]:
    """抽取数值断言，正确处理否定与维度归一。

    ★★ 两个实测踩坑：
      1. 否定词被吞进维度名：「破坏力不是 A」→ 维度=「破坏力不」
         → 先按否定切分
      2. ★ 正则切碎维度：「可以将X的破坏力等级定为 5 级」
         正则的`[一-鿿]{2,4}?` 从「破」开始匹配 2 字就停 →得到「力」
         → **放弃从正则捕获组取维度**，改用「就近原则」：
           在数值位置**向前回溯**找最完整的维度词
    """
    asserts: list[dict] = []
    # 先按否定切分
    parts = re.split(r"(不是|不为|并不是)", answer)
    for i, part in enumerate(parts):
        neg_word = (i > 0 and i % 2 == 0)
        for m in _NUM_ASSERT.finditer(part):
            val = m.group(2)
            # ★ 就近归一：取数值位置前 12 字内最长的维度词
            prefix = part[max(0, m.start() - 12):m.start()]
            dim = _canon_dim(prefix) or _canon_dim(m.group(1)) or \
                re.sub(r"[不是为的等级定个]", "", m.group(1))
            # ★★ 剔除「列表序号」这类假断言
            #   实测踩坑：「体现在以下几个方面：1. 缩小敌人」里的 1
            #   被当成「破坏力=1」→ 凭空的假断言拉低整个指标。
            #   判据：数值紧跟列表符号（、. 或数字点）或在「以下方面」之后。
            if _is_list_index(part, m, val):
                continue
            asserts.append({
                "dimension": dim, "raw_dimension": m.group(1),
                "value": val, "source": "assert", "negated": neg_word,
            })
        if neg_word:
            # 「破坏力不是 A 级」：等级在否定词之后
            mneg = re.match(r"\s*([ABCDE]|\d+)\s*级?", part)
            if mneg and i >= 2:
                prefix = parts[i - 2][-12:]
                dim = _canon_dim(prefix) or \
                    (_DIM_CANON and re.findall(r"[\u4e00-\u9fff]{2,4}",
                                              parts[i - 2])[-1:])
                dim = dim[0] if isinstance(dim, list) and dim else (dim or "")
                asserts.append({
                    "dimension": dim, "raw_dimension": parts[i - 2][-6:],
                    "value": mneg.group(1), "source": "assert",
                    "negated": True,
                })
    return asserts


# ★ 证据里的「维度 → 等级/数字」映射
#★★ 实测踩坑：窗口大小是致命细节。
#   最初用 ±20 词窗口，证据「Destructive Power: A, Speed: C, Range: B」
#   里Power 的窗口会含B 和 C → 「射程是 A」被误判为正确。
#   → 改用**最小必要窗口**（维度词后 0~max_gap 个词）。
_DIM_ALIAS = {
    "破坏力": ["destructive", "power", "pwr"],
    "速度": ["speed", "spd"],
    "射程": ["range", "rng"],
    "持续力": ["stamina", "sta", "durability", "persistence"],
    "精密性": ["precision", "prc"],
    "成长性": ["growth", "dev", "development"],
}


def extract_dim_values(evidence_text: str, max_gap: int = 4
                       ) -> tuple[dict[str, set], dict[str, set]]:
    """从证据里抽「每个维度出现了哪些等级 / 数字」。

    Returns:
        (每维度的字母等级集合, 每维度的数字集合)
    """
    ev = normalize(evidence_text)
    words = re.findall(r"[a-z][a-z0-9'’]*|\d+(?:\.\d+)?|[a-e]", ev)
    grades: dict[str, set] = {d: set() for d in _DIM_ALIAS}
    nums: dict[str, set] = {d: set() for d in _DIM_ALIAS}
    all_aliases = {a for al in _DIM_ALIAS.values() for a in al}
    for i, w in enumerate(words):
        if w not in all_aliases:
            continue
        dim = next(d for d, al in _DIM_ALIAS.items() if w in al)
        # ★★ 向前扫描，但**遇到下一个维度词就停**
        #   「Power: A, Speed: C」里 Power 只取到 A，
        #   不会因为 max_gap=4 而把 Speed 的 C 也算进来。
        for j in range(i + 1, min(i + max_gap + 2, len(words))):
            g = words[j]
            if g in all_aliases and g != w:
                break                      # 撞上下个维度 → 停
            if re.fullmatch(r"[a-e]", g):
                grades[dim].add(g)
            elif re.fullmatch(r"\d+(?:\.\d+)?", g):
                nums[dim].add(g)
    return grades, nums


# ★ 等级字母 ↔ 0–5 数值的**权威映射**（来自 dataset/pipeline/encode.py）
#   ★★★ 实测踩坑：我自己推导 `abcde[4-n]`，
#      n=5 时索引 -1 越界**绕回** 'e' —— 明明 A=5 却算出 E。
#      原因：字母表是A..E（升序），而数值是 5..1（降序），
#      索引关系是 `(5-n)-1`，不是 `4-n`。
#      → 结论：**映射表必须复用数据层的权威定义，不要重新推导。**
GRADE_TO_VALUE = {"A": 5, "B": 4, "C": 3, "D": 2, "E": 1}
VALUE_TO_GRADE = {v: k for k, v in GRADE_TO_VALUE.items()}


def _looks_like_grade(answer: str, a: dict) -> bool:
    """判断 0–5 的小数字是「等级」还是「物理量」。

    ★ 实测踩坑：「破坏力等级定为 5 级」应按等级处理（A=5），
      但「射程是 5 米」是物理量。两者正则形态相同。
      → 看上下文有没有「等级/级/Level」这类词。
    """
    idx = answer.find(a["value"])
    ctx = answer[max(0, idx - 14): idx + 12] if idx >= 0 else answer
    return bool(re.search(r"等级|\s级|\blevel\b|rating", ctx, re.I))


def numeric_faithfulness(answer: str, evidence_text: str) -> dict:
    """抽取答案里的数值断言，逐条核对。

    ★★ 要点：
      1. 必须**维度 + 等级同时**在证据的对应位置出现才算支持
         （最初用子串匹配，任何等级都能在别处找到 → 恒为 0.00）
      2. 否定断言单独处理：「破坏力不是 A」与「破坏力是 A」判断相反
    """
    asserts = _extract_asserts(answer)

    for m in re.finditer(r"(\d+)\s*(个|位|名|条|种)", answer):
        asserts.append({"dimension": "count", "value": m.group(1),
                        "source": "count_assert", "negated": False})

    if not asserts:
        return {"n_assertions": 0, "supported": 0, "ratio": float("nan"),
                "details": []}

    ev = normalize(evidence_text)
    # ★ 复用公用的最小窗口扫描（contradiction 用的是同一套）
    ev_dim_grades, ev_dim_nums = extract_dim_values(evidence_text)
    dim_alias = _DIM_ALIAS          # 供下方按维度 key 查表
    all_grades = set(re.findall(r"\b[a-e]\b", ev))
    all_nums = set(re.findall(r"\d+(?:\.\d+)?", ev))

    ok = 0
    details: list[dict] = []
    for a in asserts:
        dim_raw, val = a["dimension"], str(a["value"])
        v = {"∞": "infinite", "无": "none", "未知": "unknown",
             "不存在": "none"}.get(val, val).lower()
        neg = a.get("negated", False)

        if a["source"] == "count_assert":
            in_ev = v in re.findall(r"\d+", ev)
        elif re.fullmatch(r"[a-e]", v) or (
                v in {"0", "1", "2", "3", "4", "5"} and _looks_like_grade(answer, a)):
            # ---- 等级类断言（A–E 字母或 0–5 数值）----
            # ★ 字母与数值是**同一件事的两种表示**：
            #   证据写 A，答案可能写「5级」（A=5）——
            #   只做字面比对会把正确回答误判为幻觉。
            # ★ 0–5 的小整数还需区分「等级」与「物理量」：
            #   「破坏力等级定为 5 级」是等级；「射程是 5 米」是物理量。
            dim_key = a["dimension"] if a["dimension"] in dim_alias else None
            grades = ev_dim_grades[dim_key] if dim_key else all_grades
            if v in grades:
                in_ev = True
            elif v in {"0", "1", "2", "3", "4", "5"}:
                in_ev = (VALUE_TO_GRADE.get(int(v)) or "").lower() in grades
            else:
                in_ev = False
        else:
            # ---- ★ 物理量类断言（如「射程是 5 米」）----
            #   实测踩坑：证据里射程写的是 `5 m (16.5 ft)` 这种**带单位的物理量**，
            #   不是 A–E 等级。原实现只比等级字母 → 全部误判为不支持。
            dim_key2 = a["dimension"] if a["dimension"] in dim_alias else None
            if dim_key2 and ev_dim_nums.get(dim_key2):
                # 优先：在该维度的邻近窗口里找这个数字
                in_ev = v in ev_dim_nums[dim_key2]
            else:
                # 退化：全文里有这个数字就算支持
                in_ev = v in re.findall(r"\d+(?:\.\d+)?", ev)

        # 否定断言：「不是A」在证据里 A 不存在 → 正确
        supported = (not in_ev) if neg else in_ev
        details.append({**a, "supported": supported})
        if supported:
            ok += 1
    return {
        "n_assertions": len(asserts),
        "supported": ok,
        "ratio": ok / len(asserts),
        "details": details,
    }


# ==================================================================
# 指标 3：实体一致率
# ==================================================================

def entity_consistency(answer: str, known_stands: set[str],
                       evidence_text: str) -> dict:
    """检查答案提到的替身名是否在证据里出现。"""
    ev = normalize(evidence_text)
    mentioned: list[str] = []
    for name in sorted(known_stands, key=len, reverse=True):
        if name.lower() in answer.lower():
            mentioned.append(name)
    if not mentioned:
        return {"n_entities": 0, "supported": 0, "ratio": float("nan"),
                "hallucinated": []}
    ok = 0
    bad: list[str] = []
    for m in mentioned:
        if normalize(m) in ev:
            ok += 1
        else:
            bad.append(m)
    return {"n_entities": len(mentioned), "supported": ok,
            "ratio": ok / len(mentioned), "hallucinated": bad}


# ==================================================================
# 指标 4：矛盾检测（最高价值）
# ==================================================================

# 反义词/对立模式
_CONTRA = [
    # 「不是 E」/「不是 E 级」
    (re.compile(r"不(?:是|为)\s*([ABCDE])\s*级?"), "grade_neg"),
    (re.compile(r"(?:没有|无)\s*(?:数值|等级|数据)"), "no_value"),
]


def contradiction(answer: str, evidence_text: str) -> dict:
    """检测答案是否与证据给出**相反**的说法。

    ★ 这是比「溯源」更严格的指标：
      答案里的词都在证据里，但说的是反面意思 —— 溯源 100% 但完全错误。
    """
    issues: list[str] = []
    ev = normalize(evidence_text)
    ans = normalize(answer)

    # --- 1. 显式否定：「不是 E 级」但证据里是 E ---
    for m in re.finditer(r"不(?:是|为)([abcde])", ans):
        g = m.group(1)
        if re.fullmatch(r"[a-e]", g) and g in re.findall(r"\b[a-e]\b", ev):
            issues.append(f"答案说「不是{g.upper()}级」，但证据里明确有该等级")

    for m in re.finditer(r"没有(?:数值|等级|数据)", ans):
        if re.search(r"[a-e]", ev):
            issues.append("答案说「没有数值」，但证据里有明确等级")

    # --- 2. ★ 维度冲突：证据说 C，答案说 E ---
    # 这类最隐蔽：答案的所有词都在证据里，但配错了维度
    # ★ 复用公用的最小窗口扫描（与 numeric_faithfulness 同一套）
    ev_dim_grades, _ = extract_dim_values(evidence_text)

    # ★复用 _extract_asserts —— 与 numeric_faithfulness 保持同一套抽取逻辑
    for a in _extract_asserts(answer):
        if a["source"] != "assert" or a.get("negated"):
            continue
        val = str(a["value"]).lower()
        if not re.fullmatch(r"[a-e]", val):
            continue
        for dim, grades in ev_dim_grades.items():
            if dim == a["dimension"] and grades and val not in grades:
                issues.append(
                    f"维度冲突：答案说{dim}={val.upper()}，"
                    f"但证据里{dim}的等级是 "
                    f"{'/'.join(sorted(g.upper() for g in grades))}")

    return {
        "n_contradictions": len(issues),
        "has_contradiction": bool(issues),
        "issues": issues,
    }


# ==================================================================
# 反向指标：防止「只抄一句」
# ==================================================================

def evidence_utilization(answer: str, evidence_texts: list[str]) -> dict:
    """答案覆盖了证据的多少。

    ★ 防止「溯源 100% 但只抄了最不相关的一句」。
    """
    ans = normalize(answer)
    if not ans:
        return {"ratio": 0.0, "n_evidence": len(evidence_texts)}
    used = 0
    per: list[float] = []
    for e in evidence_texts:
        toks = _content_tokens(e)
        if not toks:
            continue
        hit = sum(1 for t in toks if t in ans)
        r = hit / len(toks)
        per.append(r)
        if r >= 0.2:
            used += 1
    return {
        "ratio": sum(per) / len(per) if per else 0.0,
        "n_evidence": len(evidence_texts),
        "n_used": used,
    }


def is_hedging(answer: str) -> bool:
    """空洞回答检测：只说「无法确定」这类无信息量的话。"""
    hedges = [
        "无法确定", "无法回答", "不知道", "抱歉", "没有提供",
        "未提及", "没有提到", "不确定", "资料不足", "无法获取",
    ]
    a = normalize(answer)
    # 短且含hedging 词
    return len(a) < 40 and any(h in a for h in hedges)


# ==================================================================
# 汇总
# ==================================================================

@dataclass
class FaithScore:
    """单条答案的忠实度得分。"""

    trace_ratio: float          # 句级溯源率
    token_coverage: float# token 覆盖率
    numeric_ratio: float# 数值断言正确率
    entity_ratio: float         # 实体一致率
    contradiction: bool         # 是否有矛盾
    util_ratio: float           # 证据利用率
    hedging: bool               # 是否空洞回答
    n_unsupported: int = 0
    unsupported_examples: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "trace_ratio": round(self.trace_ratio, 4),
            "token_coverage": round(self.token_coverage, 4),
            "numeric_ratio": (round(self.numeric_ratio, 4)
                              if self.numeric_ratio == self.numeric_ratio
                              else None),
            "entity_ratio": (round(self.entity_ratio, 4)
                             if self.entity_ratio == self.entity_ratio
                             else None),
            "contradiction": self.contradiction,
            "util_ratio": round(self.util_ratio, 4),
            "hedging": self.hedging,
            "n_unsupported": self.n_unsupported,
            "unsupported_examples": self.unsupported_examples[:3],
        }


def score_one(answer: str, evidence_texts: list[str],
              known_stands: set[str],
              evidence_text: Optional[str] = None) -> FaithScore:
    """算单条答案的全部指标。"""
    if not evidence_texts:
        evidence_text = ""
    else:
        evidence_text = " ".join(evidence_texts)

    tr = trace_coverage(answer, evidence_texts)
    nu = numeric_faithfulness(answer, evidence_text)
    en = entity_consistency(answer, known_stands, evidence_text)
    co = contradiction(answer, evidence_text)
    ut = evidence_utilization(answer, evidence_texts)

    return FaithScore(
        trace_ratio=tr["ratio"],
        token_coverage=tr.get("avg_token_coverage", 0.0),
        numeric_ratio=nu["ratio"],
        entity_ratio=en["ratio"],
        contradiction=co["has_contradiction"],
        util_ratio=ut["ratio"],
        hedging=is_hedging(answer),
        n_unsupported=tr["n_sentences"] - tr["supported"],
        unsupported_examples=tr["unsupported_sents"],
    )


def aggregate(scores: list[FaithScore]) -> dict:
    """汇总。**nan 值不参与平均**（无断言时是 nan，不是 0）。"""
    import math

    def avg(xs: list[float]) -> Optional[float]:
        xs = [x for x in xs if not math.isnan(x)]
        return sum(xs) / len(xs) if xs else None

    n = len(scores)
    return {
        "n": n,
        "trace_ratio": avg([s.trace_ratio for s in scores]),
        "token_coverage": avg([s.token_coverage for s in scores]),
        "numeric_ratio": avg([s.numeric_ratio for s in scores]),
        "entity_ratio": avg([s.entity_ratio for s in scores]),
        "contradiction_rate": sum(1 for s in scores if s.contradiction) / n,
        "util_ratio": avg([s.util_ratio for s in scores]),
        "hedging_rate": sum(1 for s in scores if s.hedging) / n,
    }


# ==================================================================
# 乱码证据构造（配置 D）
# ==================================================================

def shuffle_evidence(evidence_texts: list[str],
                     seed: int = 42) -> list[str]:
    """构造对照证据（配置 D）。

    ★★ 实测踩坑：最初设计为「打乱词序」，但**词袋模型下
      打乱顺序不改变 token 集合**，导致乱码下溯源率仍 1.00，
      敏感度检验完全失效。
      → 改为**替换成真实存在的其他替身的证据**。
        这才是有意义的对照：内容真实、但与问题无关。
    """
    rng = random.Random(seed)
    # 从语料里取其他替身的文本（若可用）
    try:
        chunks = json.loads((PROC / "text_chunks.json").read_text(encoding="utf-8"))
        pool = [c["content"] for c in chunks if len(c["content"]) > 200]
        if pool:
            return [rng.choice(pool) for _ in evidence_texts]
    except Exception:
        pass
    # 退化：随机英文词
    words = []
    for t in evidence_texts:
        words.extend(t.split())
    rng.shuffle(words)
    return [" ".join(words[i:i + 60]) for i in range(0, len(words), 60)] or [""]


def strip_evidence(evidence_texts: list[str]) -> list[str]:
    """构造空证据（配置 C）。

    ★ 保留块数但内容清空 —— 这样检索元信息一致，
      唯一变量是「有没有内容」。
    """
    return ["" for _ in evidence_texts]


# ==================================================================
if __name__ == "__main__":
    # 自测：用构造数据验证每个指标的行为
    print("=" * 68)
    print("忠实度指标自测")
    print("=" * 68)

    ev = ["Star Platinum is a close-range Stand with exceptional strength "
          "and speed. Its destructive power is rated A.",
          "Time Stop is The World's ability."]

    # 期望列含义：数值断言是否应被正确判定
    # ★注意「编造数值」「与证据矛盾」两例的**溯源率仍会很高**——
    #   因为句子的词确实都在证据里（star/platinum/破坏力），
    #  只是等级说错了。这正是「溯源率高≠答案对」的典型演示，
    #   必须靠 numeric_ratio 与 contradiction 两个指标才能抓到。
    # ===★ 回归用例集（实测踩坑全部固化于此）★★
    ev2 = ("Tower of Gray is a close-range Stand. Destructive Power: A, "
           "Speed: C, Range: 5 m.")
    reg_cases = [
        # —— 等级字母 vs 0–5 数值是同一件事的两种表示 ——
        ("证据 A，答案写 5 级（换算）", "可以将破坏力等级定为 5 级。", True),
        ("证据 A，答案写 A 级", "破坏力是 A 级。", True),
        ("证据 A，答案写 C 级", "破坏力是 C 级。", False),
        ("证据 A，答案写 3 级", "破坏力是 3 级。", False),
        # —— 等级 vs 物理量必须区分 ——
        ("证据 Range: 5 m，答案 5 米", "射程是 5 米。", True),
        ("证据 Range: 5 m，答案 300 米", "射程是 300 米。", False),
        # —— 维度不能被切碎，也不能串到相邻维度 ——
        ("速度 C 正确", "速度是 C。", True),
        ("速度 A 错误（不能借用破坏力的值）", "速度是 A。", False),
        ("否定断言", "破坏力不是 A 级。", False),
    ]
    print("\n【回归用例】维度隔离 / 等级换算 / 物理量区分")
    print(f"  {'用例':38s} {'判定':>6s}  结果")
    print("  " + "-" * 60)
    reg_bad = 0
    for label, a, exp in reg_cases:
        r = numeric_faithfulness(a, ev2)
        got = r["ratio"] == 1.0
        if got != exp:
            reg_bad += 1
        print(f"  {label:38s} {r['ratio']:>6.2f}  "
              f"{'OK' if got == exp else '?? 期望' + str(exp)}")
    print(f"  → 回归失败 {reg_bad}/{len(reg_cases)}")

    cases = [
        ("完全抄证据",
         "Star Platinum 的破坏力是 A 级。", True),
        ("改写但信息保留",
         "Star Platinum 具备卓越的力量，破坏力评级为 A。", True),
        ("编造数值（证据里没有 B）",
         "Star Platinum 的破坏力是 B 级。", False),
        ("提到证据外的实体",
         "White Stone 的破坏力是 A 级。", True),
        ("否定正确（证据是 A）",
         "Star Platinum 的破坏力不是 A 级。", False),
        ("空洞回答",
         "无法确定。", None),
    ]

    known = {"Star Platinum", "The World", "White Stone"}

    print(f"\n{'用例':30s} {'溯源':>6s} {'数值':>6s} {'矛盾':>6s}  判定")
    print("-" * 72)
    for label, ans, expect in cases:
        s = score_one(ans, ev, known)
        num = f"{s.numeric_ratio:.2f}" if s.numeric_ratio == s.numeric_ratio else "n/a"
        # ★ 判定依据是「数值断言是否正确」，不是溯源率
        got = (s.numeric_ratio == 1.0) if expect is not None else None
        if got is None:
            mark = "OK"
        else:
            mark = "OK" if got == expect else "??"
        print(f"{label:30s} {s.trace_ratio:>6.2f} {num:>6s} "
              f"{'有' if s.contradiction else '无':>6s}  "
              f"{mark} (exp={expect})")

    print("\n★ 关键观察：「编造数值」与「否定正确」两例的**溯源率都是 1.00**，")
    print("  因为句子的词全都来自证据，错的是**等级数值**。")
    print("  → 这证明**单一指标不够**，必须组合 numeric + contradiction。")

    print("\n=== 反向指标 ===")
    s1 = score_one("Star Platinum 的破坏力是 A 级。", ev, known)
    s2 = score_one("Time Stop is The World's ability.", ev, known)
    print(f"  短答案证据利用率={s1.util_ratio:.3f}  空洞={s1.hedging}")
    print(f"  长答案证据利用率={s2.util_ratio:.3f}  空洞={s2.hedging}")
    print("  ★ 短答案溯源可能 1.0 但利用率低 → 双重指标防「只抄一句」")

    print("\n=== 乱码证据（敏感度检验）===")
    sh = shuffle_evidence(ev)
    s3 = score_one("Star Platinum 的破坏力是 A 级。", sh, known)
    print(f"  乱码证据下溯源率={s3.trace_ratio:.3f}（真实={s1.trace_ratio:.3f}）")
    print("  ★ 若乱码下仍很高 → 指标无效")

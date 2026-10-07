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

# ★★ 中文停用词（M11 新增）
#   中文句子必须切词，否则覆盖率只数英文词 → 中文部分「免费通过」。
_ZH_STOP = {
    "的", "了", "是", "在", "和", "与", "有", "为", "被", "把", "给",
    "并", "而", "及", "也", "都", "就", "还", "或", "一个", "这个",
    "可以", "能够", "会", "着", "过", "很", "非常", "比较", "这些",
    "那些", "它", "他", "她", "他们", "她们", "它们", "其", "该",
    "如下", "以下", "根据", "提供", "证据", "描述", "如下所示", "关于",
}


def _content_tokens(s: str) -> list[str]:
    """提取有信息量的 token（英文词 + 数字 + 等级字母 + **中文词块**）。

    ★★ M11 修正（重要）：
      原来**只切英文**，导致中文句子里只有替身名被计数，
      中文部分完全不影响覆盖率 → 「Anubis 由钢铁构成」被判 ratio=1.0。
      这个缺陷在 M6 没暴露，因为那时的答案主要是照抄英文原文。

      → 现在中文按 2 字滑窗切分，并过滤停用词。
        粒度粗一点没关系：宁可多计几个词，也不要让中文「免费通过」。
    """
    s = normalize(s)
    words = re.findall(r"[a-z][a-z0-9'’]{1,}", s)
    nums = re.findall(r"\b\d+(?:\.\d+)?\b", s)
    grades = re.findall(r"\b(?:[a-e]|∞)\b", s)
    toks = [w for w in words if w not in STOP] + nums + grades
    # ★ 中文字块
    #★★ M11 修正：滑窗会产生大量**跨词边界的垃圾 token**
    #   （「身体和黑暗」的滑窗给出 体和|和黑…），
    #   它们永远不可能在证据里命中 → 把 ratio 压得极低。
    #   实测：「具有人类的身体和黑暗犬类的头」→ 14 token 里只有 2 个是真概念，
    #   ratio 被压到 0.21，导致**真实翻译被误判为幻觉**。
    #
    #   → 只保留**落在映射表里的**窗口，即只算「已知概念」；
    #     不猜未知词的切法。未知内容由英文 token 兜底。
    zh_toks: list[str] = []
    known_zh = set(_ZH_EN_EQUIV)
    for run in re.findall(r"[\u4e00-\u9fff]+", s):
        for i in range(len(run) - 1):
            g = run[i:i + 2]
            if g in known_zh or g in _ZH_STOP:
                zh_toks.append(g)
    return toks + zh_toks


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



# ==================================================================
# ★★ 跨语言对应（实测踩坑 M11）
# ==================================================================
# 现象：生成式忠实度只有 0.75，比M6 离线的 0.91 低一截。
# 逐句定位后确认：**不是模型幻觉，是我的指标测不准**。
#
#   证据原文（英文）："with a human body and a dark canid's head"
#   答案（中文）  ："具有人类的身体和黑暗犬类的头"
#   → 这是**逐字翻译，完全忠于证据**，但中文词对不上英文 token
#   → 词级命中率低 → 被判为「无据」
#
# ★ 为什么不能简单放宽 min_ratio：
#   M3 已经踩过这个坑 —— 「乱码证据也能拿满分」，
#   放宽阈值会退化成「什么都能判对」，指标就失效了。
#
# ★ 正确做法：**只补跨语言等价词的对应关系**，
#   判定阈值与短语要求一律不动。
#   这样「翻译」被识别为有据，而「编造」仍会被抓住。
#
# 数据来源：均为本项目语料里实测的中英对照
# （不是通用翻译词典 —— 通用词典会引入大量无关映射）
_ZH_EN_EQUIV = {
    # ============================================================
    # ★★ 收录标准（踩过三次坑后总结）：
    #   只收「**可被替换的具体名词/部件/材质**」，
    #   长度 ≥ 2 字，且改掉它就改变了事实。
    #
    # ★ 反例（实测会误杀/漏判，务必不要加回来）：
    #   「外」「力」「速」        单字泛用词 —— 「外观/外形/外貌」全中
    #   「形象」「特点」「特征」  泛用描述词
    #   「出现」「描绘」「组成」  泛用动词
    #   「破坏力」「速度」        维度名 —— 证据写 "Destructive Power"，
    #                           映射成 destruction 对不上 → 误杀正确答案
    #
    # ★ 这张表是**守卫**用的：表里有、而证据里查不到对应英文
    #   → 判定为幻觉（把证据里的部件改成了别的）。
    #   因此表越宽泛，越容易误杀正常行文。
    # ============================================================

    # —— 头部 / 身体 ——
    "头饰": "headdress",
    "王冠": "headdress",
    "兜帽": "hood",
    "面具": "mask",
    "眼睛": "eye",
    "瞳孔": "pupil",
    "嘴巴": "mouth",
    "鼻子": "nose",
    "脖子": "neck",
    "手臂": "arm",
    "皮肤": "skin",
    "骨骼": "skeleton",
    "金属": "metal",
    "钢铁": "steel",
    "机械": "mechanic",
    "机械的": "mechanic",

    # —— 动物特征 ——
    "犬头": "dog head",
    "猫头": "cat head",
    "鸟头": "bird head",
    "人头": "human head",
    "骨头": "bone head",
    "犬类的头": "canid head",
    "黑暗犬": "dark canid",
    "翅膀": "wing",
    "翅膀状": "wing",
    "尾巴": "tail",
    "爪子": "claw",
    "犄角": "horn",

    # —— 能力元素 ——
    "火焰": "flame",
    "激光": "laser",
    "闪电": "lightning",
    "毒液": "poison",
    "光束": "beam",
    "弹丸": "bullet",
    "子弹": "bullet",
    "刀刃": "blade",
}



def _apply_cross_lang(answer_text: str, ev_set: set[str]) -> list[str]:
    """跨语言等价概念匹配。

    ★★M11 修正：改成**短语级**匹配，不是双字窗口映射。
      实测踩坑：tokens 是双字切分（人类/类的/身体），
      而映射表键是整词（人类的身体）→ 双字窗口永远匹配不上。
      → 现在直接在**原句**里搜整词，粒度与映射表对齐。

    ★ 只认「等价词确实在证据里」——
      映射表里有不代表证据里有，必须再判一次。
    ★ 阈值不动（min_ratio / require_phrase 的严格度保持不变），
      只是把「翻译」从「无据」纠正到「有据」。
    """
    hits: list[str] = []
    low = answer_text.lower()
    for zh, en in _ZH_EN_EQUIV.items():
        if zh in answer_text:
            # 正向：中文答案 ← 英文证据
            words_en = en.split()
            # ★ 容忍所有格与常见变形
            #   实测踩坑：证据是 "canid's head"，token 是 canid's，
            #   映射写canid head → 判不出，翻译被误判为幻觉。
            def _has(w: str) -> bool:
                cands = {w, f"{w}'s", w + "s"}
                if w.endswith("s"):
                    cands.add(w[:-1])
                return bool(cands & ev_set)
            if words_en and all(_has(w) for w in words_en):
                hits.append(zh)
            # 反向：证据里就是中文
            elif zh in ev_set:
                hits.append(zh)
    return hits


def _mapped_concepts(answer_text: str) -> list[str]:
    """句中出现的、映射表里有对应条目的**中文概念**。"""
    return [zh for zh in _ZH_EN_EQUIV if zh in answer_text]


_MODIFIERS = re.compile(r"一个|一些|数个|这个|那个|的|了|是|有")


def _strip_modifiers(s: str) -> str:
    """删掉无量词与结构助词，让「一个猫的头」能匹配到「猫头」。"""
    return _MODIFIERS.sub("", s)


def _has_unbacked_concept(answer_text: str, ev_set: set[str]) -> bool:
    """句中是否有**在证据里找不到对应**的可映射概念。

    ★ 这是跨语言幻觉的关键守卫（M11）。
      「翅膀」在映射表里（→ wing），但证据里没有 wing
      → 说明这句把证据里的「犬头」改成了「翅膀」→ 是幻觉，不是翻译。

    ★ 只认「映射表里有但证据里查不到」这一种情况；
      映射表里没有的未知词不走这里（交给 ratio 与短语判定）。
    """
    # ★ 先去掉修饰词再匹配
    #   实测踩坑：「一个猫的头」与映射键「猫头」不匹配
    #   （中间隔了「一个」「的」）→ 守卫漏判幻觉。
    #   → 把「一个/的/的」这类无量词删掉，做二次匹配。
    for zh in _mapped_concepts(answer_text) + \
                _mapped_concepts(_strip_modifiers(answer_text)):
        en = _ZH_EN_EQUIV[zh]
        ws = en.split()
        # 正向：对应英文（容许所有格变形）是否在证据里
        # ★★ 关键：多词短语必须**全部**命中才算「被支持」
        #   实测踩坑（M11）：「猫头」→ cat head，
        #   head 在证据里但 cat 不在。
        #   原实现「任一词命中即算支持」→ cat head 被放行 → **漏判幻觉**。
        #   （这比误判更严重：幻觉被放过 = 指标失效）
        #   → 改成 ALL 命中。
        found = bool(ws)
        for w in ws:
            # 所有格/复数变形双向兼容
            #   证据原文 "canid's head" → token 是 canid's，
            #   而映射写 canid → 不变形就查不到 → 真实翻译被误杀。
            cands = {w, f"{w}'s", w + "s"}
            if w.endswith("s"):
                cands.add(w[:-1])
            if not (cands & ev_set):
                found = False
                break
        # 反向：证据里就是这串中文
        if not found and zh not in ev_set:
            return True
    return False


def _is_cjk(s: str) -> bool:
    return any("\u4e00" <= c <= "\u9fff" for c in s)



# ★★ 开场套话（M11）：模型常在答案开头加这类引导语
_LEAD_IN = re.compile(
    r"^(根据(提供的)?(证据|资料|内容|上文)[，,、]?|"
    r"依据(上述|以上)?(证据|资料)[，,、]?|"
    r"从(提供的)?证据(中|里)?(可以)?(看出|得知|发现)?[，,、]?|"
    r"(关于|对于)[^，,。；]{0,20}(的)?(描述|说明|信息)?(如下|为)?[：:]?|"
    r"回答(如下|是)?[：:]?|"
    r"根据以上信息[，,、]?)\s*"
)


def strip_lead_in(sentence: str) -> str:
    """剥掉答案开头的引导语。

    ★★ M11 实测踩坑：
      模型输出「根据提供的证据，关于 Tusk 的描述如下：\n1. 在概念艺术中…」
      —— 缺句号导致整段被当成一句，其中前半截全是套话，
      词级覆盖率被拉到 0.5 → **正确内容被误判为幻觉**。

    ★ 只剥前缀，不动正文 —— 避免把事实内容也当套话删掉。
    """
    out = _LEAD_IN.sub("", sentence, count=1)
    return out if out.strip() else sentence


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
    # ★★豁免：正确声明「证据里没有」的句子（详见 is_hedge_sentence）
    #   这类句子是忠于证据的表现，不是幻觉；
    #   用词级覆盖率去judge它必然误判。
    if is_hedge_sentence(sentence):
        return True, 1.0

    # ★★剥掉开场套话（详见 strip_lead_in）
    sentence = strip_lead_in(sentence)

    toks = _content_tokens(sentence)
    if not toks:
        return True, 1.0        # 纯停用词的句子不算幻觉
    ev_set = _evidence_token_set(evidence_texts)

    # ★★★关键守卫（M11）：**映射表里有、但证据里没有**的概念 = 幻觉
    #   实测踩坑：判「Anubis 具有人类的身体和翅膀」时，
    #   「人类的身体」命中 → ratio 达标 → 判为有据，
    #   但「翅膀」在证据里**不存在** —— 这是把犬头改成了翅膀，是真幻觉。
    #
    #   → 必须在判定前检查：句中所有可映射的概念，
    #     是否都能在证据里找到对应。不在证据里的 → 直接判无据。
    if _has_unbacked_concept(sentence, ev_set):
        return False, 0.0
    hit = sum(1 for t in toks if t in ev_set)
    # ★ 跨语言对应（M11）：中文答案译自英文证据时，
    #   「人类的身体」↔ human body 这类等价应算命中。
    #   只补映射，不动阈值 —— 避免退化成 M3 的「什么都能判对」。
    # ★ 跨语言概念（M11）：一个等价概念约等于 2 个双字 token 的信息量
    if hit < len(toks):
        xhits = _apply_cross_lang(sentence, ev_set)
        hit += len(xhits) * 2
        ratio = hit / len(toks)
    ratio = min(1.0, hit / len(toks))
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
            # ★ 跨语言答案（中文）不可能在英文证据里找到 2-gram，
            #   所以对含 CJK 的句子改用「等价概念成对出现」作判据：
            #   至少要有 2 个映射命中，或 1 个映射 + 词级覆盖达标。
            #   注意：这不是放宽 —— 仍然要求**具体的**等价关系成立。
            mapped = set(_apply_cross_lang(sentence, ev_set))
            if len(mapped) >= 1:
                #★ 有明确的等价概念命中即算短语成立
                #（原文匹配走2-gram，这里是跨语言，两者等价可靠）
                has_phr = True
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


# ★ 空洞/拒答的标志短语（M11 扩充）
_HEDGE_MARKERS = [
    "无法确定", "无法回答", "不知道", "抱歉", "没有提供",
    "未提及", "没有提到", "不确定", "资料不足", "无法获取",
    # ★★ M11 新增：模型常用的「证据里没有这回事」说法
    "没有具体描述", "没有描述", "未详细描述", "没有详细",
    "没有提及", "没有说明", "没有提到", "没有给出", "未给出",
    "不包含", "没有该",
]


def is_hedge_sentence(sentence: str) -> bool:
    """判断单句是否在**声明「证据里没有相关内容」**。

    ★★ M11 新增（重要）：
      句级溯源检查「答案句的词是否在证据里」，
      而「证据中未提及 X 的外观形态」这种**正确的拒答**，
      天然不含证据里的内容 → 词级覆盖率必然很低 → 被误判为幻觉。

      实测踩坑：「Made in Heaven 的外观形态方面没有具体描述」
      忠实度判 0.000，但这**恰恰是正确答案**
      （该替身确实没有外观描述）。

      → 这类句子本身就在**正确地断言「证据里没有」**，
        属于忠于证据的表现，应豁免溯源判定。
    """
    a = normalize(sentence)
    return any(h in a for h in _HEDGE_MARKERS)


def is_hedging(answer: str) -> bool:
    """空洞回答检测：只说「无法确定」这类无信息量的话。"""
    a = normalize(answer)
    # 短且含 hedging 词
    return len(a) < 40 and any(h in a for h in _HEDGE_MARKERS)


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

    # ===★ 跨语言用例（M11 新增）===
    #  ★ 必须**双向**验证：
    #    只测「翻译该判对」会掩盖「幻觉被放过」——
    #    幻觉漏判比误判更严重（指标直接失效）。
    ev_zh = ["Anubis appears as an approximate version of the mythological "
             "Anubis it is named after, with a human body and a dark canid's "
             "head. Anubis is bare-chested but wears a headdress from ancient "
             "Egypt."]
    xlang = [
        ("翻译①人体+犬头（应有据）",
         "Anubis 是一个近似于神话中的 Anubis 的形象，具有人类的身体和黑暗犬类的头。",
         True),
        ("翻译②裸体+头饰（应有据）",
         "Anubis 裸体但戴着来自古埃及的头饰。", True),
        ("翻译③带修饰（应有据）",
         "Anubis 穿着来自古埃及的头饰，且是裸体的。", True),
        ("幻觉：犬头→翅膀（应无据）",
         "Anubis 具有人类的身体和翅膀。", False),
        ("幻觉：犬头→猫头（应无据）",
         "Anubis 具有人类的身体和一个猫的头。", False),
        ("幻觉：翅膀+臂章（应无据）",
         "Anubis 具有人类的身体、翅膀并戴着臂章。", False),
        ("幻觉：钢铁+激光（应无据）",
         "Anubis 由钢铁构成，能够发射激光。", False),
        ("幻觉：编数值（应无据）",
         "Anubis 的破坏力是 A 级，速度是 C。", False),
        ("幻觉：完全无关（应无据）",
         "Tusk 是能够操控火焰的替身。", False),
    ]
    print("\n【回归用例】跨语言翻译 vs 幻觉")
    print(f"  {'用例':30s} {'判定':>6s}  结果")
    print("  " + "-" * 58)
    xbad = 0
    for label, a, exp in xlang:
        r = trace_coverage(a, ev_zh)
        got = r["ratio"] == 1.0
        if got != exp:
            xbad += 1
        print(f"  {label:30s} {r['ratio']:>6.2f}  "
              f"{'OK' if got == exp else '?? 期望' + str(exp)}")
    print(f"  → 回归失败 {xbad}/{len(xlang)}")

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

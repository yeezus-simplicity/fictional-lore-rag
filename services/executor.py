"""
结构化查询执行器（M5）。

M4 的路由决定走 structured 路径后，**具体怎么查**由本模块负责。

设计立场：**不生成自然语言，只返回结构化结果 + 证据**。
理由：M1-M4 都没有生成层，若这里用模板拼句子，
等于引入一个没被评测过的组件。返回结构化数据更诚实。

每个函数返回 `(result, evidence)`：
  result    —— 可直接JSON 序列化的结构
  evidence  —— 这个结果是怎么来的（表名 / SQL 片段 / 来源）

★ 核心约束：**SQL 里绝不拼接用户输入的值**，全部走参数化查询。
   唯一用 f-string 的地方是「从枚举白名单里选列名」。
"""

from __future__ import annotations

import re
from typing import Any, Optional

# 六维白名单（唯一允许出现在 SQL 里的用户可控部分）
DIMS = ["PWR", "SPD", "RNG", "STA", "PRC", "DEV"]
DIM_CN = {
    "PWR": "破坏力", "SPD": "速度", "RNG": "射程",
    "STA": "持续力", "PRC": "精密性", "DEV": "成长性",
}

# ★★ M31：维度的多语言别名（简体 / 繁体 / 日文）★★
#   原来 extract_dim 只认 DIM_CN 里的**简体**名，于是
#   「スタープラチナの破壊力」虽然被判成 fact 意图（INTENT_FACT 认日文），
#   却在这步抽不出维度 → 拿不到数值，最后退回一段英文原文。
#   实测症状：route=structured 但答案是 ['Star Platinum is a humanoid...']。
DIM_ALIASES: dict[str, tuple[str, ...]] = {
    "PWR": ("破坏力", "破壞力", "破壊力", "パワー"),
    "SPD": ("速度", "スピード"),
    "RNG": ("射程", "射程距離", "レンジ"),
    "STA": ("持续力", "持續力", "持続力", "スタミナ"),
    "PRC": ("精密性", "精度", "プレシジョン"),
    "DEV": ("成长性", "成長性", "ポテンシャル"),
}
LEVEL_CN = {0: "无", 1: "E", 2: "D", 3: "C", 4: "B", 5: "A"}
AGG_CN = {"max": "最高", "min": "最低", "count": "数量", "top": "最强前"}

# 类别 → 人话（用于异常值说明）
CAT_CN = {
    "RANKED": "正常等级",
    "NONE": "明确无此能力",
    "EMPTY_SLOT": "数据缺失（空位）",
    "UNKNOWN": "未知（存在争议）",
    "INFINITE": "无限",
    "CONDITIONAL": "条件值（附条件说明）",
    "CONDITIONAL_NO_BASE": "条件值（无基础等级）",
    "NOT_APPLICABLE": "该源未提供",
    "UNPARSED": "未解析",
}


def level_label(value, cat: str | None = None) -> str | None:
    """等级值 → 显示文本。

    ★★ M53：INFINITE 的 `value` 是 NULL（∞ 放不进 1..5 的刻度），
       **但它是有值的** —— raw 列写着「∞」。
       以前这里一律按 `value is None` 返回 None，于是：
          用户问「天堂制造的速度是几级」→「数值」显示 **—**
          （看起来像"没数据"，而事实是"无限"，含义刚好相反）
       实测全库只有 6 个 INFINITE 维度（4 个替身），
       类别分布：NOT_APPLICABLE 198 / EMPTY_SLOT 53 / UNKNOWN 16 /
       **INFINITE 6** / CONDITIONAL_NO_BASE 3
       —— 只有 INFINITE 的 raw 是一个**值符号**，其余都是真的没有这个数。
    """
    if cat == "INFINITE":
        return "∞"
    return LEVEL_CN.get(value) if value is not None else None

# 意图识别
# ★ M31：维度词补上**繁体与日文** —— 原来只有简体，
#   于是「スタープラチナの破壊力」虽然路由放行了，意图却认不出来，
#   只能退回语义检索拿一段英文原文，而不是直接查到数值。
INTENT_FACT = re.compile(
    r"(是几级|幾級|何級|多少级|是多少|能力值|能力値|"
    r"破坏力|破壞力|破壊力|速度|スピード|射程|"
    r"持续力|持續力|持続力|精密性|成长性|成長性)")
INTENT_OWNER = re.compile(
    r"(使用者是谁|谁使用|持有者|是谁的)|"          # 正查：某替身的使用者
    r"(?:使用者|持有者)\s*.{2,30}?(?:有哪些|拥有|的替身)"  # 反查：某使用者有哪些替身
)
# ★★ M20 新增：「XX 的替身是什么」→ 反查（某使者有哪些替身）
#   实测用户反馈：「东方常秀的替身是什么」→ 答成一段 Soft & Wet 的
#   能力介绍（那是东方定助的替身）。两个问题：
#     1) 认不出人名 → 应拒答（已修 _is_known的裸 return True）
#     2) 认出人名时也不该走语义检索拿长段原文，
#        问的是「哪个替身」→ 应直接 SQL 查库给名字。
#   → 这个正则同时支持中文与英文写法：
#     「东方定助的替身」「承太郎的替身」「吉良吉影的 Stand」
INTENT_STAND_OF = re.compile(
    # ★ 主体要支持**含空格的英文全名**：'Jotaro Kujo的替身是什么'
    #   只写 (.{2,12}?) 会因中间空格匹配失败 → 意图不触发。
    # ★ 中文名要允许**间隔号**（·／・）：'乔鲁诺·乔巴拿'、'迪奥·布兰度'
    #   只写 [一-鿿] 会把带间隔号的中文名整句漏掉 → 退回 semantic
    #   又变成一长段原文介绍（M21 实测）。
    r"((?:[A-Za-z][A-Za-z0-9.'\-]*\s*){1,4}|[一-鿿·・]{2,14}?)\s*的\s*"
    r"(?:替身|STAND|Stand|stand|使的替身|本人|的本体)")

# ★★ M20：这些词出现时，**不能**判成「某人的替身」——
#   实测我引入的回归：「各部的替身数量」被 stand_of 抢走
#   （"各部"被当成人名），而它该走 part_count。
#   判据：出现聚合/章节/疑问词，说明问的不是「某个人的替身」。
INTENT_AGG_GUARD = re.compile(
    r"(多少|几个|数量|总数|排名|最高|最强|最弱|最大|最小|"
    r"各部|每部|这部|哪部|第\s*(\d+|[一二三四五六七八九十]+)\s*部|部数)")
# ★ 「第X部有哪些替身」这类**列表**问法。
#   实测缺失：它不含 INTENT_EXTREME 的词、也不含「多少个」，
#   于是 detect_intent 落到 unknown → 退回语义检索，
#   返回一堆无关 wiki 片段（补进去的第9部替身完全查不到）。
INTENT_PART_LIST = re.compile(
    r"(有哪些|是哪些|都有(哪些|什么)|都是(什么|哪些)|列出|列一下|列表)")
INTENT_FORMS = re.compile(r"(几个形态|有哪些形态|形态链)")
# ★★ M73：出场章节意图 ★★
#   M71 建了 `stand_appearances`（4902 条）与 `/appearances/{stand_id}` 端点，
#   但**没有自然语言入口** —— 用户打「白金之星在哪些章节出场」得到的是
#   一段原文片段（语义检索兜底），而库里明明有结构化的章节列表。
#   ★ 判据必须同时含「出场语义」与「章节/话数」：
#     只写「出场」会把「黄金体验的能力出场效果」之类误收；
#     只写「章节」会抢走「第 4 部有哪些章节」这类篇章问句。
INTENT_APPEARANCE = re.compile(
    r"(在哪几话|哪几话|在哪几集|哪几集|哪些章节|哪些话|哪些集|"
    r"(?:出场|登场|出现)\s*(?:记录|章节|话数|集数|在哪)|"
    r"在哪些(?:章节|话|集|漫画)|"
    r"的出场|出场记录|登场记录)")
INTENT_EXTREME = re.compile(
    r"(最高|最大|最强|最低|最小|最弱|多少个|几个|排名|综合|总分|"
    r"所有替身|同时满足|达到)")
# ★ 部数：必须同时认中文数字。
#   原正则 `第\s*(\d+)\s*部` 只认阿拉伯数字，
#   实测「第八部有多少替身」「第九部有哪些替身」全都匹配不到
#   → part=None → 落到语义检索，返回一堆无关 wiki 片段。
#   （而 verify 里用 SQL 直接查 part=3 是通的，所以静态检查发现不了。）
_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def part_to_int(s: str | None) -> int | None:
    """把 '8' / '八' / '第九' 之类的部数写法转成 int。"""
    if not s:
        return None
    s = s.strip()
    if s.isdigit():
        return int(s)
    # 「十」→10；「十一」这类本项目用不到，但仍处理「十N」
    if s.startswith("十"):
        return 10 + _CN_NUM.get(s[1:], 0) if len(s) > 1 else 10
    return _CN_NUM.get(s[0])


INTENT_PART = re.compile(r"(第\s*(\d+|[一二三四五六七八九十]+)\s*部)")
# ★ 篇章统计的**显式**触发词（M14 新增）
#   原来没有这些词，靠 detect_intent 的兜底"碰巧"触发 ——
#   代价是所有识别失败的问题都落到这里。
INTENT_PART_COUNT = re.compile(
    r"(每(部|个篇章|一部)|各(部|篇章)|分(部|篇章)|"
    r"按(部|篇章)|部.*(统计|分布|数量)|"
    r"(篇章|部分).*(统计|分布|多少个|几个))")


def detect_intent(question: str) -> str:
    """判断结构化子意图。

    ★★ 修正（M14 实测踩坑）：**`part_count` 不能当兜底默认值。**

      原实现最后一行是 `return "part_count"`，
      于是**任何匹配不上其他意图的问题**都会返回「按篇章分组的统计表」。

      用户实测撞到的就是这个：
          Q「黄金体验的能力」
          A → Stardust Crusaders 33 个替身 / Diamond is Unbreakable 29 个…
             （一张完全无关的统计表，且看起来"合理"）

      根因链：
        ① 中文名「黄金体验」不在 name2id（只收录英文名）→ 认不出实体
        ② 「能力」不在 INTENT_FACT 的关键词里 → 也不匹配 fact
        ③ → 落到兜底 part_count → 返回统计表

      ★ 一个「看起来合理但完全无关」的答案，比明确报错有害得多——
        用户会以为系统在回答他。

      修正：
        - 篇章统计改为**显式意图**（必须问「每部/各部/分部」才触发）
        - 认不出实体时返回 `unknown`，由上层给出明确提示与建议
    """
    if INTENT_FORMS.search(question):
        return "forms"
    # ★ M73：出场章节（M71 的 stand_appearances 的 NL 入口）
    if INTENT_APPEARANCE.search(question):
        return "appearance"
    # ★ 「第X部有哪些替身」→ 列表。必须放在 stand_of 之前：
    #   它含「替身」，若主体被当成部数名会诱使 stand_of 抢走。
    if INTENT_PART.search(question) and INTENT_PART_LIST.search(question) \
            and "替身" in question:
        return "part_list"
    # ★ M20：「XX 的替身是什么」优先于 owner —— 
    #   问的是「哪个替身」(stand_of)，不是「替身的使用者」(owner)。
    #   两者都含「的替身」，必须先判 stand_of，
    #   否则会把「东方定助的替身」答成「Soft & Wet 的使用者是东方定助」。
    if INTENT_STAND_OF.search(question) \
            and not INTENT_OWNER.search(question) \
            and not INTENT_AGG_GUARD.search(question):
        return "stand_of"
    if INTENT_OWNER.search(question):
        return "owner"
    if INTENT_PART_COUNT.search(question):
        # ★ 必须显式问「每部/各部/分部」才给篇章统计
        return "part_count"
    if INTENT_EXTREME.search(question):
        return "extreme"
    if INTENT_FACT.search(question):
        return "fact"
    # ★ 兜底改为「认不出」，而不是篇章统计
    return "unknown"


# ★ M21：替身中文名缓存（避免每次查询都读盘）
_STAND_ZH_CACHE: dict[str, str] = {}
_STAND_ZH_LOADED = False


def _stand_zh(stand_id: str) -> Optional[str]:
    """取替身中文名（M21）。给「XX 的替身是什么」的答案补上中文，更友好。"""
    global _STAND_ZH_LOADED
    if not _STAND_ZH_LOADED:
        try:
            from aliases import load_zh_map
            for sid, rec in load_zh_map().items():
                if isinstance(rec, dict) and rec.get("name_zh"):
                    _STAND_ZH_CACHE[sid] = rec["name_zh"]
        except Exception:  # noqa: BLE001
            pass
        _STAND_ZH_LOADED = True
    return _STAND_ZH_CACHE.get(stand_id)


def extract_dim(question: str) -> Optional[str]:
    """从问句里抽六维名。

    ★ M31：支持**简体 / 繁体 / 日文**三种写法（见 DIM_ALIASES）。
      英文缩写（PWR 等）也认。
    """
    for d in DIMS:
        for alias in DIM_ALIASES.get(d, (DIM_CN[d],)):
            if alias in question or re.search(rf"\b{d}\b", question, re.I):
                return d
    return None


# ---------------------------------------------------------------------------
# ★★ M38：条件值（stand_stat_conditional）的中文说明 ★★
# ---------------------------------------------------------------------------
# 背景：23 条六维带**条件限定**，例如
#     black_sabbath  PWR  base=1(E)  ← "Restraining Power: A"
#     whitesnake     PWR  base=NULL  ← "Power: Likely A (JoJoveller p.245)"
#     baby_face      PRC  base=NULL  ← "Depending on Education"
# 这些限定语一直存在 `*_raw` 里，但**只被当作杂讯丢掉** ——
# 用户问「黑安息日的破坏力」得到 E，永远不知道「束缚时是 A」。
# ★ 这不是缺功能，是**答案残缺但看起来完整**，比答错更难发现。
#
# ★ 设计原则（沿用本项目的可追溯原则）：
#   英文原文**一定保留**并展示 —— 它是源数据，是证据；
#   中文只是**辅助理解**（人工整理的 gloss，不是机翻）。
#   所以是「原文 + 中文说明」并列，不是替换。
_CONDITION_CN: dict[str, str] = {
    "Restraining Power: A": "束缚敌人时破坏力为 A（常态为 E）",
    "Depending on Education": "取决于「教育」程度，不是固定等级",
    "Only during replays": "仅在「回放」状态下成立",
    "Line of Sight": "射程取决于视线范围",
    "The length of the string": "射程取决于线的长度",
    "Range of a bullet": "射程约等于子弹射程",
    "Speed of Rain": "速度等同于雨下落的速度",
    "Extends to an entire train": "可延伸至整列火车",
    "Walls of G.D. St. Prison": "可覆盖绿海豚街监狱的全部墙壁",
    "About 3km in all directions": "约 3 公里，全方向",
    "100s of meters in mirror world": "镜中世界内数百米",
    "Approximately entire Orlando, Florida":
        "约覆盖整个佛罗里达州奥兰多市",
    "Tens of meters": "数十米",
    "20-30 meters": "20–30 米",
    "1-2 meters": "1–2 米",
}

# 前缀式限定语（"Speed: xxx" / "Power: xxx" / "Range: xxx"）
_COND_PREFIX_CN = {
    "power": "破坏力", "speed": "速度", "range": "射程",
    "stamina": "持续力", "precision": "精密性", "development": "成长性",
}

# ★★ 括号里的内容有两种，必须区分 ★★
#     "Range: 2 m (6.6 ft)"                    → 括号是**单位换算**，不是出处
#     "Power: Likely A (JoJoveller p.245)"     → 括号是**出处**
#   一律当出处会把「6.6 ft」写成「出处：6.6 ft」——
#   那是把换算关系说成了引用来源，属于**编造证据**。
#   → 只有命中「像出处的特征」才当出处。
_SRC_HINT_RE = re.compile(
    r"\bp\.\s*\d|\bvol\.|\bch\.|chapter|JOJOVELLER|artbook|guidebook"
    r"|databook|interview|\d{4}", re.I)


def _split_paren(body: str) -> tuple[str, Optional[str]]:
    """把 "... (xxx)" 拆成 (主体, 括号内容)。没有括号则返回 (body, None)。"""
    m = re.search(r"\(([^()]*)\)\s*$", body)
    if not m:
        return body.strip(), None
    return body[:m.start()].strip(), m.group(1).strip()


def explain_condition(note: Optional[str], dim: Optional[str] = None
                      ) -> Optional[str]:
    """把条件值原串整理成一句中文说明。识别不出返回 None。

    ★ 返回 None 时**不要编** —— 上层会只显示英文原文。
    """
    if not note:
        return None
    # 原文里有不换行空格（\xa0），先归一化，否则中文里会夹怪空格
    s = re.sub(r"\s+", " ", note.replace("\xa0", " ")).strip()
    if s in _CONDITION_CN:
        return _CONDITION_CN[s]

    # "Power: Likely A (JoJoveller p.245)" → 破坏力：Likely A（出处：…）
    m = re.match(r"^([A-Za-z]+)\s*:\s*(.+)$", s)
    if m:
        head = _COND_PREFIX_CN.get(m.group(1).lower())
        body, paren = _split_paren(m.group(2))
        if head:
            out = f"{head}：{body}"
            if paren:
                # ★ 只有确实像「出处」才标出处；否则当补充说明
                if _SRC_HINT_RE.search(paren):
                    out += f"（出处：{paren}）"
                else:
                    out += f"（{paren}）"
            return out

    # "Range: 2 m (6.6 ft)" 这类没走到上面（head 命中时会走），
    # 兜底：纯数值/量值
    body, paren = _split_paren(s)
    if re.search(r"\d", body) and re.search(
            r"met(er|re)s?|\bkm\b|\bm\b|\bft\b", body, re.I):
        return f"具体量值：{body}" + (f"（{paren}）" if paren else "")
    return None


def condition_caveat(cond: dict, dim: str, value: Optional[int]
                     ) -> str:
    """生成给用户看的提醒句。

    ★ 分两种情况，措辞必须不同：
      · value 有值 → 「常态是 X，但有限定」——用户已经看到 X 了
      · value 为 None → 「没有固定等级，源里给的是条件/推测值」
        这两种若用同一句话，前者会显得多余、后者会误导。
    """
    note = cond.get("note")
    cn = explain_condition(note, dim)
    dim_cn = DIM_CN.get(dim, dim)
    gloss = f"（{cn}）" if cn else ""

    if value is None:
        return (f"{dim_cn}没有固定的等级 —— 源数据给的是条件/推测值："
                f"「{note}」{gloss}。这不是「无数据」，"
                f"而是「该项能力取决于条件」。")
    base = LEVEL_CN.get(value, str(value))
    return (f"{dim_cn}的 {base} 是常态值，但该项带条件限定："
            f"「{note}」{gloss}。")


def extract_level(question: str) -> Optional[int]:
    """从问句里抽等级（A/B/C/D/E 或 5-1）。"""
    m = re.search(r"\b([A-E])\s*级?\b", question)
    if m:
        return 5 - "ABCDE".index(m.group(1))
    m = re.search(r"(\d)\s*级", question)
    if m and 0 <= int(m.group(1)) <= 5:
        return int(m.group(1))
    return None


def extract_stand_name(question: str, name2id: dict[str, str]) -> Optional[str]:
    """识别问句里的替身名。返回 stand_id。

    ★ 长名优先：避免 "Tusk" 抢走 "Tusk ACT1"。
    ★ 参数是 name→id 映射（api.STATE["name2id"]），不是集合。
    """
    for name in sorted(name2id, key=len, reverse=True):
        if name and name.lower() in question.lower():
            return name2id[name]
    return None


# ==================================================================
# 执行器
# ==================================================================

class StructuredExecutor:
    """结构化查询执行器。所有查询走参数化，无SQL 注入风险。"""

    def __init__(self, conn):
        self.conn = conn

    # ---------------------------------------------------------
    def execute(self, question: str, name2id: dict[str, str]
                ) -> tuple[Optional[dict], list[dict]]:
        """按意图分发。返回 (result, evidence)。

        Args:
            question: 用户问句
            name2id:替身名 → stand_id 映射
        """
        intent = detect_intent(question)
        fn = {
            "fact": self._fact,
            "extreme": self._extreme,
            "owner": self._owner,
            "forms": self._forms,
            "part_count": self._part_count,
            # ★ M20
            "stand_of": self._stand_of,
            # ★ 「第X部有哪些替身」列表
            "part_list": self._part_list,
            # ★ M73：出场章节（M71 数据的自然语言入口）
            "appearance": self._appearance,
        }.get(intent)
        # ★ M14：认不出意图时**明确说不知道**，不给无关答案
        if fn is None:
            return self._unknown(question, name2id)
        return fn(question, name2id)

    # ---------------------------------------------------------
    def _unknown(self, question: str, name2id: dict[str, str]):
        """★ M14：意图与实体都认不出时的**明确**回应。

        实测踩坑（用户报的 bug）：
          Q「黄金体验的能力」
          原实现 → 兜底 part_count → 返回一张
                   「Stardust Crusaders 33 个替身…」的统计表
                   —— **看起来合理但完全答非所问**，用户以为系统在回答他。

        这里改成说清楚三件事：
          1. 我没听懂（哪些词没认出来）
          2. 你可以在数据里搜到哪些相近的名字
          3. 怎么正确提问
        """
        sid = extract_stand_name(question, name2id)
        hints: list[str] = []

        # ★ 如果实体认不出但描述里像是在问某个替身，给替代建议
        if not sid:
            # 问句里的中文片段，尝试当别名查（服务层会用全量别名表）
            for frag in re.findall(r"[\u4e00-\u9fff]{2,}", question):
                if frag in name2id:
                    hints.append(f"你想问的是「{frag}」（{name2id[frag]}）？")

        # ★ 注意：Evidence.note 是 **str**（Pydantic 白名单模型，
        #   塞 dict 会 ValidationError → 500。实测踩过）。
        #   要传结构化信息得用 Evidence 上已声明的字段。
        lines = ["未能理解这个问题。"]
        if hints:
            lines.append("、".join(hints))
        lines.append("请用替身名提问，例如「Star Platinum 的能力是什么」"
                     "或「Star Platinum 的破坏力是几级」。")
        return None, [{
            "type": "note",
            "note": "".join(lines),
            "stand_id": sid,          # 认出来了就带上（认不出是 None）
        }]

    # ---------------------------------------------------------
    def _fact(self, question: str, name2id: dict[str, str]):
        """单点查询：某替身某维度是多少。"""
        sid = extract_stand_name(question, name2id)
        dim = extract_dim(question)
        if not sid or not dim:
            return None, [{"note": "未能识别替身名或维度名"}]

        cur = self.conn.cursor()
        cur.execute("""
            SELECT s.name_en, s.part, st.pwr, st.pwr_raw, st.pwr_cat,
                   st.spd, st.spd_raw, st.spd_cat,
                   st.rng, st.rng_raw, st.rng_cat,
                   st.sta, st.sta_raw, st.sta_cat,
                   st.prc, st.prc_raw, st.prc_cat,
                   st.dev, st.dev_raw, st.dev_cat
            FROM stands s JOIN stand_stats st ON st.stand_id = s.stand_id
            WHERE s.stand_id = %s
        """, (sid,))
        row = cur.fetchone()
        if not row:
                return None, [{"note": f"{sid} 不在库中"}]

        name = row[0]
        # 列顺序：name, part, 然后每维 (value, raw, cat)
        off = 2
        vals = {}
        for d in DIMS:
            i = off + DIMS.index(d) * 3
            vals[d] = {"value": row[i], "raw": row[i + 1], "cat": row[i + 2]}

        # ★★ M38：取该替身的条件值（23 条里可能命中该替身的某一维）★
        #   之前完全没查这张表 —— 于是「黑安息日的破坏力」只答 E，
        #   永远不提「束缚时是 A」。
        cur.execute("""
            SELECT stat_dim, base_value, raw_value, condition_note
            FROM stand_stat_conditional WHERE stand_id = %s
        """, (sid,))
        conds = {r[0]: {"base_value": r[1], "raw": r[2], "note": r[3]}
                 for r in cur.fetchall()}

        v = vals[dim]
        cat = v["cat"] or "UNKNOWN"
        cond = conds.get(dim)
        result = {
            "type": "fact",
            "stand_id": sid,
            "stand_name": name,
            "part": row[1],
            "dimension": dim,
            "dimension_cn": DIM_CN[dim],
            "value": v["value"],
            # ★ M53：INFINITE → ∞（不是 None → 前端显示「—」）
            "value_label": level_label(v["value"], cat),
            "raw": v["raw"],
            "category": cat,
            "category_cn": CAT_CN.get(cat, cat),
            "all_dims": {d: {
                "value": vals[d]["value"],
                "label": level_label(vals[d]["value"], vals[d]["cat"]),
                "raw": vals[d]["raw"],
                "category": vals[d]["cat"],
                # ★ M38：连同中文一起给 —— 前端就不必再抄一份映射
                #   （抄两份迟早漂移，实测前端那套已经缺了 5 个类别）
                "category_cn": CAT_CN.get(vals[d]["cat"] or "", None),
                # ★ 每一维也带上自己的条件（雷达卡/前端可逐格提示）
                "conditional": ({**conds[d],
                                 "cn": explain_condition(conds[d]["note"], d)}
                                if d in conds else None),
            } for d in DIMS},
        }

        # ★ 被问的那一维有条件是重点：单独给 caveat，前端要显式展示
        if cond:
            result["conditional"] = {
                "note": cond["note"],
                "note_cn": explain_condition(cond["note"], dim),
                "base_value": cond["base_value"],
            }
            result["caveat"] = condition_caveat(cond, dim, v["value"])

        # ★ 该替身**其他**维度有条件时也提一句：用户可能没问那一维，
        #   但「A·A·C·A·A·A」这种六个字母并排展示时，
        #   若其中一格其实带条件，整体印象就被误导了。
        others = [d for d in DIMS if d in conds and d != dim]
        if others:
            result["other_conditions"] = [
                {"dimension": d, "dimension_cn": DIM_CN[d],
                 "note": conds[d]["note"],
                 "note_cn": explain_condition(conds[d]["note"], d),
                 "value": vals[d]["value"],
                 "value_label": level_label(vals[d]["value"], vals[d]["cat"])}
                for d in others
            ]

        evidence = [{
            "type": "database",
            "table": "stand_stats",
            "field": f"{dim.lower()}, {dim.lower()}_raw, {dim.lower()}_cat",
            "stand_id": sid,
            "note": "数值来自消解后的 stand_stats 表",
        }]
        if cond:
            # ★ 条件值也算一条证据来源 —— 不能让用户以为
            #   「有限定语」是模型自己加的
            evidence.append({
                "type": "database",
                "table": "stand_stat_conditional",
                "field": f"stat_dim='{dim}', condition_note",
                "stand_id": sid,
                "note": f"条件限定来自源数据原文：「{cond['note']}」",
            })
        return result, evidence

    # ---------------------------------------------------------
    def _extreme(self, question: str, name2id: dict[str, str]):
        """极值/聚合查询。"""
        cur = self.conn.cursor()
        dim = extract_dim(question)
        mp = INTENT_PART.search(question)
        part = part_to_int(mp.group(2)) if mp else None

        # top-N 形式
        m = re.search(r"前\s*(\d+)", question)
        if m or "最强" in question or "最高" in question:
            n = int(m.group(1)) if m else 5
            dim = dim or "PWR"
            # ★★ M38：LEFT JOIN 条件值表 ★★
            #   排名里若混进「带条件限定」的项，用户会当成硬实力。
            #   实测：「射程最高」的榜单里有 Moody Blues(RNG=5)，
            #   但它那条是「Only during replays」—— 只在回放时成立。
            #   不标出来，这个第一名就是误导。
            sql = """
                SELECT o.name_en, o.part, o.{d}, o.name_ja, c.condition_note,
                       o.{d}_cat
                FROM v_stand_overview o
                LEFT JOIN stand_stat_conditional c
                       ON c.stand_id = o.stand_id AND c.stat_dim = %s
                -- ★★ M54：把「∞（无限）」放进排名，并排在 A 之前 ★★
                --   以前 `o.{d} IS NOT NULL` 直接把它排除 ——
                --   实测「速度最高的是谁」漏掉了速度无限的天堂制造，
                --   返回的是 A 级的 Ball Breaker。
                --   ∞ 的数值列是 NULL（放不进 1..5），所以三个条件都要放宽：
                --     · composite IS NOT NULL（原过滤：六维不全就不参评）
                --     · {d} IS NOT NULL
                WHERE (o.composite IS NOT NULL OR o.{d}_cat = 'INFINITE')
                  AND (o.{d} IS NOT NULL OR o.{d}_cat = 'INFINITE')
                {part_filter}
                -- ★ 先按「是不是 ∞」排（true 在前），再按数值降序。
                --   DESC 默认 NULLS FIRST（Postgres）→ 必须显式 NULLS LAST，
                --   否则 ∞ 那一行的数值 NULL 会跑到最前面。
                ORDER BY (o.{d}_cat = 'INFINITE') DESC NULLS LAST,
                         o.{d} DESC NULLS LAST, o.name_en
                LIMIT %s
            """
            # ★ psycopg2 的 execute() 返回 None，结果要用 cur.fetchall() 取
            params: tuple = (dim.upper(),)
            if part:
                params += (part,)
            params += (n,)
            cur.execute(sql.format(d=dim.lower(),
                                   part_filter="AND o.part = %s" if part else ""),
                        params)
            rows = cur.fetchall()
            items = []
            n_inf = 0
            for r in rows:
                _cat = r[5]
                _inf = (_cat == "INFINITE")
                if _inf:
                    n_inf += 1
                # ★ M54：INFINITE 的 value 是 NULL → label 用 level_label 得 '∞'
                it = {"name": r[0], "part": r[1], "value": r[2],
                      "label": level_label(r[2], _cat), "name_ja": r[3]}
                if _inf:
                    it["infinite"] = True
                if r[4]:
                    it["conditional_note"] = r[4]
                    it["conditional_note_cn"] = explain_condition(r[4], dim)
                items.append(it)
            n_cond = sum(1 for it in items if it.get("conditional_note"))
            result = {
                "type": "extreme_top",
                "dimension": dim, "dimension_cn": DIM_CN[dim],
                "part": part, "limit": n,
                "items": items,
            }
            _caveats = []
            if n_inf:
                # ★ 必须说明排在前面的原因，否则「∞ 排第一」看着像系统 bug
                _caveats.append(
                    f"榜单含 {n_inf} 项「∞（无限）」：源数据里它不是等级数字"
                    f"（数值列为空），此处按「高于 A 级」排在 A 之前。"
                    f"它无法与 A~E 直接比较，只表示「超出该刻度」。")
            if n_cond:
                # ★ 明说有几个带条件 —— 不加这句，榜单看起来全是硬实力
                _caveats.append(
                    f"该榜单有 {n_cond} 项带条件限定（已在条目上标注）："
                    f"这些值只在特定条件下成立，不代表无条件时的水平。")
            if _caveats:
                result["caveat"] = " ".join(_caveats)
            return (
                result,
                [{"type": "database", "table": "v_stand_overview",
                  "note": f"按 {dim} 降序（∞ 视为高于 A）；"
                          f"过滤 composite IS NOT NULL 或该维为 ∞"}
                 ] + ([{"type": "database", "table": "stand_stat_conditional",
                        "note": f"{n_cond} 项带条件限定（来自源数据 condition_note）"}]
                      if n_cond else []),
            )

        # count 形式
        if "多少个" in question or "几个" in question:
            lvl = extract_level(question)
            dim = dim or "PWR"
            if lvl is None:
                # 「有多少个替身」→ 计数
                if part:
                    cur.execute(
                        "SELECT count(*) FROM stands WHERE part = %s", (part,))
                else:
                    cur.execute("SELECT count(*) FROM stands")
                cnt = cur.fetchone()[0]
                return (
                    {"type": "count", "scope": f"第{part}部" if part else "全部",
                     "count": cnt},
                    [{"type": "database", "table": "stands"}],
                )
            sql = f"""
                SELECT name_en, part FROM v_stand_overview
                WHERE {dim.lower()} = %s
                {"" if part is None else "AND part = %s"}
                ORDER BY name_en
            """
            # ★ psycopg2 的 execute() 返回 None，要用 cur.fetchall() 取结果
            cur.execute(sql,
                        (lvl,) if part is None else (lvl, part))
            rows = cur.fetchall()
            return (
                {"type": "count_by_level", "dimension": dim,
                 "dimension_cn": DIM_CN[dim], "level": lvl,
                 "level_label": LEVEL_CN.get(lvl), "part": part,
                 "count": len(rows),
                 "items": [{"name": r[0], "part": r[1]} for r in rows]},
                [{"type": "database", "table": "v_stand_overview",
                  "note": f"{dim} = {lvl}"},
                 {"type": "conflict", "note":
                  "★ 数值已应用 M4 的冲突消解结果；若该维度存在未消解冲突，"
                  "cat 字段会是 UNKNOWN/EMPTY_SLOT"}],
            )

        # max/min
        agg = "ASC" if ("最低" in question or "最小" in question) else "DESC"
        word = "最低" if agg == "ASC" else "最高"
        dim = dim or "PWR"
        _d = dim.lower()
        if agg == "DESC":
            # ★★ M54：最高 —— 「∞（无限）」不是缺失，它比 A 更高 ★★
            #   以前 `{d} IS NOT NULL` 把它整行排除 → 「速度最高的是谁」
            #   漏掉速度无限的天堂制造。
            sql = f"""
                SELECT name_en, part, {_d}, {_d}_cat
                FROM v_stand_overview
                WHERE (composite IS NOT NULL OR {_d}_cat = 'INFINITE')
                  AND ({_d} IS NOT NULL OR {_d}_cat = 'INFINITE')
                {"" if part is None else "AND part = %s"}
                ORDER BY ({_d}_cat = 'INFINITE') DESC NULLS LAST,
                         {_d} DESC NULLS LAST, name_en
            """
        else:
            # ★ 最低：∞ 不是「最低」，保持排除（与既有行为一致）
            sql = f"""
                SELECT name_en, part, {_d}, {_d}_cat
                FROM v_stand_overview
                WHERE composite IS NOT NULL AND {_d} IS NOT NULL
                {"" if part is None else "AND part = %s"}
                ORDER BY {_d} ASC, name_en
            """
        cur.execute(sql, (part,) if part else ())
        rows = cur.fetchall()
        if not rows:
            return None, [{"note": "无满足条件的替身（六维不完整）"}]
        top, top_cat = rows[0][2], rows[0][3]
        # ★ M54：top 为 NULL 时只有一种可能 —— 它是 ∞（其余 NULL 已被 WHERE 排除），
        #   所以并列项要按 cat 判，不能按 `r[2] == top`（None == None 会误并）
        winners = ([r for r in rows if r[3] == "INFINITE"]
                   if top_cat == "INFINITE"
                   else [r for r in rows if r[2] == top])
        result = {
            "type": "extreme", "dimension": dim, "dimension_cn": DIM_CN[dim],
            "direction": word, "part": part, "value": top,
            # ★ M54：∞ 的 value 是 NULL → level_label 给 '∞'（不是 None → 前端「—」）
            "value_label": level_label(top, top_cat),
            "winners": [{"name": r[0], "part": r[1], "cat": r[3]}
                        for r in winners],
        }
        if top_cat == "INFINITE":
            result["caveat"] = (
                f"{DIM_CN[dim]}最高的是「∞（无限）」—— 它不是一个等级数字，"
                f"此处按「高于 A 级」列出；它无法与 A~E 直接比较。")
        return (
            result,
            [{"type": "database", "table": "v_stand_overview",
              "note": (f"ORDER BY {dim} {agg}；"
                       f"∞（无限）视为高于 A —— 它的数值列是空的，"
                       f"所以先按「是否 ∞」排、再按数值排"
                       if agg == "DESC" else
                       f"ORDER BY {dim} {agg}；已过滤 composite IS NOT NULL "
                       f"（★ 缺任一维时 composite 为 NULL，不代表能力低；"
                       f"∞ 也不参评「最低」）")}],
        )

    # ---------------------------------------------------------
    def _appearance(self, question: str, name2id: dict[str, str]):
        """★ M73：某替身的**出场章节 / 剧集**。

        M71 建了 `stand_appearances`（4902 条：漫画 2739 / 动画 1003 /
        仅封面 1160），并开了 `/appearances/{stand_id}` 端点，但
        **没有自然语言入口** —— 实测问「白金之星在哪些章节出场」会掉进
        语义检索，拿回一段原文介绍，而库里明明有结构化的章节列表。

        ★ 口径：
          · 漫画（manga）与动画（anime）**分开列**，不混成一张表。
          · `cover`（只出现在封面）**默认不列** —— 那不是"出场"，
            列进去会让「出场章节」变成用户不认的东西。
          · 章号是源站的**部内相对章号**（M71 的发现：第 6–9 部从 1 起），
            所以每条都回传 `raw`（源站原文，形如
            "Stone Ocean Chapter 12: The Visitor, Part 2"），
            前端展示原文，避免用户拿着 "ch12" 对不上漫画。
        """
        sid = extract_stand_name(question, name2id)
        if not sid:
            return None, [{"note": "未能识别替身名 —— 出场记录按替身查，"
                                   "例如「Star Platinum 在哪些章节出场」"}]
        cur = self.conn.cursor()
        cur.execute("SELECT name_en, part FROM stands WHERE stand_id = %s",
                    (sid,))
        head = cur.fetchone()
        if not head:
            return None, [{"note": f"{sid} 不在库中"}]
        cur.execute("""
            SELECT kind, chapter_no, episode_no, chapter_title, raw_text
            FROM stand_appearances
            WHERE stand_id = %s AND kind IN ('manga', 'anime')
            ORDER BY kind, chapter_no NULLS LAST, episode_no
        """, (sid,))
        manga: list[dict] = []
        anime: list[dict] = []
        for kind, ch, ep, title, raw in cur.fetchall():
            item = {"chapter": ch, "episode": ep,
                    "title": title, "raw": (raw or "")[:180]}
            (manga if kind == "manga" else anime).append(item)

        note = ("出场章节取自源站替身条目的 Appearance 段；漫画列话数、"
                "动画列集数，均附源站原文。「仅封面出场」不计入。")
        if not manga and not anime:
            note = "源站该替身条目里没有记录漫画/动画出场（只有封面出场或未列）。"
        return (
            {"type": "appearance", "stand_id": sid,
             "stand_name": head[0], "part": head[1],
             "n_manga": len(manga), "n_anime": len(anime),
             "manga": manga, "anime": anime, "note": note},
            [{"type": "database", "tables": ["stand_appearances"]}],
        )

    def _owner(self, question: str, name2id: dict[str, str]):
        sid = extract_stand_name(question, name2id)
        cur = self.conn.cursor()
        if sid:
            cur.execute("""
                SELECT s.name_en, s.part, s.owner_name_raw,
                       c.name_en, c.character_id, c.part
                FROM stands s LEFT JOIN characters c ON c.character_id = s.owner_id
                WHERE s.stand_id = %s
            """, (sid,))
            r = cur.fetchone()
            if not r:
                return None, [{"note": f"{sid} 不在库中"}]
            return (
                {"type": "owner", "stand_name": r[0], "part": r[1],
                 "owner": r[3], "owner_raw": r[2],
                 "character_id": r[4], "owner_part": r[5]},
                [{"type": "database", "tables": ["stands", "characters"]}],
            )

        # 反查：某使用者有哪些替身
        m = re.search(r"(?:使用者|持有者)\s*([A-Za-z][A-Za-z\s'’.]{2,40}?)\s*(?:有哪些|拥有|的)", question)
        if not m:
            return None, [{"note": "未能识别使用者"}]
        owner = m.group(1).strip()
        cur.execute("""
            SELECT s.name_en, s.part, s.stand_id
            FROM stands s JOIN characters c ON c.character_id = s.owner_id
            WHERE lower(c.name_en) = lower(%s)
            ORDER BY s.name_en
        """, (owner,))
        rows = cur.fetchall()
        return (
            {"type": "stands_of_owner", "owner": owner, "count": len(rows),
             "stands": [{"name": r[0], "part": r[1]} for r in rows]},
            [{"type": "database", "tables": ["stands", "characters"]}],
        )

    # ---------------------------------------------------------
    def _stand_of(self, question: str, name2id: dict[str, str]):
        """★★ M20：「XX 的替身是什么」→ 直接给**替身名**。

        ★ 为什么单独加这个意图（用户报的 bug）★
          用户问「东方常秀的替身是什么」，期望答案是「Soft & Wet」一个名字，
          但系统返回了 Soft & Wet 的**能力介绍长段原文**——
          既是别的角色（东方定助），又答非所问。
        → 问「哪个替身」就该走 SQL 直接给名字，不该走语义检索。

        角色名查得到就答；查不到返回 unknown，让上层拒答/给候选
        （而不是拿别的替身的长段介绍凑答案）。
        """
        m = INTENT_STAND_OF.search(question)
        owner = (m.group(1) if m else "").strip()
        # ★ 主体可能含**内部空格**（'Jotaro Kujo'），
        #   之前用 re.split(r"[是谁的\s]") 会在第一个空格处截断
        #   → owner 变成 "Jotaro"，显示时不完整。
        #   → 只在「已知噪声词」处截断，空格保留。
        owner = re.split(r"[是谁的]", owner)[0].strip()
        if not owner:
            return self._unknown(question, name2id)

        # ★★ M21：先看是不是**角色中文名**（如「东方定助」「空条承太郎」）★
        #   为什么需要（用户报的 bug）：角色表只有英文名，
        #   中文问句「东方定助的替身是什么」永远查不到。
        #   → 用 fetch_zh_characters.py 抓到的角色中文名反查 character_id。
        cur = self.conn.cursor()
        try:
            from aliases import build_char_alias_table
            char_zh = build_char_alias_table()
        except Exception:  # noqa: BLE001
            char_zh = {}
        cid = char_zh.get(owner) or char_zh.get(
            re.sub(r"[\s·・]", "", owner))
        if cid:
            cur.execute("""
                SELECT s.name_en, s.name_ja, s.part, s.stand_id
                FROM stands s WHERE s.owner_id = %s
                ORDER BY s.name_en
            """, (cid,))
            rows = cur.fetchall()
            if rows:
                return (
                    {"type": "stand_of", "owner": owner, "count": len(rows),
                     "stands": [{"name": r[0], "ja": r[1], "part": r[2],
                                 "stand_id": r[3], "zh": _stand_zh(r[3])}
                                for r in rows],
                     "answer": "、".join(r[0] for r in rows)},
                    [{"type": "database",
                      "tables": ["stands", "characters"]}],
                )

        # ★ 英文名路径：角色表只有英文名（实测 144 个角色，中文名 0 个）
        #   所以先试英文精确匹配 → 再试"名字包含"
        cur.execute("""
            SELECT s.name_en, s.name_ja, s.part, s.stand_id
            FROM stands s JOIN characters c ON c.character_id = s.owner_id
            WHERE lower(c.name_en) = lower(%s)
            ORDER BY s.name_en
        """, (owner,))
        rows = cur.fetchall()
        if not rows:
            cur.execute("""
                SELECT s.name_en, s.name_ja, s.part, s.stand_id
                FROM stands s JOIN characters c ON c.character_id = s.owner_id
                WHERE lower(c.name_en) LIKE lower(%s)
                ORDER BY s.name_en LIMIT 8
            """, (f"%{owner}%",))
            rows = cur.fetchall()
        if not rows:
            return self._unknown(question, name2id)

        return (
            {"type": "stand_of", "owner": owner, "count": len(rows),
             # ★ 只要名字，不要长段介绍 —— 这是用户明确要的
             # ★ M25：带上 stand_id/zh —— 多轮追问要据此建立上下文
             #   （不带的话「那它的速度呢」只能拿到角色名，代词会指错）
             "stands": [{"name": r[0], "ja": r[1], "part": r[2],
                         "stand_id": r[3], "zh": _stand_zh(r[3])}
                        for r in rows],
             "answer": "、".join(r[0] for r in rows)},
            [{"type": "database", "tables": ["stands", "characters"]}],
        )

    # ---------------------------------------------------------
    def _forms(self, question: str, name2id: dict[str, str]):
        sid = extract_stand_name(question, name2id)
        cur = self.conn.cursor()
        if not sid:
            cur.execute("""
                SELECT s.name_en, count(f.form_id)
                FROM stands s JOIN stand_forms f ON f.stand_id = s.stand_id
                GROUP BY s.name_en HAVING count(f.form_id) > 1
                ORDER BY count(f.form_id) DESC, s.name_en
            """)
            rows = cur.fetchall()
            return (
                {"type": "multi_form_stands", "count": len(rows),
                 "items": [{"name": r[0], "n_forms": r[1]} for r in rows]},
                [{"type": "database", "table": "stand_forms"}],
            )
        cur.execute("""
            SELECT form_id, form_name, form_type,
                   pwr, spd, rng, sta, prc, dev, pwr_raw, rng_raw
            FROM stand_forms WHERE stand_id = %s ORDER BY raw_order
        """, (sid,))
        rows = cur.fetchall()
        # ★ 反查替身名（name2id 是 name → id，需反向查）
        name = next((n for n, i in name2id.items() if i == sid), sid)
        return (
            {"type": "forms", "stand_id": sid, "stand_name": name,
             "count": len(rows),
             "items": [{
                 "form_id": r[0], "form_name": r[1], "form_type": r[2],
                 "dims": {d: {"value": r[3 + i],
                              "label": LEVEL_CN.get(r[3 + i])}
                          for i, d in enumerate(DIMS)},
                 "pwr_raw": r[9], "rng_raw": r[10],
             } for r in rows]},
            [{"type": "database", "table": "stand_forms"}],
        )

    # ---------------------------------------------------------
    def _part_list(self, question: str, name2id: dict[str, str]):
        """★ 列出某部的全部替身（「第九部有哪些替身」）。

        实测缺失：这类问法既不含 INTENT_EXTREME 的词，也不含「多少个」，
        detect_intent 原会落到 unknown → 退回语义检索，
        返回一堆无关 wiki 片段 —— 补进去的第 9 部替身**完全查不到**。
        """
        cur = self.conn.cursor()
        mp = INTENT_PART.search(question)
        part = part_to_int(mp.group(2)) if mp else None
        if part is None:
            return self._unknown(question, name2id)
        cur.execute(
            "SELECT name_en, stand_id, name_ja FROM stands "
            "WHERE part = %s ORDER BY name_en", (part,))
        rows = cur.fetchall()
        return (
            {"type": "part_list", "part": part, "count": len(rows),
             "items": [{"name": r[0], "stand_id": r[1], "name_ja": r[2]}
                       for r in rows]},
            [{"type": "database", "table": "stands", "note": f"part = {part}"}],
        )

    # ---------------------------------------------------------
    def _part_count(self, question: str, name2id: dict[str, str]):
        cur = self.conn.cursor()
        cur.execute("""
            SELECT part, max(part_name_en), count(*),
                   count(composite), round(avg(composite), 2)
            FROM v_stand_overview WHERE part IS NOT NULL
            GROUP BY part ORDER BY part
        """)
        rows = cur.fetchall()
        # ★ avg() 返回 Decimal，FastAPI/Pydantic 无法序列化 → 转 float
        rows = [(r[0], r[1], r[2], r[3],
                 float(r[4]) if r[4] is not None else None) for r in rows]
        # ★ execute() 返回 None，要用 cur.fetchone()
        cur.execute("SELECT count(*) FROM stands WHERE part IS NULL")
        total = cur.fetchone()[0]
        return (
            {"type": "part_stats",
             "parts": [{"part": r[0], "name": r[1], "n_stands": r[2],
                        "n_with_composite": r[3],
                        "avg_composite": r[4]} for r in rows],
             "n_unknown_part": total},
            [{"type": "database", "table": "v_part_stats"}],
        )

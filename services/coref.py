"""多轮追问的指代消解（M25）。

解决的问题
----------
原来 `/query` 是**完全无状态**的 —— 每一轮都当新问题。
所以「白金之星的破坏力是几级？」→「那**它**的速度呢？」，
第二轮里的「它」没人知道指谁，路由只能当成了个含糊问句。

实测的失败形态（就是用户报的那种）：
    第 1 轮  「空条承太郎的替身是什么」 → Star Platinum
    第 2 轮  「那它的速度呢」           → 认不出「它」→ 答非所问

做法
----
在 `router.route()` **之前**把代词换成上一轮的实体，
得到 `resolved_q`，下游（路由/执行/检索）看到的已经是个完整问句
—— 对下游完全透明，不用改任何一行执行器代码。

    q = "那它的速度呢"
    resolved_q = "那Star Platinum的速度呢"
    router.route(resolved_q)     # 走 structured，正常作答

★★ 三个必须小心的地方（都是中文特有的坑）★★

1. **代词误伤**：`"其他"` 里含 `"他"`、`"尤其"` 里含 `"其"`、
   `"其实"`/`"其中"`/`"其余"` 同理。
   实测：不做排除的话，「其他替身有哪些」会被替换成
   「其Star Platinum」这种句子 —— 反而更糟。
   → 先屏蔽排除词再匹配。

2. **本轮自带实体时不替换**：
   「Star Platinum 的速度呢」后面接「白金之星的破坏力呢」——
   第二轮虽然也有上下文，但它**自己说了新实体**，不该被替换。
   → 只有「有代词」且「本轮抽不到任何实体」时才启用。

3. **只替换第一个代词**（`count=1`）：
   「它和它的替身」这种句子罕见，替换两次容易出乱子。

会话状态
--------
存在进程内存里（`STATE["sessions"]`），带 TTL。
★ 为什么不做持久化：本项目是单机演示服务，
  重启丢上下文是**可接受**的；而引入 Redis/SQLite 会凭空多一层依赖。
  TTL 是为了防止长时间运行把内存堆满。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------
# 指代词表
# ---------------------------------------------------------------
# ★ 顺序有讲究：**长词在前**，这样「这个替身」会先于「这个」匹配，
#   替换后语义更完整（「这个替身」→ 名字，而不是把「这个」换掉留下「替身」）。
PRONOUNS: tuple[str, ...] = (
    # 指示代词 + 名词（更明确）
    "这个替身", "那个替身", "该替身", "此替身", "这替身",
    "这个角色", "那个角色", "该角色", "此角色",
    "这个能力", "那个能力", "该能力",
    "这位", "那位",
    # 人称代词
    "它", "他", "她", "祂",
    # 指示代词（短）
    "这个", "那个", "该", "此", "其",
)

# ★★ 必须排除的「伪代词」★★
#   中文里这些词含"他/其"但不是指代，误替换会造出病句。
#   实测形态：「其他替身有哪些」→ 若替换会变成「其Star Platinum替身有哪些」。
_FALSE_POSITIVE = re.compile(
    r"其他|其它|其中|尤其|其次|其间|其余|其实|具体|其它|"
    r"各其他|尤其|极其|与其|及其|不其|"
    r"彼此|乃至|所谓|之类"
)

# 代词匹配正则：长词优先（把 PRONOUNS 按长度倒序拼）
_PRONOUN_RE = re.compile(
    "|".join(re.escape(p) for p in sorted(PRONOUNS, key=len, reverse=True))
)


@dataclass
class TurnContext:
    """一轮对话留下的上下文。

    两个实体都记（而不是只记一个）是为了处理跨类型追问：
      第 1 轮「东方常秀的替身是什么」  → 角色=东方常秀, 替身=Nut King Call
      第 2 轮「它有什么能力」          → 「它」指替身
      第 2 轮「他还有别的替身吗」      → 「他」指角色
    """
    stand_id: Optional[str] = None
    stand_name: Optional[str] = None     # 用**用户写的那个写法**回填，更自然
    char_id: Optional[str] = None
    char_name: Optional[str] = None
    question: str = ""
    ts: float = field(default_factory=time.time)

    def entity_for(self, pronoun: str) -> Optional[str]:
        """按代词选实体：'他/她' 优先指角色，'它' 优先指替身。

        ★ 依据：中文里「他/她」指人，替身多为无生命物 → 用「它」。
          这比对所有代词都用同一个实体更符合语感。
        """
        if pronoun in ("他", "她", "祂", "这位", "那位"):
            return self.char_name or self.stand_name
        if pronoun.startswith("这个角色") or pronoun.startswith("那个角色") \
                or pronoun.startswith("该角色"):
            return self.char_name or self.stand_name
        return self.stand_name or self.char_name


class SessionStore:
    """会话上下文存储（内存 + TTL）。"""

    def __init__(self, ttl_sec: int = 1800, max_sessions: int = 500):
        self._data: dict[str, TurnContext] = {}
        self.ttl = ttl_sec
        self.max_sessions = max_sessions
        self.n_hits = 0
        self.n_misses = 0

    # ---------------- 存取 ----------------
    def get(self, sid: Optional[str]) -> Optional[TurnContext]:
        if not sid:
            self.n_misses += 1
            return None
        ctx = self._data.get(sid)
        if ctx is None:
            self.n_misses += 1
            return None
        if time.time() - ctx.ts > self.ttl:
            # 过期 → 当作没有上下文
            self._data.pop(sid, None)
            self.n_misses += 1
            return None
        self.n_hits += 1
        return ctx

    def put(self, sid: Optional[str], ctx: TurnContext) -> None:
        if not sid:
            return
        self._data[sid] = ctx
        self._evict_if_needed()

    def clear(self, sid: Optional[str]) -> bool:
        return self._data.pop(sid, None) is not None if sid else False

    def _evict_if_needed(self) -> None:
        """超量时先清过期，再按最旧淘汰。"""
        if len(self._data) <= self.max_sessions:
            return
        now = time.time()
        for k in [k for k, v in self._data.items()
                  if now - v.ts > self.ttl]:
            self._data.pop(k, None)
        if len(self._data) > self.max_sessions:
            oldest = sorted(self._data.items(), key=lambda kv: kv[1].ts)
            for k, _ in oldest[: len(self._data) - self.max_sessions]:
                self._data.pop(k, None)

    def stats(self) -> dict:
        return {"sessions": len(self._data), "ttl_sec": self.ttl,
                "hits": self.n_hits, "misses": self.n_misses}


def find_pronoun(q: str) -> Optional[str]:
    """找问句里的指代词。找不到返回 None。

    ★ 先屏蔽「伪代词」（其他/其中/尤其…），否则「其他」里的「他」
      会被当成代词 —— 实测会把句子替换成病句。
    """
    masked = _FALSE_POSITIVE.sub(lambda m: "〇" * len(m.group()), q)
    m = _PRONOUN_RE.search(masked)
    return m.group() if m else None


def resolve(q: str, session_id: Optional[str], store: SessionStore,
            guess_stand, guess_char=None
            ) -> tuple[str, Optional[dict], Optional[TurnContext]]:
    """把问句里的代词换成上一轮实体。

    Args:
        q: 本轮问句
        session_id: 会话标识（前端传）
        store: 会话存储
        guess_stand: 抽替身的函数（复用 api 的 _guess_stand，签名 (q) -> id|None）
        guess_char: 抽角色的函数（(q) -> id|None），可选

    Returns:
        (resolved_q, coref_info, prev_ctx)
        coref_info 为 None 表示**没有发生替换**（无上下文 / 无代词 / 本轮自带实体）
        —— 这个字段会原样返回给前端，让用户看见系统把「它」理解成了什么。
    """
    ctx = store.get(session_id)
    if ctx is None:
        return q, None, None

    pron = find_pronoun(q)
    if not pron:
        return q, None, ctx

    # ★ 本轮自己带了实体 → 不替换（否则「白金之星的破坏力呢」会被上一轮污染）
    #   替身与角色都要查：用户可能报角色名追问
    if guess_stand(q) is not None:
        return q, None, ctx
    if guess_char is not None and guess_char(q) is not None:
        return q, None, ctx

    target = ctx.entity_for(pron)
    if not target:
        return q, None, ctx

    resolved = q.replace(pron, target, 1)
    if resolved == q:            # 保险：没替换成功就原样返回
        return q, None, ctx

    return resolved, {
        "pronoun": pron,
        "resolved_to": target,
        "from_question": ctx.question,
        "kind": "stand" if target == ctx.stand_name else "character",
    }, ctx


def build_context(question: str,
                  stand_id: Optional[str],
                  stand_name: Optional[str],
                  char_id: Optional[str] = None,
                  char_name: Optional[str] = None,
                  prev: Optional[TurnContext] = None) -> TurnContext:
    """构造本轮上下文。本轮没识别出实体时**继承上一轮**。

    ★ 为什么继承：用户可能连着追问两次
      「它的速度呢」→「那它的射程呢」，
      第二轮问题里同样没有实体，若直接覆盖成 None，
      上下文就断了。
    """
    ctx = TurnContext(
        stand_id=stand_id, stand_name=stand_name,
        char_id=char_id, char_name=char_name,
        question=question,
    )
    if prev is not None:
        # 只在**本轮缺失**的字段上继承
        if ctx.stand_id is None:
            ctx.stand_id, ctx.stand_name = prev.stand_id, prev.stand_name
        if ctx.char_id is None:
            ctx.char_id, ctx.char_name = prev.char_id, prev.char_name
    return ctx

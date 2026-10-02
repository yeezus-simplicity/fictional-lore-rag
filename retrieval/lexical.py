"""
检索层（M2-2 基线 / M3 实验共用）。

★ 本文件实现**不依赖 torch / embedding 模型**的纯词法检索：
  - BM25（Okapi）
  - 与 BM25 的混合融合（RRF）
  - 结构化路径的模拟器（Oracle / 规则路由）

为什么先做纯词法：
  1. M2 基线不应被「embedding 装不上」阻塞
  2. **纯 BM25 本身就是 D2 决策点的对照组**——
     没有它就无法证明「混合检索比纯向量好」
  3. 零依赖，秒级重跑，适合做参数扫描

待 M3 补：向量检索 + Rerank + 融合权重扫描。
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

PROC = Path(__file__).resolve().parents[1] / "dataset" / "processed"


# ==================================================================
# 分词
# ==================================================================

# 高频虚词，无检索价值
STOPWORDS = set("""
a an the of and or to in on at for with by from is are was were be been being
this that these those it its as but if then than so such not no nor do does did
have has had can could will would shall should may might must i you he she they
we me him her them my your his their our what which who whom whose when where
why how all any both each few more most other some only own same too very
""".split())

CJK = re.compile(r"[\u4e00-\u9fff]")
TOKEN = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    """分词：英文按词，中文按字bigram + 单字。

    数据以英文为主（jojowiki），中文只用于问题中的维度名等。
    中文按单字切会过度碎片化，采用「单字 + bigram」双粒度。
    """
    if not text:
        return []
    s = unicodedata.normalize("NFKC", str(text)).lower()
    s = s.replace("\xa0", " ")
    toks: list[str] = [t for t in TOKEN.findall(s) if t not in STOPWORDS and len(t) > 1]

    # 中文：单字 + bigram
    cjk = CJK.findall(s)
    toks.extend(cjk)
    for i in range(len(cjk) - 1):
        toks.append(cjk[i] + cjk[i + 1])
    return toks


def tokenize_field(text: str) -> list[str]:
    """同tokenize，保留为公开别名（语义更清晰）。"""
    return tokenize(text)


# ==================================================================
# BM25 索引
# ==================================================================

@dataclass
class BM25Index:
    """Okapi BM25 倒排索引。"""

    docs: list[dict]                      # 原始文档（text_chunks 行）
    doc_ids: list[int]                    # 与 docs 对齐的 chunk_id
    k1: float = 1.5
    b: float = 0.75

    # 倒排：token -> [(doc_idx, tf), ...]
    inverted: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    doc_len: list[int] = field(default_factory=list)
    avg_len: float = 0.0
    df: dict[str, int] = field(default_factory=dict)
    _n: int = 0

    def __post_init__(self):
        self._n = len(self.docs)
        for i, d in enumerate(self.docs):
            # 检索文本：正文 + 实体名 + 小节名（字段加权靠重复拼接）
            parts = [d.get("content", "")]
            if d.get("entity"):
                parts.append(d["entity"] * 2)          # 实体名加权
            if d.get("stand_name"):
                parts.append(d["stand_name"] * 2)      # 替身名加权
            if d.get("section"):
                parts.append(d["section"])
            toks = tokenize(" ".join(parts))
            self.doc_len.append(len(toks))
            tf = Counter(toks)
            for t, c in tf.items():
                self.inverted.setdefault(t, []).append((i, c))
        self.avg_len = (sum(self.doc_len) / self._n) if self._n else 0.0
        self.df = {t: len(post) for t, post in self.inverted.items()}

    def idf(self, term: str) -> float:
        n = self._n
        d = self.df.get(term, 0)
        if d == 0:
            return 0.0
        return math.log(1 + (n - d + 0.5) / (d + 0.5))

    def search(self, query: str, top_k: int = 20,
               boost_stand: Optional[str] = None) -> list[tuple[int, float]]:
        """返回 [(chunk_id, score)]，按分数降序。"""
        q = tokenize(query)
        if not q:
            return []
        scores: dict[int, float] = defaultdict(float)
        for term in q:
            post = self.inverted.get(term)
            if not post:
                continue
            w = self.idf(term)
            for i, tf in post:
                dl = self.doc_len[i] or 1
                denom = tf + self.k1 * (1 - self.b + self.b * dl / (self.avg_len or 1))
                scores[i] += w * (tf * (self.k1 + 1)) / denom

        # 替身名精确匹配加权：查询里出现替身名则该文档大幅加权
        if boost_stand:
            for i, d in enumerate(self.docs):
                if d.get("stand_id") == boost_stand:
                    scores[i] += 6.0

        ranked = sorted(scores.items(), key=lambda x: -x[1])[:top_k]
        return [(self.doc_ids[i], s) for i, s in ranked]


# ==================================================================
# RRF 融合
# ==================================================================

def rrf_fuse(rankings: list[list[tuple[int, float]]],
             k: int = 60, top_k: int = 20) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion。

    Args:
        rankings: 多路排序结果，每项为 [(doc_id, score)]
        k: RRF 常数，业界惯例 60
    """
    acc: dict[int, float] = defaultdict(float)
    for ranking in rankings:
        for rank, (doc_id, _) in enumerate(ranking, 1):
            acc[doc_id] += 1.0 / (k + rank)
    ranked = sorted(acc.items(), key=lambda x: -x[1])[:top_k]
    return ranked


# ==================================================================
# 检索数据集
# ==================================================================

@dataclass
class RetrievalCorpus:
    """检索语料：文本块 + 结构化索引。"""

    chunks: list[dict]
    stands: list[dict]
    stats: list[dict]
    bm25: BM25Index

    @classmethod
    def load(cls, chunks_file: str = "text_chunks.json") -> "RetrievalCorpus":
        chunks = json.loads((PROC / chunks_file).read_text(encoding="utf-8"))
        stands = json.loads((PROC / "stands.json").read_text(encoding="utf-8"))
        stats = json.loads((PROC / "stand_stats.json").read_text(encoding="utf-8"))
        bm25 = BM25Index(
            docs=chunks,
            doc_ids=[c["chunk_id"] for c in chunks],
        )
        return cls(chunks=chunks, stands=stands, stats=stats, bm25=bm25)

    def index_stands(self) -> None:
        self.sinfo = {s["stand_id"]: s for s in self.stands}
        self.tinfo = {s["stand_id"]: s for s in self.stats}
        # stand_name 小写 → stand_id（用于从查询里识别替身名）
        self.name2id: dict[str, str] = {}
        for s in self.stands:
            nm = (s.get("name_en") or "").strip().lower()
            if nm:
                self.name2id[nm] = s["stand_id"]

    def guess_stand(self, question: str) -> Optional[str]:
        """从查询里识别替身名（长名优先，避免误匹配短名）。"""
        q = question.lower()
        for nm, sid in sorted(self.name2id.items(), key=lambda x: -len(x[0])):
            if nm and nm in q:
                return sid
        return None


# ==================================================================
# 路由：判断查询该走哪条路径
# ==================================================================

# 结构化关键词 → 意图
STRUCT_PATTERNS = {
    "extreme": re.compile(
        r"(最高|最大|最强|最低|最小|最弱|多少个|几个|排名|排序|total|最高等级|"
        r" 综合|总分|同时满足|达到)"),
    "fact": re.compile(
        r"(是几级|多少级|是多少|哪一部|第几部|使用者是谁|有几个形态|有哪些替身|"
        r" 叫什么|能力值|破坏力|速度|射程|持续力|精密性|成长性)"),
    "multi_hop": re.compile(r"(使用者是谁.*第|出现在哪几部|形态链|有几个形态)"),
}

# 语义关键词
SEMANTIC_PATTERNS = re.compile(
    r"(描述|介绍|概述|能力说明|历史|性格|外观|来源|设定|怎么|如何|招式|"
    r" 运作|方面有哪些|是什么|appearance|personality|history)", re.I)


def classify_route(question: str, gold_chunks: list[int],
                   corpus: Optional[RetrievalCorpus] = None) -> str:
    """规则路由（基线用；M4 会换成小模型分类对比）。

    ★ 当前实现是「看 gold 证据推断 oracle 路由」，
      用于给出路由准确率的上界参考。真正的规则路由只看问题文本。
    """
    if not gold_chunks:
        return "structured"
    return "semantic"


def route_by_rules(question: str) -> str:
    """纯规则路由：只看问题文本，判断期望路径。"""
    q = question.strip()
    # 无答案探测：问的是不存在的实体
    if re.search(r"(未公布|第 ?9 ?部|是否官方认证|具体是几)", q):
        return "abstain"
    if SEMANTIC_PATTERNS.search(q) and not STRUCT_PATTERNS["extreme"].search(q):
        # 语义词 + 无极值词→ 语义
        if not re.search(r"(是几级|多少级|第几部|使用者是谁)", q):
            return "semantic"
    if STRUCT_PATTERNS["extreme"].search(q):
        return "structured"
    if re.search(r"(是几级|多少级|是多少|第几部|使用者是谁|有哪些替身|"
                 r"有几个形态|能力值)", q):
        return "structured"
    if re.search(r"(描述|概述|历史|性格|外观|来源|设定|招式|怎么|如何|运作)", q):
        return "semantic"
    return "structured"


# ==================================================================
if __name__ == "__main__":
    import sys
    print("加载语料…")
    corpus = RetrievalCorpus.load()
    corpus.index_stands()
    print(f"  文档 {len(corpus.chunks)}，替身 {len(corpus.stands)}")
    print(f"  倒排词项 {len(corpus.bm25.inverted)}")
    print(f"  平均文档长度 {corpus.bm25.avg_len:.1f} token")

    for q in [
        "Star Platinum 的破坏力是几级？",
        "所有替身中，破坏力最高的是哪个？",
        "Anubis 的外观形态方面有哪些描述？",
        "Ora Ora 是怎么运作的？",
    ]:
        sid = corpus.guess_stand(q)
        res = corpus.bm25.search(q, top_k=5, boost_stand=sid)
        print(f"\n  Q: {q}")
        print(f"     识别替身: {sid}  路由: {route_by_rules(q)}")
        for cid, sc in res[:3]:
            c = next((x for x in corpus.chunks if x["chunk_id"] == cid), {})
            print(f"     #{cid} {sc:6.3f} [{c.get('chunk_type','')}] "
                  f"{c.get('content','')[:56]}")

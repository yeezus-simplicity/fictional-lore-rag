"""
语义检索执行器（M5）。

M4 路由到 semantic / hybrid 后由本模块负责。
直接复用 M3 的检索层（BM25 + bge-m3 + RRF），不重写。

★ 关键设计：**答案必须是原文片段，不是生成文本**。
  抽取式回答的价值在于「零幻觉」—— 返回的内容就是数据源里的原话。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
PROC = ROOT / "dataset" / "processed"


class SemanticExecutor:
    """语义检索执行器。懒加载索引（首次调用时才建向量索引）。"""

    def __init__(self, use_vector: bool = True, verbose: bool = False):
        self.use_vector = use_vector
        self.verbose = verbose
        self._chunks: Optional[list] = None
        self._bm25 = None
        self._vec = None
        self._embedder = None
        self._chunk_map: Optional[dict] = None
        self._loaded = False

    # ---------------------------------------------------------
    def load(self) -> bool:
        """懒加载。返回是否成功启用向量检索。"""
        if self._loaded:
            return self._vec is not None
        self._loaded = True
        try:
            self._chunks = json.loads(
                (PROC / "text_chunks.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            print("[semantic] text_chunks.json 缺失，语义检索不可用")
            return False

        self._chunk_map = {c["chunk_id"]: c for c in self._chunks}
        sys.path.insert(0, str(ROOT / "retrieval"))
        from lexical import BM25Index
        self._bm25 = BM25Index(
            docs=self._chunks,
            doc_ids=[c["chunk_id"] for c in self._chunks],
            k1=1.2, b=0.5,          # M3 实测最优参数
        )
        if not self.use_vector:
            return False
        try:
            from dense import build_index, DenseEmbedder
            self._embedder = DenseEmbedder()
            if self._embedder.model is None:
                if self.verbose:
                    print(f"[semantic] 向量不可用（{self._embedder._load_error}）")
                return False
            self._vec = build_index(self._chunks, backend="dense",
                                    cache_name="m3_bge_m3", verbose=self.verbose)
            return True
        except Exception as e:
            if self.verbose:
                print(f"[semantic] 向量索引构建失败：{type(e).__name__}: {e}")
            return False

    # ---------------------------------------------------------
    def search(self, question: str, top_k: int = 5,
               boost_stand: Optional[str] = None
               ) -> tuple[list[dict], list[dict]]:
        """检索并返回 (证据片段, 检索元信息)。

        ★ 返回的是**原文片段 + 出处**，不做任何改写。
        """
        if not self._loaded and not self.load():
            return [], [{"error": "语义检索不可用"}]

        from lexical import rrf_fuse

        rankings = [self._bm25.search(question, top_k=max(top_k * 4, 20),
                                      boost_stand=boost_stand)]
        if self._vec is not None and self._embedder is not None:
            qv = self._embedder.encode([question])[0]
            rankings.append(self._vec.search(qv, top_k=max(top_k * 4, 20)))

        if len(rankings) == 1:
            fused = rankings[0][:top_k]
        else:
            # k=30 是 M3 组合实验的最优值
            fused = rrf_fuse(rankings, k=30, top_k=top_k)

        evidence = []
        for cid, score in fused:
            c = self._chunk_map.get(cid)
            if not c:
                continue
            evidence.append({
                "chunk_id": cid,
                "stand_id": c.get("stand_id"),
                "stand_name": c.get("stand_name"),
                "part": c.get("part"),
                "chunk_type": c.get("chunk_type"),
                "section": c.get("section"),
                "entity": c.get("entity"),
                "content": c.get("content"),
                "retrieval_score": round(float(score), 4),
                "source_url": c.get("source_url"),
            })

        meta = [{
            "retrieval": "hybrid_rrf" if len(rankings) > 1 else "bm25",
            "rrf_k": 30 if len(rankings) > 1 else None,
            "n_candidates": sum(len(r) for r in rankings),
            "vector_enabled": self._vec is not None,
            "bm25_params": {"k1": 1.2, "b": 0.5},
        }]
        return evidence, meta

    # ---------------------------------------------------------
    def keyword_snippets(self, question: str, stand_id: str,
                         limit: int = 2) -> list[str]:
        """按关键词从指定替身的块里抽最相关的句子。

        用于「hybrid」路由：结构化给数值，语义补描述。
        """
        if self._chunks is None and not self.load():
            return []
        # 抽问句里的实词
        words = [w.lower() for w in re.findall(r"[A-Za-z]{3,}", question)]
        words = [w for w in words if w not in ("the", "and", "for", "with")][:8]
        if not words:
            return []

        scored = []
        for c in self._chunks:
            if c.get("stand_id") != stand_id:
                continue
            text = c.get("content", "")
            low = text.lower()
            hit = sum(low.count(w) for w in words)
            if hit:
                scored.append((hit, len(text), c))
        scored.sort(key=lambda x: (-x[0], x[1]))

        out = []
        for _, _, c in scored[:limit]:
            # 抽取包含最多关键词的句子
            sents = re.split(r"(?<=[.!?])\s+", c["content"])
            best, best_hit = "", -1
            for s in sents:
                h = sum(s.lower().count(w) for w in words)
                if h > best_hit:
                    best, best_hit = s, h
            if best:
                out.append(best.strip()[:400])
        return out

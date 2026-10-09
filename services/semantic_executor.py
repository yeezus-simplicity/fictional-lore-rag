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

    def __init__(self,
                 use_vector: bool = True,
                 verbose: bool = False,
                 chunk_merge_target: Optional[int] = None):
        """
        Args:
            use_vector: 是否启用向量检索（False = 纯 BM25，秒级）
            verbose: 打印诊断信息
            chunk_merge_target:
                ★ M12：语义块合并目标长度（字符）。
                None / 0 → **不合并**（默认，保持 M6–M11 的历史行为）。
                512     → 合并到 512（M9 实测推荐）。

                为什么默认不合并：M6/M7/M9 的评测数据都是在
                **不合并**的索引上跑出来的，改默认值会让历史结论不可复现。
        """
        self.use_vector = use_vector
        self.verbose = verbose
        self.chunk_merge_target = chunk_merge_target or None
        self._chunks: Optional[list] = None
        self._bm25 = None
        self._vec = None
        self._embedder = None
        self._chunk_map: Optional[dict] = None
        self._src_index: Optional[dict] = None   # ★ 原始 id → 合并块
        self._loaded = False

    # ---------------------------------------------------------
    def load(self) -> bool:
        """懒加载。返回是否成功启用向量检索。"""
        if self._loaded:
            return self._vec is not None
        self._loaded = True
        try:
            sys.path.insert(0, str(ROOT / "retrieval"))
            from merging import build_chunks, index_stats
            self._chunks = build_chunks(self.chunk_merge_target)
        except FileNotFoundError:
            print("[semantic] text_chunks.json 缺失，语义检索不可用")
            return False
        except ImportError:
            # 没有 merging 模块时退回原始行为（向后兼容）
            self._chunks = json.loads(
                (PROC / "text_chunks.json").read_text(encoding="utf-8"))

        # ★ M12：记录合并统计与溯源索引
        if self.chunk_merge_target:
            st = index_stats(self._chunks)
            print(f"[semantic] 块合并 {self.chunk_merge_target}："
                  f"{st['n_chunks']} 块（均 {st['mean_chars']:.0f} 字，"
                  f"{st['n_merged']} 个由多块合并）")
            self._src_index = {}
            for c in self._chunks:
                for bid in c.get("base_chunk_ids", [c["chunk_id"]]):
                    self._src_index[bid] = c

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
            self._vec = build_index(
                self._chunks, backend="dense",
                # ★★ 缓存 key 必须含合并参数 ——
                #   否则 512 模式会读到「不合并」的旧缓存，
                #   或反过来（实测踩过：缓存是按 chunk_id 列表算的）
                cache_name=(f"m3_bge_m3_merge{self.chunk_merge_target}"
                            if self.chunk_merge_target else "m3_bge_m3"),
                verbose=self.verbose)
            return True
        except Exception as e:
            if self.verbose:
                print(f"[semantic] 向量索引构建失败：{type(e).__name__}: {e}")
            return False

    def trace_to_base(self, chunk_id: int) -> list[int]:
        """★ M12：把合并块的 id 展开成原始 chunk_id 列表。

        界面上展示证据时用 —— 用户点开一条证据，
        应该能看到它是由哪几个原始块合并来的。
        """
        c = self._chunk_map.get(chunk_id)
        if not c:
            return []
        return c.get("base_chunk_ids", [chunk_id])

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
                # ★ M12：合并信息 —— 界面上显示「由 N 个原始块合并」
                #   base_chunk_ids 让证据可回溯到原始块（见 trace_to_base）
                "n_base_blocks": c.get("n_base_blocks", 1),
                "base_chunk_ids": c.get("base_chunk_ids", [cid]),
                "merged": bool(c.get("merged", False)),
                "n_chars": c.get("n_chars", len(c.get("content", ""))),
            })

        meta = [{
            "retrieval": "hybrid_rrf" if len(rankings) > 1 else "bm25",
            "rrf_k": 30 if len(rankings) > 1 else None,
            "n_candidates": sum(len(r) for r in rankings),
            "vector_enabled": self._vec is not None,
            # ★ M12：把当前索引配置写进 meta ——
            #   界面要能显示「当前用的是什么索引」，否则用户无法判断结果差异
            "chunk_merge_target": self.chunk_merge_target,
            "n_chunks": len(self._chunks),
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

    # ============================================================
    # M16：实体锚定检索
    # ============================================================
    def stand_chunks(self, stand_id: str, top_k: int = 3) -> list[dict]:
        """取出某个替身自己的正文块（优先 overview / ability）。

        ★ 为什么不能用 keyword_snippets 做这件事：
          它只用 `[A-Za-z]{3,}` 抽英文词打分 →
          中文问句（「骇游天外的能力是什么」）抽出 words=[] → 直接返回 []
          → 又退回全库检索 → 又返回别的替身。
          而语料（jojowiki 抓的正文）本来就是**英文**的，
          所以这里不该用问句打分，而该**按 stand_id 直接取**。

        ★ 排序规则（不依赖问句）：
          1. chunk_type 优先级：ability_overview > appearance > 其他
          2. 同类型下，内容越长信息越全（合并块更完整）
        → 对「这个替身是什么」这类问句，返回它自己的描述就对了。
        """
        if self._chunks is None and not self.load():
            return []

        prio = {"ability_overview": 0, "appearance": 1}
        mine = [c for c in self._chunks
                if c.get("stand_id") == stand_id and c.get("content")]
        if not mine:
            return []
        mine.sort(key=lambda c: (prio.get(c.get("chunk_type"), 2),
                                 -len(c["content"])))
        return mine[:top_k]

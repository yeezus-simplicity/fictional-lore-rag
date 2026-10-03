"""
重排层（M3 · D3）。

交叉编码器（CrossEncoder）与双编码器（BiEncoder）的本质差异：

  双编码器（bge-m3）：query 与 doc **分别**编码成向量，再算相似度
    → doc 向量可预计算，检索 O(1) 但精度有上限
    → 精度损失在「query 与 doc 交互信息丢失」

  交叉编码器（bge-reranker）：query 与 doc **拼在一起**过一遍模型
    → 能建模 token 级交互，精度高得多
    → 但**必须实时计算**，无法预计算 → 只用于 Top-N 重排，不能用于全库检索

★ 这就是标准范式「召回（快、粗）→ 重排（慢、精）」的理论依据。
  本项目的实验目标是量化「重排能带来多少提升，代价是多少」。

模型：BAAI/bge-reranker-v2-m3（0.6B，XLM-RoBERTa 架构）
显存：fp16 约 1.1GB
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
MODEL_DIR = ROOT / "models" / "BAAI_bge-reranker-v2-m3"
PROC = ROOT / "dataset" / "processed"

RERANKER_NAME = "BAAI/bge-reranker-v2-m3"
MAX_LEN = 512          # 交叉编码器要把 query+doc 拼起来，512 够用
BATCH = 16


# ==================================================================
# 模型封装
# ==================================================================

def cuda_available() -> bool:
    try:
        import torch
        return torch.cuda.is_available()
    except ImportError:
        return False


class CrossEncoderReranker:
    """bge-reranker-v2-m3 交叉编码器。"""

    def __init__(self, model_path: Optional[str] = None,
                 device: Optional[str] = None, max_len: int = MAX_LEN):
        self.model_path = model_path or (
            str(MODEL_DIR) if (MODEL_DIR / "config.json").exists()
            else RERANKER_NAME
        )
        self.device = device or ("cuda" if cuda_available() else "cpu")
        self.max_len = max_len
        self.model = None
        self._error = None
        self._load()

    def _load(self):
        try:
            from sentence_transformers import CrossEncoder
            import torch
            # ★ 与 DenseEmbedder 同样的 API 兼容问题：ST 6.x 改了参数名
            for kwargs in ({"device": self.device},
                           {"device": self.device,
                            "model_kwargs": {"torch_dtype": torch.float16}},
                           {}):
                try:
                    self.model = CrossEncoder(self.model_path,
                                              max_length=self.max_len, **kwargs)
                    break
                except TypeError:
                    continue
            if self.model is None:
                raise RuntimeError("CrossEncoder 构造参数不兼容")
        except Exception as e:
            self._error = f"{type(e).__name__}: {e}"
            self.model = None

    def score(self, query: str, docs: list[str]) -> list[float]:
        """给 (query, doc) 对打分，返回相关性分数（越大越相关）。"""
        if self.model is None:
            raise RuntimeError(f"reranker 不可用：{self._error}")
        if not docs:
            return []
        pairs = [(query, d) for d in docs]
        scores = self.model.predict(pairs, batch_size=BATCH,
                                    show_progress_bar=False)
        return [float(s) for s in scores]

    def rerank(self, query: str, candidates: list[tuple[int, str]],
               top_k: int = 20) -> list[tuple[int, float]]:
        """对候选集重排。

        Args:
            query: 查询文本
            candidates: [(chunk_id, doc_text), ...] 来自召回阶段
            top_k: 返回前 k条

        Returns:
            [(chunk_id, rerank_score), ...] 按分数降序
        """
        if not candidates:
            return []
        ids = [c[0] for c in candidates]
        docs = [c[1] for c in candidates]
        scores = self.score(query, docs)
        ranked = sorted(zip(ids, scores), key=lambda x: -x[1])
        return ranked[:top_k]

    def warmup(self) -> None:
        """预热：触发 CUDA 上下文初始化与 kernel 编译。

        ★ 必须做：首次推理含 kernel 编译（10–30 秒），
          若计入延迟统计会严重污染平均延迟数据。
        """
        if self.model is None:
            return
        t0 = time.time()
        self.score("warmup query", ["warmup document"])
        if time.time() - t0 > 5:
            print(f"  [warmup] 首次推理 {time.time() - t0:.1f}s（含 kernel 编译）")

    def info(self) -> dict:
        return {
            "model": self.model_path,
            "device": self.device,
            "max_len": self.max_len,
            "loaded": self.model is not None,
            "error": self._error,
        }


# ==================================================================
# 集成到 Retriever
# ==================================================================

@dataclass
class RerankConfig:
    enabled: bool = True
    candidates: int = 50      # 送入重排的候选数
    top_k: int = 20           # 重排后保留数
    max_len: int = MAX_LEN


def build_rerank_pipeline(reranker: CrossEncoderReranker,
                          chunk_map: dict[int, dict]):
    """构造「重排函数」：输入 (query, [(id, score)])，输出重排后的 [(id, score)]。

    设计为高阶函数，便于在 Retriever.search 中按配置开关，
    避免把 rerank 逻辑硬编码进检索主流程。
    """

    def rerank_fn(query: str, ranked: list[tuple[int, float]],
                  top_k: int = 20) -> list[tuple[int, float]]:
        cand = ranked[:RerankConfig.candidates]
        docs = [chunk_map.get(cid, {}).get("content", "") for cid, _ in cand]
        pairs = [(cid, d) for (cid, _), d in zip(cand, docs)]
        return reranker.rerank(query, pairs, top_k=top_k)

    return rerank_fn


# ==================================================================
if __name__ == "__main__":
    print("=" * 68)
    print("重排模型自测")
    print("=" * 68)
    print(f"  cuda: {cuda_available()}")
    print(f"  模型路径: {MODEL_DIR}")

    r = CrossEncoderReranker()
    print(f"  info: {r.info()}")
    if r.model is None:
        print("  模型不可用，退出")
        raise SystemExit(1)

    chunks = json.loads((PROC / "text_chunks.json").read_text(encoding="utf-8"))
    chunk_map = {c["chunk_id"]: c for c in chunks}

    # 用真实数据构造一个候选集
    q = "What is Star Platinum's time stop ability?"
    cands = [c for c in chunks if c["stand_id"] == "star_platinum"][:20]
    pairs = [(c["chunk_id"], c["content"]) for c in cands]

    t0 = time.time()
    out = r.rerank(q, pairs, top_k=5)
    dt = time.time() - t0
    print(f"\n  查询: {q}")
    print(f"  候选 {len(pairs)} 条，重排耗时 {dt*1000:.0f}ms "
          f"({dt/len(pairs)*1000:.1f}ms/条)")
    for cid, sc in out:
        print(f"    #{cid} score={sc:+.4f}  {chunk_map[cid]['content'][:64]}")

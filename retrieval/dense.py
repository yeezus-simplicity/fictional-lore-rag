"""
向量检索层（M3）。

设计原则：**优雅降级**
  - torch + sentence-transformers 可用 → 真实embedding
  - 不可用 → 退化为可解释的稀疏向量（字符 n-gram TF-IDF）
    ★ 这不是占位符，而是一个真实可用的基线：
      字符 n-gram 在专有名词密集的语料上表现不差，
      且能作为「无embedding」对照组，量化 embedding 的真实增益。

Embedding 选型（数据规范 §3.3）：
  bge-m3（568M，1024 维，MIT 授权）—— 中英混合，8K 上下文
  显存预算 2.2GB（fp16）

索引：内存暴力检索（2407 条 × 1024 维 = 10MB float32）
  → 规模小到不需要 HNSW/IVF，暴力精确检索反而更准
  → 这是「按数据规模选型」的体现，与 D8 的"结构化不进向量库"同源
"""

from __future__ import annotations

import json
import math
import os
import pickle
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
PROC = ROOT / "dataset" / "processed"
CACHE = ROOT / "index"

# 模型配置（显存预算见 docs/数据规范.md §3.4）
MODEL_NAME = "BAAI/bge-m3"
MODEL_DIM = 1024
MAX_SEQ_LEN = 1024          # 块长度 p95=671，取1024 覆盖
BATCH_SIZE = 16


# ==================================================================
# 后端1：真实Embedding
# ==================================================================

def torch_available() -> bool:
    try:
        import torch  # noqa: F401
        return True
    except ImportError:
        return False


def cuda_available() -> bool:
    if not torch_available():
        return False
    import torch
    return torch.cuda.is_available()


def resolve_model_path(model_name: str = MODEL_NAME) -> str:
    """解析模型来源：**优先本地已下载目录**，其次 HF 名。

    ★ 本环境 huggingface.co 被拦（502），必须用 fetch_model.py
      下载到 models/ 后离线加载。直接用 HF 名会触发网络请求并失败。
    """
    local = ROOT / "models" / model_name.replace("/", "_")
    if (local / "config.json").exists():
        return str(local)
    return model_name


class DenseEmbedder:
    """bge-m3 embedding（fp16 优先，显存不足时回退 fp32/CPU）。"""

    def __init__(self, model_name: Optional[str] = None,
                 device: Optional[str] = None):
        self.model_name = model_name or resolve_model_path()
        self.model = None
        self.device = device or ("cuda" if cuda_available() else "cpu")
        self.dim = MODEL_DIM
        self.backend = "none"
        self._load_error = None
        self._try_load()

    def _try_load(self):
        try:
            from sentence_transformers import SentenceTransformer
            import torch
            self.model = SentenceTransformer(self.model_name, device=self.device)
            #精度：cuda 用 fp16 省显存，cpu 必须 fp32（fp16 在 cpu 上极慢且不稳定）
            # ★ ST 6.x 的 dtype 参数名是 model_kwargs，不是 dtype；
            #   不同版本签名不同 → 逐个尝试，失败则退回默认精度
            target = torch.float16 if self.device == "cuda" else torch.float32
            for kwargs in ({"dtype": target},
                           {"model_kwargs": {"torch_dtype": target}},
                           {"model_kwargs": {"dtype": target}},
                           {}):
                try:
                    self.model = SentenceTransformer(
                        self.model_name, device=self.device, **kwargs
                    )
                    self.model.max_seq_length = MAX_SEQ_LEN
                    break
                except TypeError:
                    continue
            self.model.max_seq_length = MAX_SEQ_LEN
            self.backend = f"sentence-transformers/{self.model_name}"
            # ★ ST 6.x 改名为 get_embedding_dimension，旧名会发 FutureWarning
            if hasattr(self.model, "get_embedding_dimension"):
                self.dim = self.model.get_embedding_dimension()
            else:
                self.dim = self.model.get_sentence_embedding_dimension()
            try:
                self.backend += f"/{self.model.dtype}"
            except Exception:
                pass
        except Exception as e:      # 缺包 / 模型下载失败 / 显存不足
            self._load_error = f"{type(e).__name__}: {e}"
            self.model = None

    def encode(self, texts: list[str], batch_size: int = BATCH_SIZE,
               show_progress: bool = False) -> np.ndarray:
        if self.model is None:
            raise RuntimeError(f"embedding 模型不可用：{self._load_error}")
        vecs = self.model.encode(
            texts, batch_size=batch_size, normalize_embeddings=True,
            show_progress_bar=show_progress, convert_to_numpy=True,
        )
        return np.asarray(vecs, dtype=np.float32)

    def info(self) -> dict:
        return {
            "backend": self.backend,
            "device": self.device,
            "dim": self.dim,
            "max_seq_len": MAX_SEQ_LEN,
            "error": self._load_error,
        }


# ==================================================================
# 后端 2：字符 n-gram TF-IDF（降级基线，非占位符）
# ==================================================================

class CharNgramEmbedder:
    """字符 3-gram + 词 unigram 的稀疏向量 → 稠密化。

    ★ 为什么这不是「假embedding」：
      专有名词密集的语料（替身名如 "Star Platinum: The World"），
      字符 n-gram 能捕捉词形相似性与拼写变体，
      在小规模语料上往往接近词级 embedding。
      它同时充当「无embedding」对照组，用于量化真实 embedding 的增益。
    """

    def __init__(self, ngram: int = 3, min_df: int = 1):
        self.ngram = ngram
        self.min_df = min_df
        self.vocab: dict[str, int] = {}
        self.idf: np.ndarray = np.zeros(0, dtype=np.float32)
        self.backend = f"char{ngram}-gram tfidf"

    def _chargrams(self, text: str) -> list[str]:
        s = unicodedata.normalize("NFKC", str(text)).lower()
        s = re_sub_ws(s)
        grams = []
        # 词级
        for w in "".join(ch if ch.isalnum() else " " for ch in s).split():
            if len(w) > 1:
                grams.append(f"w:{w}")
        # 字符n-gram（补边界）
        padded = f"  {s} "
        for i in range(len(padded) - self.ngram + 1):
            grams.append(f"c:{padded[i:i + self.ngram]}")
        return grams

    def fit(self, texts: list[str]) -> "CharNgramEmbedder":
        df: Counter = Counter()
        for t in texts:
            df.update(set(self._chargrams(t)))
        keep = [g for g, c in df.items() if c >= self.min_df]
        keep.sort()
        self.vocab = {g: i for i, g in enumerate(keep)}
        n = len(texts)
        self.idf = np.asarray(
            [math.log(1 + (n - df[g] + 0.5) / (df[g] + 0.5)) for g in keep],
            dtype=np.float32,
        )
        self.dim = len(self.vocab)
        return self

    def encode(self, texts: list[str], **kw) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, t in enumerate(texts):
            cnt = Counter(self._chargrams(t))
            for g, c in cnt.items():
                j = self.vocab.get(g)
                if j is not None:
                    out[i, j] = c
            n = np.linalg.norm(out[i])
            if n > 0:
                out[i] /= n          # L2 归一化 → 余弦 = 点积
        return out

    def info(self) -> dict:
        return {"backend": self.backend, "dim": self.dim,
                "vocab_size": len(self.vocab)}


def re_sub_ws(s: str) -> str:
    import re
    return re.sub(r"\s+", " ", s).strip()


# ==================================================================
# 向量索引
# ==================================================================

@dataclass
class VectorIndex:
    """内存暴力检索（2407 × 1024 规模下，精确检索优于近似索引）。"""

    chunk_ids: list[int]
    matrix: np.ndarray                   # (N, D) 已 L2 归一化
    embedder_info: dict = field(default_factory=dict)
    metric: str = "cosine"

    def search(self, query_vec: np.ndarray, top_k: int = 20
               ) -> list[tuple[int, float]]:
        if query_vec.ndim == 1:
            q = query_vec[None, :]
        else:
            q = query_vec
        # 归一化查询
        norms = np.linalg.norm(q, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        q = q / norms
        sims = q @ self.matrix.T              # (B, N)
        if q.shape[0] == 1:
            sims = sims[0]
            idx = np.argsort(-sims)[:top_k]
            return [(self.chunk_ids[i], float(sims[i])) for i in idx]
        out = []
        for r in range(sims.shape[0]):
            idx = np.argsort(-sims[r])[:top_k]
            out.append([(self.chunk_ids[i], float(sims[r][i])) for i in idx])
        return out

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as f:
            pickle.dump({
                "chunk_ids": self.chunk_ids,
                "matrix": self.matrix,
                "embedder_info": self.embedder_info,
                "metric": self.metric,
            }, f)

    @classmethod
    def load(cls, path: Path) -> "VectorIndex":
        with path.open("rb") as f:
            d = pickle.load(f)
        return cls(**d)


# ==================================================================
# 构建入口
# ==================================================================

def build_index(chunks: list[dict], backend: str = "auto",
                cache_name: Optional[str] = None,
                verbose: bool = True) -> VectorIndex:
    """构建向量索引。

    Args:
        backend: auto / dense / ngram
            auto →优先 dense，不可用则降级 ngram
        cache_name: 缓存文件名（不含扩展名），给定时命中则直接加载
    """
    cache_path = CACHE / f"{cache_name}.pkl" if cache_name else None
    if cache_path and cache_path.exists():
        if verbose:
            print(f"  命中缓存：{cache_path.name}")
        return VectorIndex.load(cache_path)

    texts = [_index_text(c) for c in chunks]
    chunk_ids = [c["chunk_id"] for c in chunks]

    embedder = None
    if backend in ("auto", "dense"):
        if verbose:
            print("  尝试加载 bge-m3 ...")
        embedder = DenseEmbedder()
        if embedder.model is None:
            if verbose:
                print(f"  dense 不可用（{embedder._load_error}）")
                print("  → 降级为 char3-gram tfidf（可解释基线，非占位）")
            if backend == "dense":
                raise RuntimeError(f"dense 后端不可用：{embedder._load_error}")
            embedder = None
        else:
            if verbose:
                print(f"  dense 就绪：{embedder.info()}")

    t0 = time.time()
    if embedder is not None:
        mat = embedder.encode(texts, show_progress=verbose)
        info = embedder.info()
    else:
        ng = CharNgramEmbedder(ngram=3)
        ng.fit(texts)
        mat = ng.encode(texts)
        info = ng.info()
        if verbose:
            print(f"  ngram 就绪：{info}")

    # 归一化（余弦 = 点积）
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    mat = mat / norms

    idx = VectorIndex(chunk_ids=chunk_ids, matrix=mat.astype(np.float32),
                      embedder_info=info)
    if verbose:
        print(f"  构建完成：{mat.shape}，用时 {time.time() - t0:.1f}s，"
              f"显存/内存 {mat.nbytes / 1024 / 1024:.1f} MB")
    if cache_path:
        idx.save(cache_path)
        if verbose:
            print(f"  已缓存：{cache_path.name}")
    return idx


def _index_text(c: dict) -> str:
    """索引用文本：正文 + 实体 + 替身名（与 BM25 侧字段加权对齐）。"""
    parts = [c.get("content", "")]
    if c.get("entity"):
        parts.append(c["entity"] * 2)
    if c.get("stand_name"):
        parts.append(c["stand_name"] * 2)
    if c.get("section"):
        parts.append(c["section"])
    return " ".join(parts)


# ==================================================================
if __name__ == "__main__":
    import sys
    print("=" * 68)
    print("向量索引构建")
    print("=" * 68)
    print(f"  torch: {torch_available()}   cuda: {cuda_available()}")
    print(f"  CUDA 设备: {DenseEmbedder().info()}")

    chunks = json.loads((PROC / "text_chunks.json").read_text(encoding="utf-8"))
    print(f"\n  语料 {len(chunks)} 块")
    idx = build_index(chunks, backend="auto", cache_name="m3_auto")
    print(f"\n  {idx.embedder_info}")

    for q in ["Star Platinum 的破坏力", "时停能力", "外观形态"]:
        import numpy as np
        if idx.embedder_info.get("backend", "").startswith("char"):
            ng = CharNgramEmbedder()
            # 复用已构建的模型
        qv = None
        # 简化：用同后端编码
        if idx.embedder_info.get("backend", "").startswith("char"):
            ng = CharNgramEmbedder()
            ng.vocab = None
            # 重新 fit 保证 vocab 一致（演示用）
            texts = [_index_text(c) for c in chunks]
            ng.fit(texts)
            qv = ng.encode([q])
        else:
            emb = DenseEmbedder()
            qv = emb.encode([q])
        res = idx.search(qv[0], top_k=3)
        print(f"\n  Q: {q}")
        cmap = {c["chunk_id"]: c for c in chunks}
        for cid, sc in res:
            print(f"    #{cid} {sc:.4f} {cmap[cid]['content'][:60]}")

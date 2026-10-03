"""
切块策略对照（D1）。

M1 产出的切块是**语义切块**（按 wiki 小节与招式边界切），
立项书 D1 要求与「固定长度切块」做对照，量化语义切块的价值。

★ 核心难点：**评测集的 gold chunk_id 是按语义切块生成的**。
  若换成固定长度切块，gold id 全部失效。
  正确做法：**保持语料块集合不变，只改变「检索时的分块视图」**——
  即把语义块按固定长度重新聚合/拆分，然后用映射表把 gold 追溯到新 id。

本模块采用两种对照策略：
  fixed_rechunk  把相邻语义块合并成固定长度的窗口（模拟朴素切块）
  semantic       原始语义切块（对照组）
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
PROC = ROOT / "dataset" / "processed"

# 切块目标长度（与 D1 立项书一致：固定 512 字符）
FIXED_LEN = 512
OVERLAP = 64


@dataclass
class ChunkView:
    """切块视图：文本内容 + 溯源信息。"""

    view_id: str
    content: str
    src_chunk_ids: list[int]   # 溯源到原始语义块
    stand_id: str
    stand_name: str = ""
    part: Optional[int] = None
    chunk_type: str = "section"

    def to_dict(self) -> dict:
        return {
            "view_id": self.view_id,
            "content": self.content,
            "src_chunk_ids": self.src_chunk_ids,
            "stand_id": self.stand_id,
            "stand_name": self.stand_name,
            "part": self.part,
            "chunk_type": self.chunk_type,
        }


def _clean(s: str) -> str:
    t = unicodedata.normalize("NFKC", s or "")
    t = re.sub(r"\s+", " ", t)
    return t.strip()


# ==================================================================
# 策略 1：语义切块（对照基线，直接用 M1 产物）
# ==================================================================

def build_semantic_view(chunks: list[dict]) -> list[ChunkView]:
    out = []
    for c in chunks:
        out.append(ChunkView(
            view_id=f"v_sem_{c['chunk_id']}",
            content=c["content"],
            src_chunk_ids=[c["chunk_id"]],
            stand_id=c["stand_id"],
            stand_name=c.get("stand_name", ""),
            part=c.get("part"),
            chunk_type=c.get("chunk_type", "section"),
        ))
    return out


# ==================================================================
# 策略 2：固定长度切块
# ==================================================================

def build_fixed_view(chunks: list[dict],
                     target_len: int = FIXED_LEN,
                     overlap: int = OVERLAP) -> list[ChunkView]:
    """把同一替身的语义块顺序拼接，再按固定长度窗口重新切分。

    ★ 为什么这样做才公平：
      若直接对全文做固定长度切分，会跨越替身边界，产生语义上无意义的块
      （「A 的最后一句 + B 的第一句」在一个块里）。
      正确对照是「保持替身边界内，只改分块粒度」。
    """
    # 按替身分组，保持原有顺序
    by_stand: dict[str, list[dict]] = {}
    for c in chunks:
        by_stand.setdefault(c["stand_id"], []).append(c)

    out: list[ChunkView] = []
    for stand_id, group in by_stand.items():
        group.sort(key=lambda c: c["chunk_id"])
        # 拼接，同时记录每个字符归属的源块
        buf: list[str] = []
        owner: list[int] = []
        for c in group:
            txt = _clean(c["content"])
            if not txt:
                continue
            if buf:
                buf.append(" ")
                owner.append(c["chunk_id"])
            buf.append(txt)
            owner.extend([c["chunk_id"]] * len(txt))

        text = "".join(buf)
        if not text:
            continue
        if len(text) <= target_len:
            # 短替身整体作为一个块
            srcs = sorted(set(owner))
            out.append(ChunkView(
                view_id=f"v_fix_{stand_id}_0",
                content=text,
                src_chunk_ids=srcs,
                stand_id=stand_id,
                stand_name=group[0].get("stand_name", ""),
                part=group[0].get("part"),
                chunk_type="section",
            ))
            continue

        # 滑窗
        step = max(target_len - overlap, 1)
        idx = 0
        wi = 0
        while idx < len(text):
            piece = text[idx:idx + target_len]
            if not piece.strip():
                break
            srcs = sorted(set(owner[idx:idx + len(piece)]))
            out.append(ChunkView(
                view_id=f"v_fix_{stand_id}_{wi}",
                content=piece,
                src_chunk_ids=srcs,
                stand_id=stand_id,
                stand_name=group[0].get("stand_name", ""),
                part=group[0].get("part"),
                chunk_type="section",
            ))
            idx += step
            wi += 1
    return out


# ==================================================================
# 检索适配
# ==================================================================

class ViewCorpus:
    """把切块视图包装成 BM25Index 可用的形态。"""

    def __init__(self, views: list[ChunkView]):
        self.views = views
        self.docs = [v.to_dict() for v in views]
        # 溯源映射：语义 chunk_id → 包含它的 view_id 列表
        self.src_to_views: dict[int, list[str]] = {}
        for v in views:
            for sid in v.src_chunk_ids:
                self.src_to_views.setdefault(sid, []).append(v.view_id)
        # view_id → stand_id
        self.view_stand = {v.view_id: v.stand_id for v in views}

    def __len__(self):
        return len(self.views)


def build_views(strategy: str, chunks: list[dict]) -> list[ChunkView]:
    if strategy == "semantic":
        return build_semantic_view(chunks)
    if strategy == "fixed":
        return build_fixed_view(chunks)
    raise ValueError(f"未知策略：{strategy}")


if __name__ == "__main__":
    chunks = json.loads((PROC / "text_chunks.json").read_text(encoding="utf-8"))
    print("=" * 68)
    print("D1 切块策略对照")
    print("=" * 68)
    print(f"  原始语义块：{len(chunks)}")

    for strat in ("semantic", "fixed"):
        views = build_views(strat, chunks)
        lens = [len(v.content) for v in views]
        lens.sort()
        p50 = lens[len(lens) // 2] if lens else 0
        srcs = sum(len(v.src_chunk_ids) for v in views)
        print(f"\n  {strat:10s} 块数={len(views):5d}  "
              f"总字符={sum(lens):7d}  "
              f"块长 p50={p50:4d} max={lens[-1] if lens else 0:5d}  "
              f"平均溯源={srcs/max(len(views),1):.2f}")

    print("\n  fixed 策略样例：")
    fv = build_fixed_view(chunks)
    for v in fv[:2]:
        print(f"    {v.view_id}  len={len(v.content)}  "
              f"溯源={v.src_chunk_ids[:4]}")
        print(f"      {v.content[:90]}")

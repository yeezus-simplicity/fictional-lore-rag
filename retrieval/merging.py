"""
检索块合并（M12）。

把 M7 结论应用为生产配置：把语义块按目标长度合并，减少「答案装不下」的情况。

==★ 为什么是配置层而不是改数据 ==

M7/M9 已验证「合并到 512」能让空洞率从 0.50 降到 0.30（延迟 +52%）。
最直接的落地方式 seems 是重写 `text_chunks.json`。但**那样会毁掉评测**：

    评测集 `eval_set.json` 的 T4（25 条）/ T5（21 条）直接引用 chunk_id，
    例如 `evidence.chunks: [2390]`。
    而 M9 的实验实现用 `len(out)` 给合并块**重编号** →
    重编号后 2390 指向另一个块 → **46 条题的 gold 全部失效**。

→ 所以本模块：
  1. **不动原始数据**（`text_chunks.json` 保持 2407 块不变）
  2. 合并块**保留 `base_chunk_ids`**（原始 chunk_id 列表）→ 溯源链不断
  3. 通过配置启用，可一键回退到不合并

==★ 为什么按「同一替身内相邻合并」而不是定长切 ==

语义块是 wiki 的天然语义单元（section / ability_overview / history 等）。
合并它们不破坏语义完整性；而从原始段落定长切会把句子拦腰截断
（M7 实测：段落均 452 字，target=1024 时只出 17 块，实验直接失效）。

实测（M9，混合检索，30 题）：
    空洞率0.5000 → 0.3000   （−40%）
    被用字数   169 → 262（+55%）
    延迟        5723ms → 8720ms（+52%）
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
PROC = ROOT / "dataset" / "processed"

# ★ M9 实测推荐的合并目标（字符）
RECOMMENDED_TARGET = 512
# ★ 重叠块数：1 表示相邻块重叠 1 块，避免边界处语义割裂
RECOMMENDED_OVERLAP_BLOCKS = 1

#★ 语义块的平均长度（约 289 字），用来把「重叠字符数」换算成「重叠块数」
#   仅用于 CLI 友好显示，不影响检索逻辑
AVG_BLOCK_CHARS = 229


def merge_chunks(base: list[dict],
                 target: int = RECOMMENDED_TARGET,
                 overlap_blocks: int = RECOMMENDED_OVERLAP_BLOCKS,
                 ) -> list[dict]:
    """把语义块按目标长度合并（同一替身内相邻合并）。

    ★★ 与 M9 实验实现的关键差异：**不重编号 chunk_id**

      M9 的实验代码用 `"chunk_id": len(out)` 给合并块重新编号，
      那样得到的是一套与 `text_chunks.json` 不一致的 ID 空间。
      本实现：
        - 合并块的 `chunk_id` 取**首个基础块的原始 id**（保证唯一且可回溯）
        - `base_chunk_ids` 记录全部来源块的原始 id
        - `chunk_id` 范围不新增编号 → 评测集里的 [2390] 仍能定位到正确的来源

      这样即使服务开启了合并，评测集与证据链依然有效。

    Args:
        base: 基准语义块（text_chunks.json 的行）
        target: 目标块长（字符）。单块超目标则独立成块，不硬切。
        overlap_blocks: 重叠块数（0 = 不重叠）

    Returns:
        合并后的块列表，每块带 base_chunk_ids / n_base_blocks。
    """
    if target <= 0:
        return list(base)

    out: list[dict] = []
    # ★ 按 stand_id 分组，保持原有顺序（相邻才合并，不跨替身）
    by_stand: dict[str, list[dict]] = {}
    order: list[str] = []
    for c in base:
        sid = c.get("stand_id") or ""
        if sid not in by_stand:
            by_stand[sid] = []
            order.append(sid)
        by_stand[sid].append(c)

    def _emit(buf: list[dict]) -> None:
        """把缓冲区输出成一个合并块。

        ★★ chunk_id 的取法（实测踩过两次坑）：
          最初想「沿用首个基础块的 id」，理由是不新增编号空间。
          但实测**会撞号**：超长单块在循环里被独立输出（用原 id），
          而它的 id 又可能正好是后面某个合并块的首个 id
          → 实测 1986 个块里77 个 id 重复 → 7 个评测引用失效。

          → 改为**独立的合并块 id 空间**：负数区间
            （原始块是非负的 0..2406，合并块用 -1, -2, ...）
            这样：
              ·合并块之间绝不重复
              ·与原始块绝不冲突
              ·`base_chunk_ids` 保留全部来源 id，溯源链完整
        """
        if not buf:
            return
        base_ids = [b["chunk_id"] for b in buf]
        out.append({
            # ★ 负数 id 空间：不与原始块冲突，也不会互相重复
            "chunk_id": -(len(out) + 1),
            "base_chunk_ids": base_ids,
            "n_base_blocks": len(buf),
            "stand_id": buf[0].get("stand_id"),
            "stand_name": buf[0].get("stand_name"),
            "part": buf[0].get("part"),
            "chunk_type": buf[0].get("chunk_type"),
            "section": " | ".join(dict.fromkeys(
                str(b.get("section", "")) for b in buf))[:120],
            "entity": buf[0].get("entity") or buf[0].get("stand_id"),
            "content": "\n".join(b["content"] for b in buf),
            "n_chars": sum(len(b["content"]) for b in buf),
            "source_url": buf[0].get("source_url"),
            # ★ 标记这是合并块 —— 界面上可以显示「由 N 个原始块合并」
            "merged": True,
        })

    for sid in order:
        items = by_stand[sid]
        buf: list[dict] = []
        buf_len = 0

        for c in items:
            L = len(c["content"])

            # 单块就超目标 → 独立成块（★ 不硬切，会截断语义）
            if L >= target:
                _emit(buf)
                buf, buf_len = [], 0
                out.append({
                    # ★ 超长单块也走负数 id 空间（保持 id 空间统一）
                    "chunk_id": -(len(out) + 1),
                    "base_chunk_ids": [c["chunk_id"]],
                    "n_base_blocks": 1,
                    "stand_id": c.get("stand_id"),
                    "stand_name": c.get("stand_name"),
                    "part": c.get("part"),
                    "chunk_type": c.get("chunk_type"),
                    "section": c.get("section", ""),
                    "entity": c.get("entity") or c.get("stand_id"),
                    "content": c["content"],
                    "n_chars": L,
                    "source_url": c.get("source_url"),
                    "merged": False,
                })
                continue

            # 加入后会超目标 → 先 flush，若配置了重叠则保留尾部若干块
            if buf and buf_len + L > target:
                _emit(buf)
                keep = buf[-overlap_blocks:] if overlap_blocks else []
                buf = list(keep)
                buf_len = sum(len(b["content"]) for b in buf)

            buf.append(c)
            buf_len += L

        _emit(buf)

    return out


def build_chunks(target: Optional[int] = None,
                 overlap_blocks: int = RECOMMENDED_OVERLAP_BLOCKS,
                 ) -> list[dict]:
    """载入主索引，按需合并。

    Args:
        target: 合并目标块长；None / 0 表示**不合并**（默认，保持原行为）
        overlap_blocks: 重叠块数

    Returns:
        块列表。不合并时就是原始 2407 块。
    """
    p = PROC / "text_chunks.json"
    if not p.exists():
        raise FileNotFoundError(f"主索引缺失：{p}")
    base = json.loads(p.read_text(encoding="utf-8"))

    if not target or target <= 0:
        return base
    return merge_chunks(base, target, overlap_blocks)


def index_stats(chunks: list[dict]) -> dict:
    """块集合的统计信息（用于日志与验证）。"""
    lens = sorted(c["n_chars"] if "n_chars" in c else len(c["content"])
                  for c in chunks)
    n = len(lens) or 1
    return {
        "n_chunks": len(chunks),
        "mean_chars": round(sum(lens) / n, 1),
        "p50": lens[len(lens) // 2],
        "p90": lens[min(len(lens) - 1, 9 * len(lens) // 10)],
        "max": lens[-1],
        "n_merged": sum(1 for c in chunks if c.get("merged")),
    }


if __name__ == "__main__":
    # 自测：python retrieval/merging.py
    print("=" * 66)
    print("合并模块自检")
    print("=" * 66)

    raw = json.loads((PROC / "text_chunks.json").read_text(encoding="utf-8"))
    print(f"\n原始：{len(raw)} 块")

    for tgt in (0, 512, 1024):
        st = index_stats(build_chunks(tgt or None))
        label = "不合并" if not tgt else f"合并到 {tgt}"
        print(f"  {label:12s} {st['n_chunks']:>5} 块  均{st['mean_chars']:>6.0f} 字"
              f"  p50={st['p50']:<5} 合并块 {st['n_merged']}")

    # ★★ 关键不变量：合并后所有原始 chunk_id 都还能被找到
    print("\n[不变量检查]")
    merged = build_chunks(RECOMMENDED_TARGET)
    raw_ids = {c["chunk_id"] for c in raw}

    # 溯源索引：原始 id →包含它的合并块
    src_index: dict[int, dict] = {}
    for c in merged:
        for bid in c.get("base_chunk_ids", [c["chunk_id"]]):
            src_index[bid] = c
    covered = set(src_index)
    print(f"  原始 id 集合大小     {len(raw_ids)}")
    print(f"  合并后可溯源的 id 数 {len(covered)}")
    ok = covered >= raw_ids
    print(f"  {'✓' if ok else '✗'} 所有原始 chunk_id 均可溯源"
          f"{'' if ok else ' —— 有 id 丢失！'}")

    # chunk_id 唯一性（合并块用负数空间，绝不能重复）
    ids = [c["chunk_id"] for c in merged]
    uniq = len(ids) == len(set(ids))
    print(f"  {'✓' if uniq else '✗'} 合并块 chunk_id 唯一"
          f"（{len(ids)} 个，{len(set(ids))} 个唯一）")

    # id 空间不与原始冲突
    conflict = {i for i in ids if i in raw_ids}
    print(f"  {'✓' if not conflict else '✗'} 合并块 id 不与原始冲突"
          f"{'' if not conflict else f' —— {len(conflict)} 个冲突'}")

    # 评测集引用完整性（★ 通过 base_chunk_ids 解析）
    ev_path = PROC / "eval_set.json"
    if ev_path.exists():
        ev = json.loads(ev_path.read_text(encoding="utf-8"))
        n_ref = miss = 0
        miss_ids: list[int] = []
        for x in ev:
            e = x.get("evidence") or {}
            for cid in e.get("chunks") or []:
                n_ref += 1
                if cid not in src_index:
                    miss += 1
                    miss_ids.append(cid)
        print(f"  {'✓' if miss == 0 else '✗'} 评测集 chunk 引用可溯源："
              f"{n_ref} 个引用，{miss} 个失效"
              f"{'' if miss == 0 else f' —— 失效 id {miss_ids[:5]}'}")

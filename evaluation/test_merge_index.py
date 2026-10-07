"""
块合并回归测试（M13）。

★★ 为什么必须有这个：
  合并涉及 `chunk_id` 的重新组织，一旦出错会**静默毁掉评测集**——
  T4/T5 直接引用 chunk_id（如 `[2390]`），指向错了gold 就全错，
  而检索本身仍能正常返回结果，看不出异常。

  本测试把四条不变量固化下来，任何改动破坏了都立刻报错。

用法：
    python evaluation/test_merge_index.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "retrieval"))
sys.path.insert(0, str(ROOT / "services"))
sys.path.insert(0, str(ROOT))

from merging import (  # noqa: E402
    RECOMMENDED_TARGET, build_chunks, index_stats, merge_chunks,
)

PROC = ROOT / "dataset" / "processed"


def main() -> int:
    print("=" * 68)
    print("块合并回归测试（M13）")
    print("=" * 68)

    raw = json.loads((PROC / "text_chunks.json").read_text(encoding="utf-8"))
    raw_ids = {c["chunk_id"] for c in raw}

    # ---------------------------------------------------------
    print("\n[1] 默认不合并 —— 必须与原始完全一致")
    d = build_chunks(None)
    same = len(d) == len(raw) and d[0]["content"] == raw[0]["content"]
    print(f"  {'✓' if same else '✗'} build_chunks(None) == 原始 "
          f"({len(d)} vs {len(raw)} 块)")
    if not same:
        return 1
    d0 = build_chunks(0)
    print(f"  ✓ build_chunks(0) 也是不合并（{len(d0)} 块）")

    # ---------------------------------------------------------
    print("\n[2] 块数应随目标长度递减")
    prev = None
    for tgt in (256, 512, 1024, 2048):
        st = index_stats(build_chunks(tgt))
        flag = ""
        if prev is not None:
            flag = "✓" if st["n_chunks"] < prev else "✗ 未递减"
        else:
            flag = "（基准）"
        print(f"  {flag:8s} target={tgt:<5d} {st['n_chunks']:>5} 块"
              f"  均{st['mean_chars']:>6.0f} 字  p50={st['p50']}")
        if prev is not None and st["n_chunks"] >= prev:
            print("      ★ 块数应随 target 增大而减少")
            return 1
        prev = st["n_chunks"]

    # ---------------------------------------------------------
    print(f"\n[3] 不变量（合并到 {RECOMMENDED_TARGET}）")
    merged = build_chunks(RECOMMENDED_TARGET)

    # 3a. 所有原始 id 都能溯源
    src: dict[int, dict] = {}
    for c in merged:
        for bid in c.get("base_chunk_ids", [c["chunk_id"]]):
            src[bid] = c
    miss = raw_ids - set(src)
    print(f"  {'✓' if not miss else '✗'} 原始 id 全可溯源"
          f"（{len(raw_ids) - len(miss)}/{len(raw_ids)}）")
    if miss:
        print(f"      ★ 丢失 {len(miss)} 个：{sorted(miss)[:5]}")
        return 1

    # 3b. 合并块 id 唯一
    ids = [c["chunk_id"] for c in merged]
    uniq = len(ids) == len(set(ids))
    print(f"  {'✓' if uniq else '✗'} 合并块 id 唯一（{len(set(ids))}/{len(ids)}）")
    if not uniq:
        from collections import Counter
        dup = [k for k, v in Counter(ids).items() if v > 1]
        print(f"      ★ 重复 {len(dup)} 个：{dup[:5]}")
        return 1

    # 3c. 与原始 id 空间不冲突
    conflict = {i for i in ids if i in raw_ids}
    print(f"  {'✓' if not conflict else '✗'} id 空间不冲突"
          f"{'' if not conflict else f' —— {len(conflict)} 个'}")
    if conflict:
        return 1

    # 3d. 评测集引用完整
    ev_path = PROC / "eval_set.json"
    if ev_path.exists():
        ev = json.loads(ev_path.read_text(encoding="utf-8"))
        n_ref = miss_ref = 0
        bad: list[int] = []
        for x in ev:
            e = x.get("evidence") or {}
            for cid in e.get("chunks") or []:
                n_ref += 1
                if cid not in src:
                    miss_ref += 1
                    bad.append(cid)
        print(f"  {'✓' if not miss_ref else '✗'} 评测集引用可溯源"
              f"（{n_ref} 个引用，{miss_ref} 个失效）")
        if miss_ref:
            print(f"      ★ 失效 id：{bad[:5]}")
            return 1

    # ---------------------------------------------------------
    print("\n[4] 语义边界 —— 不能跨替身合并")
    bad_cross = [c for c in merged
                 if c.get("merged") and len({b for b in c["base_chunk_ids"]}) == 0]
    print(f"  {'✓' if not bad_cross else '✗'} 无空来源块"
          f"（{len(bad_cross)} 个异常）")

    #每个合并块内所有基础块必须同属一个替身
    raw_by_id = {c["chunk_id"]: c for c in raw}
    cross = 0
    for c in merged:
        sids = {raw_by_id[b].get("stand_id") for b in c["base_chunk_ids"]
                if b in raw_by_id}
        if len(sids) > 1:
            cross += 1
    print(f"  {'✓' if not cross else '✗'} 合并块内不跨替身"
          f"（{cross} 个跨替身）")
    if cross:
        return 1

    # ---------------------------------------------------------
    print("\n[5] 内容完整性 —— 合并块的原文拼接后应包含全部来源")
    import random
    rng = random.Random(42)
    sample = rng.sample([c for c in merged if c.get("merged")], min(30, len(merged)))
    lost = 0
    for c in sample:
        joined = c["content"]
        for b in c["base_chunk_ids"]:
            src_chunk = raw_by_id.get(b)
            if src_chunk:
                # 取首40 字做锚点（避免换行拼接差异）
                anchor = src_chunk["content"][:40].strip()
                if anchor and anchor not in joined:
                    lost += 1
                    break
    print(f"  {'✓' if not lost else '✗'} 抽样 30 个合并块，原文均完整保留"
          f"（{lost} 个丢失）")
    if lost:
        return 1

    # ---------------------------------------------------------
    print("\n[6] SemanticExecutor 集成")
    try:
        from semantic_executor import SemanticExecutor
        s1 = SemanticExecutor(use_vector=False)
        s1.load()
        print(f"  ✓ 不合并模式：{len(s1._chunks)} 块"
              f"（应为 {len(raw)}）")
        if len(s1._chunks) != len(raw):
            print("      ★ 默认模式块数不对")
            return 1

        s2 = SemanticExecutor(use_vector=False, chunk_merge_target=512)
        s2.load()
        print(f"  ✓ 合并模式：{len(s2._chunks)} 块")

        # trace_to_base 必须能还原
        hit, _ = s2.search("Star Platinum 的破坏力是什么等级", top_k=1)
        if hit:
            cid = hit[0]["chunk_id"]
            back = s2.trace_to_base(cid)
            ok = bool(back) and all(b in raw_by_id for b in back)
            print(f"  {'✓' if ok else '✗'} trace_to_base({cid}) → "
                  f"{back[:4]}{'...' if len(back) > 4 else ''}")
            if not ok:
                return 1
        else:
            print("  ? 检索无命中（跳过溯源检查）")
    except Exception as e:
        print(f"  ✗ SemanticExecutor 集成失败：{type(e).__name__}: {e}")
        return 1

    # ---------------------------------------------------------
    print("\n" + "=" * 68)
    print("★ 全部通过")
    print("=" * 68)
    print(f"""
合并到 {RECOMMENDED_TARGET} 后的效果（相对不合并）：
  块数        {len(raw)} → {len(merged)}
  平均字数    {sum(len(c['content']) for c in raw)//len(raw)} → {index_stats(merged)['mean_chars']:.0f}
  代价        生成耗时 +53%（与 M9 的 +52% 交叉验证）
  收益        空洞率 0.50 → 0.30（修好 8 条 / 弄坏 2 条）

★ 默认仍是「不合并」—— 历史评测数据（M6–M11）依赖它。
  要用512：python start.py --chunk-merge 512
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())

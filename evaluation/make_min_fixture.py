"""合成「最小语料」供 CI 使用（M28）。

为什么需要
----------
真实的 `text_chunks.json`（2407 块描述原文）**不入库**（第三方版权，见 M23）。
但它却是检索层必需的输入 —— 没有它，服务虽然能起来，
语义检索是空的，**几乎所有涉及回答的测试都会失败**。

CI（GitHub Actions）里没法跑抓取（需要外网 + Playwright + 10 分钟），
所以需要一份**能从仓库已有文件现场合成**的替代品。

数据来源（都在仓库里）
--------------------
  dataset/sources/details.json      154 条 infobox（名称/使用者/六维/形态）
  dataset/processed/stands.json     替身主表
  dataset/processed/stand_stats.json 编码后的六维

产物
----
  dataset/processed/text_chunks.json

★★ 重要限制 ★★
  这份语料是**合成**的：它是从结构化字段拼出来的句子，
  **不包含**真实的描述原文。所以：
    - 语法/字段/流程类测试可以用它；
    - **检索质量类评测不能用**（BM25 与向量都拿不到真实文本）。
  真实语料请按 README「重建原文语料」一节跑 run_pipeline.py。

用法
----
    python evaluation/make_min_fixture.py            # 写入 processed/
    python evaluation/make_min_fixture.py --check    # 只看规模，不写文件
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "dataset" / "sources"
PROC = ROOT / "dataset" / "processed"
OUT = PROC / "text_chunks.json"

# 与 dataset/pipeline/chunk.py 保持一致的维度顺序
DIMS = [("PWR", "破壞力"), ("SPD", "速度"), ("RNG", "射程"),
        ("STA", "持續力"), ("PRC", "精密性"), ("DEV", "成長性")]
LV = {0: "None", 1: "E", 2: "D", 3: "C", 4: "B", 5: "A"}


def wrap(md) -> str:
    if isinstance(md, list):
        return " / ".join(str(x) for x in md if x)
    return (str(md) if md is not None else "")


def build_chunks() -> list[dict]:
    details = json.loads((SRC / "details.json").read_text(encoding="utf-8"))
    stands = json.loads((PROC / "stands.json").read_text(encoding="utf-8"))
    stats = json.loads((PROC / "stand_stats.json").read_text(encoding="utf-8"))

    s_meta = {s["stand_id"]: s for s in stands}
    t_meta = {s["stand_id"]: s for s in stats}

    chunks: list[dict] = []
    cid = 0

    for d in details:
        sid = d.get("stand_id")
        if not sid:
            continue
        meta = s_meta.get(sid, {})
        st = t_meta.get(sid, {})
        info = d.get("infobox") or {}
        name_en = meta.get("name_en") or d.get("name_raw") or sid
        name_ja = wrap(info.get("name_ja")) or wrap(meta.get("name_ja"))
        owner = wrap(info.get("owner")) or wrap(meta.get("owner_name"))
        part = meta.get("part")

        # ---- 块 1：概述（名称 / 使用者 / 分部）----
        seg = [f"{name_en}"]
        if name_ja:
            seg.append(f"({name_ja})")
        if owner:
            seg.append(f"is the Stand of {owner}.")
        if part:
            seg.append(f"It appears in part {part} of the series.")
        if meta.get("stand_type"):
            seg.append(f"Type: {meta['stand_type']}.")
        cid += 1
        txt = " ".join(seg)
        chunks.append({
            "chunk_id": cid, "stand_id": sid, "stand_name": name_en,
            "part": part, "chunk_type": "ability_overview",
            "content": txt, "content_len": len(txt), "section": "overview",
        })

        # ---- 块 2：六维数值（供「几级」类问题检索）----
        lv = []
        for k, cn in DIMS:
            v = st.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                lv.append(f"{k} {LV.get(int(v), v)}")
        if lv:
            cid += 1
            txt = f"{name_en} stand stats: " + ", ".join(lv) + "."
            chunks.append({
                "chunk_id": cid, "stand_id": sid, "stand_name": name_en,
                "part": part,
                # ★★ chunk_type 必须是 schema 允许的枚举值 ★★
                #   见 dataset/schema/migrations/001_init.sql 的 chk_chunk_type：
                #     IN ('ability_overview','move','battle_record','lore','section')
                #   我第一版写了 'stats' / 'forms' —— 两个都非法，
                #   CI 上全新库跑 load_db 时直接 CheckViolation 挂掉。
                #   （本地没发现，是因为本地库早建好了、我跳过了 load_db 这步）
                "chunk_type": "ability_overview",
                "content": txt, "content_len": len(txt), "section": "(stats)",
            })

        # ---- 块 3：形态（多形态替身才有）----
        forms = d.get("forms") or []
        if len(forms) > 1:
            labels = [wrap(f.get("form_label")) or f"form_{i}"
                      for i, f in enumerate(forms)]
            cid += 1
            txt = (f"{name_en} has {len(forms)} forms: "
                   + ", ".join(labels) + ".")
            chunks.append({
                "chunk_id": cid, "stand_id": sid, "stand_name": name_en,
                "part": part,
                "chunk_type": "section",          # ★ 同上，必须是合法枚举值
                "content": txt, "content_len": len(txt), "section": "FORMS",
            })

    return chunks


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="只统计规模，不写文件")
    args = ap.parse_args()

    for f in (SRC / "details.json", PROC / "stands.json",
              PROC / "stand_stats.json"):
        if not f.exists():
            print(f"✗ 缺少输入：{f}")
            print("  （这些文件应当随仓库提供；若缺失请检查 clone 是否完整）")
            return 1

    chunks = build_chunks()
    n_stands = len({c["stand_id"] for c in chunks})
    print(f"合成语料：{len(chunks)} 块 / {n_stands} 个替身")

    if args.check:
        print("（--check：未写文件）")
        return 0

    if OUT.exists():
        real = json.loads(OUT.read_text(encoding="utf-8"))
        # 真实语料明显更大（2400+ 块）；这里做个体检，避免误覆盖
        if len(real) > len(chunks) * 2:
            print(f"⚠ {OUT.name} 已存在且规模更大（{len(real)} 块）——"
                  f"看起来是**真实语料**，不覆盖。")
            print("  如确要覆盖，请先自行备份。")
            return 2

    OUT.write_text(json.dumps(chunks, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"已写入 {OUT.relative_to(ROOT)}  "
          f"({OUT.stat().st_size/1024:.0f} KB)")
    print()
    print("★ 提醒：这是**合成**语料，仅够让服务跑起来 + 流程类测试通过；")
    print("  真实检索效果请按 README「重建原文语料」跑 run_pipeline.py。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
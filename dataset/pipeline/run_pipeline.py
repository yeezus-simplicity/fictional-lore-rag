"""
M1 数据管道主控脚本。

全流程：
    静态抓取 → 渲染抓取（Playwright）→ 切块 → 编码 → 合并 → 校验 → 落盘

用法：
    # 用已有抓取结果（默认，快）
    python run_pipeline.py

    # 重新静态抓取详情页（约 10 分钟，154 请求）
    python run_pipeline.py --refetch

    # 重新渲染抓取正文（约 3 分钟，154 页，需 Node + Playwright）
    python run_pipeline.py --rerender

    # 只跑到校验，不落盘
    python run_pipeline.py --dry-run

输出（dataset/processed/）：
    stands.json           替身主表（含 owner / part / 形态链）
    stand_stats.json      编码后的六维能力值
    stand_forms.json      形态表
    text_chunks.json      文本块（进向量索引的单元）
    conflicts.json        全部冲突与消解结果
    normalizations.json   归一化事件（非冲突）
    merge_report.json     合并统计报告
    chunk_report.json     切块统计
    validation_report.json 校验报告
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from encode import STAT_DIMS
from merge import (
    MergeResult,
    merge,
    read_csv_rows,
    save_outputs,
)
from scrape import DATA_DIR, fetch_all_details, fetch_main_table, save_json
from validate import (
    SOURCE_CSV_BOGDAN,
    SOURCE_CSV_TOPOLOGY,
    validate_all,
    validate_columns,
    validate_row,
)

# CSV 镜像路径（M1-1 探查时下载的快照）
FIELD_DIR = Path(__file__).resolve().parents[2] / "docs" / "字段映射"
BOGDAN_CSV = FIELD_DIR / "csv_bogdan_raw.csv"
TOPOLOGY_CSV = FIELD_DIR / "csv_topology_raw.csv"

# 渲染抓取工具
TOOLS_DIR = Path(__file__).resolve().parents[2] / "tools"
RENDER_DIR = DATA_DIR / "rendered"

# ★★ Node 查找顺序（M23 修正）★★
#   原来这里**写死了开发机的绝对路径**（C:\Users\...\node.exe），
#   别人 clone 后那路径不存在 → 渲染抓取直接失败、语料重建不了。
#   → 依次尝试：环境变量 NODE_BIN → PATH 里的 node → 裸 "node"
#   node_modules 也改用 tools/ 下的标准位置（npm install 装在那里）。
NODE = (os.environ.get("NODE_BIN")
        or shutil.which("node")
        or "node")
NODE_MODULES = Path(os.environ.get("NODE_PATH")
                    or (TOOLS_DIR / "node_modules"))

OUT_DIR = Path(__file__).resolve().parents[1] / "processed"
DETAILS_JSON = DATA_DIR / "details.json"


def banner(text: str) -> None:
    print("\n" + "=" * 68)
    print(text)
    print("=" * 68)


def load_details():
    """载入详情页：优先读缓存，否则抓取。"""
    if DETAILS_JSON.exists():
        data = json.loads(DETAILS_JSON.read_text(encoding="utf-8"))
        print(f"  详情页缓存：{DETAILS_JSON}（{len(data)} 个）")
        return data, True
    return None, False


def run_render_fetch(delay_ms: int = 700) -> bool:
    """调用 Node 渲染抓取脚本。返回是否成功。"""
    script = TOOLS_DIR / "render_fetch.js"
    if not script.exists():
        print(f"  渲染脚本不存在：{script}")
        return False
    # ★ NODE 现在是字符串（可能是 PATH 里的 "node"），不能再 .exists()
    if not NODE:
        print("  找不到 node —— 请安装 Node.js，"
              "或设置环境变量 NODE_BIN 指向 node 可执行文件")
        return False
    if not NODE_MODULES.exists():
        print(f"  渲染依赖未安装：{NODE_MODULES} 不存在")
        print(f"  请先执行：cd {TOOLS_DIR} && npm install")
        return False
    env = {**os.environ, "NODE_PATH": str(NODE_MODULES)}
    print(f"  渲染抓取中（间隔 {delay_ms}ms，约 {154 * delay_ms / 1000 / 60:.1f} 分钟）…")
    try:
        r = subprocess.run(
            [str(NODE), str(script), "--delay", str(delay_ms)],
            cwd=str(TOOLS_DIR), env=env, timeout=1800,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        tail = (r.stdout or "").strip().split("\n")[-3:]
        for line in tail:
            print(f"    {line}")
        return r.returncode == 0
    except FileNotFoundError:
        print(f"  无法执行 node（{NODE}）—— 请确认 Node.js 已装且在 PATH 中")
        return False
    except subprocess.TimeoutExpired:
        print("  渲染抓取超时")
        return False


def count_rendered() -> int:
    return len(list(RENDER_DIR.glob("*.json"))) if RENDER_DIR.exists() else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refetch", action="store_true",
                    help="强制重新静态抓取详情页")
    ap.add_argument("--rerender", action="store_true",
                    help="强制重新渲染抓取正文")
    ap.add_argument("--dry-run", action="store_true", help="不落盘")
    ap.add_argument("--delay", type=float, default=0.45, help="静态抓取限速（秒）")
    ap.add_argument("--render-delay", type=int, default=700, help="渲染抓取限速（ms）")
    args = ap.parse_args()

    t0 = time.time()

    # ---------- 1. 静态抓取 ----------
    banner("步骤 1/5  静态抓取（主表 + infobox）")
    main_rows = fetch_main_table(use_cache=True)
    print(f"  主表：{len(main_rows)} 条")

    details, cached = load_details()
    if args.refetch or details is None:
        if not cached:
            print(f"  抓取详情页（限速 {args.delay}s，约 10 分钟）…")
        pages = fetch_all_details(main_rows, delay=args.delay)
        details = [p.to_dict() for p in pages]
        if not args.dry_run:
            save_json(details, DETAILS_JSON)
        print(f"  详情页：{len(details)} 个")
    else:
        print(f"  详情页：{len(details)} 个（缓存）")

    bogdan = read_csv_rows(BOGDAN_CSV) if BOGDAN_CSV.exists() else []
    topology = read_csv_rows(TOPOLOGY_CSV) if TOPOLOGY_CSV.exists() else []
    print(f"  CSV 镜像 A（{SOURCE_CSV_BOGDAN}）：{len(bogdan)} 条")
    print(f"  CSV 镜像 B（{SOURCE_CSV_TOPOLOGY}）：{len(topology)} 条")

    # ---------- 1b. 渲染抓取（正文） ----------
    banner("步骤 2/5  渲染抓取（正文，Playwright）")
    n_rendered = count_rendered()
    print(f"  已有渲染结果：{n_rendered} 个")
    if args.rerender:
        run_render_fetch(args.render_delay)
    elif n_rendered == 0:
        print("  无缓存，触发首次渲染抓取")
        run_render_fetch(args.render_delay)
    print(f"  渲染结果：{count_rendered()} 个页面")

    # ---------- 2. V9 列名校验 ----------
    banner("步骤 3/5  V9 列名白名单校验")
    for label, rows, src in (
        ("镜像 A", bogdan, SOURCE_CSV_BOGDAN),
        ("镜像 B", topology, SOURCE_CSV_TOPOLOGY),
    ):
        if not rows:
            continue
        cols = list(rows[0].keys())
        rep = validate_columns(cols, src)
        print(f"  {label} {cols}")
        for iss in rep.issues:
            print(f"    {iss}")
    print("  注：列名差异（PER vs STA）不阻断，按位置映射")

    # ---------- 3. 合并 ----------
    banner("步骤 4/5  合并 + 编码 + 冲突记录")
    res = merge(main_rows, details, bogdan, topology)
    rep = res.stats_report
    print(f"  替身          {rep['n_stands']}")
    print(f"  形态          {rep['n_forms']}"
          f"（多形态替身 {rep['multi_form_stands']} 个）")
    print(f"  所属部命中     {rep['with_part']}/{rep['n_stands']}")
    print(f"  使用者命中     {rep['with_owner']}/{rep['n_stands']}")
    print(f"  composite 可算 {rep['comparable_rows']}/{rep['n_stats_rows']}")
    print(f"  冲突          {rep['n_conflicts']}")
    print(f"  归一化事件     {rep['n_normalizations']}")

    print("\n  能力值类别分布（主源编码结果）：")
    total = sum(rep["category_distribution"].values())
    for cat, n in rep["category_distribution"].items():
        print(f"    {cat:22s} {n:4d}  ({n / total * 100:5.1f}%)")

    if rep["conflict_type_distribution"]:
        print("\n  冲突类型分布：")
        for ct, n in rep["conflict_type_distribution"].items():
            print(f"    {ct:24s} {n:4d}")

    # ---------- 4. 切块 ----------
    banner("步骤 5/5  文本切块（进向量索引的单元）")
    from chunk import build_all as build_chunks, report as chunk_report
    chunks = build_chunks()
    crep = chunk_report(chunks)
    print(f"  文本块 {crep['n_chunks']} 个，覆盖 {crep['n_stands_covered']} 个替身")
    print(f"  总字符 {crep['total_chars']:,}")
    for t, n in crep["type_distribution"].items():
        print(f"    {t:20s} {n:5d}")
    ls = crep["len_stats"]
    print(f"  块长度 min={ls['min']} p50={ls['p50']} p85={ls['p85']} "
          f"p95={ls['p95']} max={ls['max']}")
    print(f"  所属部分布：{crep['part_distribution']}")

    # ---------- 5. 校验 ----------
    banner("步骤 5b/5  V1–V11 校验")
    # 校验对象是 res.stats（已编码，含 *_raw 原始字面量）
    from encode import encode_row as _er
    enc_rows = []
    for s in res.stands:
        st = next((x for x in res.stats if x["stand_id"] == s["stand_id"]), None)
        if st is None:
            continue
        enc_rows.append(_er(s["name_raw"],
                            [st[f"{d.lower()}_raw"] for d in STAT_DIMS]))
    vrep = validate_all(enc_rows)
    print(f"  被校验行数 {len(enc_rows)}")
    print(f"  {vrep.summary()}")
    for e in vrep.errors[:20]:
        print(f"    {e}")
    for w in vrep.warnings[:8]:
        print(f"    {w}")

    # 文本块完整性（V8）
    from validate import validate_chunks
    crep_v = validate_chunks([c.to_dict() for c in chunks])
    print(f"  文本块 V8：{crep_v.summary()}")
    for e in crep_v.errors[:5]:
        print(f"    {e}")

    # ---------- 落盘 ----------
    if args.dry_run:
        print("\n  --dry-run，跳过落盘")
    else:
        paths = save_outputs(res, OUT_DIR)
        # 文本块
        cp = OUT_DIR / "text_chunks.json"
        cp.write_text(json.dumps([c.to_dict() for c in chunks],
                                 ensure_ascii=False, indent=2), encoding="utf-8")
        paths["text_chunks"] = cp
        # 切块报告
        crp = OUT_DIR / "chunk_report.json"
        crp.write_text(json.dumps(crep, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        paths["chunk_report"] = crp
        # 校验报告
        vpath = OUT_DIR / "validation_report.json"
        vpath.write_text(json.dumps({
            "summary": vrep.summary(),
            "n_errors": len(vrep.errors),
            "n_warnings": len(vrep.warnings),
            "errors": [str(i) for i in vrep.errors],
            "warnings": [str(i) for i in vrep.warnings],
            "chunk_validation": crep_v.summary(),
            "merge_report": rep,
            "chunk_report": crep,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        paths["validation_report"] = vpath
        print(f"\n  输出目录：{OUT_DIR}")
        for k, p in paths.items():
            print(f"    {k:18s} {p.name}  ({p.stat().st_size / 1024:.1f} KB)")

    print(f"\n  总耗时 {(time.time() - t0) / 60:.2f} 分钟")
    return 0 if vrep.ok else 1


if __name__ == "__main__":
    sys.exit(main())

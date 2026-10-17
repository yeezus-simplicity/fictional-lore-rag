"""抓取角色 / 地点的 infobox 头像（M74）。

== 为什么需要 ==
用户实测反馈：
  「搜索人物时不会返回人物的图片」
  「地点要是有图片也加上吧」

替身图片早在 M17 就有（`fetch_stand_images.py`），角色/地点一直没有 ——
而它们的 infobox 里**本来就有图**。

== 数据来源 ==
- 角色：`{{Character Infobox | image = ...}}` 里的第一个 `[[File:XXX.png`
- 地点：`{{Location Infobox | image = ...}}`，兜底 `{{Media | *Av = XXXAv.png}}`
实测覆盖：**角色 363/368、地点 157/157**（无图的 5 个确实没有 infobox 图）。

== 为什么不复用 `fetch_stand_images.page_images_by_kind()` ==
那个是「抓整页的所有图」（替身要分形态/使用者/封面，实测一页几十张）。
角色/地点只要**一张头像** → 直接按文件名构造 URL 下载，**1 张/实体**。
481 个实体 × 1 张 ≈ 5-8 分钟（4 并发），比整页抓小一个数量级。

== 输出 ==
    images/<entity_id>/<filename>         400px thumb
    dataset/processed/media_index.json    {"character": {...}, "location": {...}}

用法：
    python dataset/sources/images/fetch_entity_images.py
    python dataset/sources/images/fetch_entity_images.py --only character
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_stand_images as F  # noqa: E402

ART = ROOT / "dataset" / "sources" / "jojowiki" / "articles.json"
OUT_IDX = ROOT / "dataset" / "processed" / "media_index.json"
IMG_ROOT = ROOT / "images"

# infobox 里 image 字段之后多少字符内找 [[File:]]
# ★ 实测必须跨行：`|image = {{Tabber\n|Part 3|\n{{Switch\n|Tab1 = Manga|[[File:...`
#   只取同一行会漏掉 247/368 个角色（实测 116 → 363）。
_WINDOW = 900


def _first_file(s: str) -> str | None:
    m = re.search(r"\[\[File:([^|\]]+)", s or "")
    return m.group(1).strip() if m else None


def parse_images() -> dict[str, dict[str, str]]:
    """→ {"character": {id: fname}, "location": {id: fname}}"""
    art = json.loads(ART.read_text(encoding="utf-8"))
    pages = art["pages"] if isinstance(art, dict) and "pages" in art else art
    out: dict[str, dict[str, str]] = {"character": {}, "location": {}}
    for p in pages:
        kind = p.get("kind")
        if kind not in out:
            continue
        eid = p.get("entity_id")
        if not eid:
            continue
        wt = p.get("wikitext") or ""
        fname = None
        m = re.search(r"\|\s*image\s*=", wt)
        if m:
            fname = _first_file(wt[m.end():m.end() + _WINDOW])
        if not fname:
            # 地点兜底：`{{Media |CAv = MoriohAv.png}}`（Av = avatar）
            m2 = re.search(
                r"[A-Za-z0-9_]*Av\s*=\s*([^\n|]+?\.(?:png|jpg|jpeg|gif|webp))",
                wt[:1600])
            if m2:
                fname = m2.group(1).strip()
        if fname:
            out[kind][eid] = fname.replace("File:", "").strip()
    # ★ 过滤源站的**占位图**：`amazon_rainforest → NoPicAvailable.png`。
    #   它不是真实头像，显示出来就是一个「无图」方块 —— 不如不显示。
    # ★ 用 `NoPic` 前缀而不是 `NoPicAvailable` 全名：
    #   实测两种缩写都有（`NoPicAvailable.png` / `NoPicAv.png`），
    #   只匹配全名会漏掉后者（测试当场抓到）。
    bad = re.compile(r"(NoPic|NoImage|Placeholder)", re.I)
    for k in out:
        out[k] = {i: f for i, f in out[k].items() if not bad.search(f)}
    return out


def _fetch_one(eid: str, fname: str) -> tuple[str, str, str | None]:
    """下载一个实体的头像 → (eid, fname, 错误信息或 None)。"""
    dest = IMG_ROOT / eid
    target = dest / fname
    if target.exists() and target.stat().st_size > 1024:
        return eid, fname, None                      # 已抓过（幂等）
    dest.mkdir(parents=True, exist_ok=True)
    # thumb 优先；原图本身就 ≤400px 时服务端 400 → 退原图（M17 同款）
    for url in (F._thumb_url(fname), F._orig_url(fname)):
        try:
            data = F.fetch(url, tries=1, timeout=25)
        except Exception:  # noqa: BLE001
            continue
        if not data or len(data) < 512:
            continue
        target.write_bytes(data)
        return eid, fname, None
    return eid, fname, "下载失败（thumb 与原图都不行）"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["character", "location"], default=None)
    ap.add_argument("--workers", type=int, default=4,
                    help="并发数（M47 实测 4 最优，8 反而慢）")
    args = ap.parse_args()

    parsed = parse_images()
    if args.only:
        parsed = {args.only: parsed[args.only]}
    total = sum(len(v) for v in parsed.values())
    print(f"解析到 {total} 个实体有头像"
          f"（角色 {len(parsed.get('character', {}))} / "
          f"地点 {len(parsed.get('location', {}))}）")

    # ★★ 全量跑时**从零重建**索引，不合并旧文件 ★★
    #   踩坑：合并旧键会让已经不该存在的条目（如被过滤的占位图）永远留在索引里
    #   —— 测试「占位图已过滤」当场挂了（实得 ['NoPicAv.png']）。
    #   `--only` 时才读旧文件（要保另一类）。
    idx: dict[str, dict[str, str]] = {}
    if args.only and OUT_IDX.exists():
        try:
            idx = json.loads(OUT_IDX.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            idx = {}
    for k in ("character", "location"):
        idx.setdefault(k, {})

    jobs = [(eid, fn) for k, m in parsed.items() for eid, fn in m.items()]
    ok = fail = 0
    fails: list[tuple[str, str]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_fetch_one, eid, fn): (eid, fn) for eid, fn in jobs}
        for i, fut in enumerate(as_completed(futs), 1):
            eid, fn = futs[fut]
            try:
                eid, fn, err = fut.result()
            except Exception as e:  # noqa: BLE001
                err = f"{type(e).__name__}"
            if err:
                fail += 1
                fails.append((eid, err))
            else:
                ok += 1
                # 归到正确的 kind 下
                for k, m in parsed.items():
                    if eid in m:
                        idx[k][eid] = fn
                        break
            if i % 50 == 0 or i == len(jobs):
                print(f"  {i}/{len(jobs)}  成功 {ok} / 失败 {fail}")

    OUT_IDX.parent.mkdir(parents=True, exist_ok=True)
    OUT_IDX.write_text(json.dumps(idx, ensure_ascii=False, indent=1),
                       encoding="utf-8")
    print(f"\n✓ 成功 {ok} / 失败 {fail} → {OUT_IDX.relative_to(ROOT)}")
    print(f"  索引：角色 {len(idx['character'])} / 地点 {len(idx['location'])}")
    if fails:
        print("  失败样例：")
        for eid, err in fails[:8]:
            print(f"    {eid}: {err}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

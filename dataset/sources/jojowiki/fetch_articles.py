"""抓取 jojowiki 的替身页与角色页正文（B 方案：扩语料）

== 目标 ==
现有语料 2407 块全部来自 `Stand_Stats` 主表的**结构化字段**。
本脚本引入**非结构化长文本**（替身页 + 角色页正文），
用于验证「向量的价值取决于语料非结构化程度」这一假设。

== 为什么走 API 而不是抓 HTML ==
★ scrape.py 里记录过：详情页的 `<p>` 正文段落**由 JS 加载，直接抓是空的**。
  而 MediaWiki API 的 `action=parse&prop=wikitext` 直接返回源文本，
  绕开 JS 渲染 → 拿到完整长文本（实测 Jotaro 页 79,845 字符）。

== 页面清单从哪来 ==
复用 `dataset/processed/stands.json` 里已有的实体名：
  - 替身页：`name_en`（154 个，实测命中 151）
  - 角色页：`owner_name`（138 个，实测命中 117）
★ 只抓与项目实体**对口**的页面 —— jojowiki 有 10000+ 条目，
  但里面大量是章节碎片（`"Green Day" and "Oasis" (1)`…`(14)`），
  无脑全抓只会灌进噪声。

== 网络 ==
★ 本机 jojowiki 必须走代理，且 **urllib 不读 http_proxy 环境变量**，
  必须显式构造 ProxyHandler（此坑实测踩过）。

用法：
    python dataset/sources/jojowiki/fetch_articles.py            # 增量抓取
    python dataset/sources/jojowiki/fetch_articles.py --limit 5  # 只抓 5 个（调试）
输出：
    dataset/sources/jojowiki/articles.json
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
STANDS = ROOT / "dataset" / "processed" / "stands.json"
OUT = Path(__file__).resolve().parent / "articles.json"

BASE = "https://jojowiki.com"
API = BASE + "/api.php"
UA = "Mozilla/5.0 (research; educational; rag-kb)"

_PX = os.environ.get("https_proxy") or os.environ.get("http_proxy")
_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({"http": _PX, "https": _PX}))
_OPENER.addheaders = [("User-Agent", UA)]


def fetch_wikitext(title: str, retries: int = 2) -> str | None:
    """取单个页面的 wikitext。页面不存在返回 None。"""
    u = (f"{API}?action=parse&prop=wikitext&redirects=1"
         f"&format=json&formatversion=2&page={urllib.parse.quote(title)}")
    for attempt in range(retries + 1):
        try:
            d = json.loads(_OPENER.open(u, timeout=60).read())
            if "error" in d:
                return None           # 页面不存在
            return d["parse"]["wikitext"]
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if attempt >= retries:
                raise
        except Exception:
            if attempt >= retries:
                raise
        time.sleep(1.5 * (attempt + 1))
    return None


def build_targets() -> list[dict]:
    """从 stands.json 生成待抓页面清单（替身页 + 角色页）。"""
    stands = json.loads(STANDS.read_text(encoding="utf-8"))
    targets: list[dict] = []
    seen: set[tuple] = set()

    for s in stands:
        sid = s.get("stand_id")
        nm = (s.get("name_en") or "").strip()
        if nm and ("stand", nm) not in seen:
            seen.add(("stand", nm))
            targets.append({"title": nm, "kind": "stand", "entity_id": sid})

    for s in stands:
        oid = s.get("owner_id")
        raw = s.get("owner_name") or ""
        for o in raw.split(","):
            o = o.strip()
            if not o or o.lower() in ("none", "unknown", ""):
                continue
            if ("character", o) in seen:
                continue
            seen.add(("character", o))
            targets.append({"title": o, "kind": "character",
                            "entity_id": oid})
    return targets


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只抓前 N 个（调试）")
    ap.add_argument("--delay", type=float, default=0.35, help="请求间隔（秒）")
    args = ap.parse_args()

    if not STANDS.exists():
        print(f"✗ 缺少 {STANDS}")
        return 1
    if not _PX:
        print("⚠ 未检测到代理环境变量，直连可能超时")

    targets = build_targets()
    if args.limit:
        targets = targets[:args.limit]

    # 断点续抓：已抓过的跳过
    done: dict[str, dict] = {}
    if OUT.exists():
        try:
            old = json.loads(OUT.read_text(encoding="utf-8"))
            done = {p["title"]: p for p in old.get("pages", [])}
            print(f"已有缓存 {len(done)} 页，增量续抓")
        except (json.JSONDecodeError, KeyError):
            pass

    todo = [t for t in targets if t["title"] not in done]
    print(f"待抓 {len(todo)} / 共 {len(targets)} 页"
          f"（替身 {sum(1 for t in targets if t['kind']=='stand')} / "
          f"角色 {sum(1 for t in targets if t['kind']=='character')}）\n")

    ok = miss = err = 0
    t0 = time.time()
    for i, t in enumerate(todo, 1):
        try:
            wt = fetch_wikitext(t["title"])
        except Exception as e:                       # noqa: BLE001
            err += 1
            print(f"  [{i}/{len(todo)}] ✗ {t['title']}  "
                  f"{type(e).__name__}: {str(e)[:60]}")
            time.sleep(args.delay * 3)
            continue
        if wt is None:
            miss += 1
            print(f"  [{i}/{len(todo)}] – {t['title']} 页面不存在")
        else:
            ok += 1
            done[t["title"]] = {
                **t,
                "wikitext": wt,
                "url": f"{BASE}/{urllib.parse.quote(t['title'].replace(' ', '_'))}",
                "wt_len": len(wt),
            }
            if i % 20 == 0 or i == len(todo):
                print(f"  [{i}/{len(todo)}] ✓ {t['title']}  "
                      f"{len(wt):,} 字符")
        time.sleep(args.delay)

    pages = list(done.values())
    OUT.write_text(json.dumps(
        {"n_pages": len(pages), "pages": pages},
        ensure_ascii=False), encoding="utf-8")

    tot = sum(p["wt_len"] for p in pages)
    dt = time.time() - t0
    print(f"\n{'='*60}")
    print(f"完成：命中 {ok} / 不存在 {miss} / 出错 {err}  用时 {dt:.0f}s")
    print(f"累计 {len(pages)} 页，{tot/1024/1024:.2f} MB wikitext")
    print(f"→ {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

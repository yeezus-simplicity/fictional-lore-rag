"""补抓第 8/9 部缺失的替身（35 个）

== 为什么需要 ==
用户反馈「第九部的替身我基本没看见」。实测：
  - 现有 154 个替身里，**第 9 部 0 个**、第 8 部仅 7 个
  - 根因：主数据源 `jojowiki.com/Stand_Stats` 那张表**不含第 8/9 部的多数替身**
  - 从 Character 页的 `|stand = [[X]]` 可反查出缺失清单：
      第 9 部 14 个、第 8 部 21 个（含 Wonder of U / Speed King 等主要替身）

== ★★ 关键约束：六维官方未评级 ★★
实测 November Rain / Wonder of U / Speed King 的 infobox：
    |destpower =   |speed =   |range =   |stamina =   |precision =   |potential =
**全部为空**（第 7 部之后官方基本不再给六维评级；Ozon Baby 的 range
也只是描述文字 "100-meter radius"，不是等级）。

→ 六维一律**留空（NULL）**，绝不填 0。
   这与项目既有硬规则一致：缺失用 NULL 表达，雷达图标注「无数据」。
   填 0 会被读成「能力为零」，比不画更糟。

== 数据来源 ==
- 替身清单：Category:Part 8/9 Characters 的角色页 `|stand = [[X]]`
- 替身字段：替身页的 `{{Stand Infobox}}` 参数
    title / ja_kanji / ja_romaji / user / namesake / type / mangadebut
- 类型：用 Category:XXX Stands 分类（wikitext 里的 `{{ST|Close}}` 是缩写）

用法：
    python dataset/sources/jojowiki/fetch_extra_stands.py
输出：
    dataset/sources/jojowiki/extra_stands.json
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PROC = ROOT / "dataset" / "processed"
OUT = Path(__file__).resolve().parent / "extra_stands.json"

PX = os.environ.get("https_proxy") or os.environ.get("http_proxy")
OP = urllib.request.build_opener(
    urllib.request.ProxyHandler({"http": PX, "https": PX}))
OP.addheaders = [("User-Agent", "Mozilla/5.0 (research; educational; rag-kb)")]

API = "https://jojowiki.com/api.php"
# 部数 → (wiki 分类, 部名)
PARTS = {8: ("Part 8 Characters", "JoJolion"),
         9: ("Part 9 Characters", "The JOJOLands")}


def _get(url: str, retries: int = 2) -> dict:
    for a in range(retries + 1):
        try:
            return json.loads(OP.open(url, timeout=60).read())
        except Exception:                       # noqa: BLE001
            if a >= retries:
                raise
            time.sleep(1.5 * (a + 1))
    return {}


def category_members(cat: str) -> list[str]:
    u = (f"{API}?action=query&list=categorymembers"
         f"&cmtitle=Category:{urllib.parse.quote(cat)}"
         f"&cmlimit=500&format=json&formatversion=2")
    d = _get(u)
    return [x["title"] for x in d.get("query", {}).get("categorymembers", [])]


def wikitext(title: str) -> str | None:
    u = (f"{API}?action=parse&prop=wikitext&redirects=1"
         f"&format=json&formatversion=2&page={urllib.parse.quote(title)}")
    d = _get(u)
    if "error" in d:
        return None
    return d["parse"]["wikitext"]


def field(v: str) -> str | None:
    """清掉 wikitext 标记，留下可读值。"""
    if not v:
        return None
    v = re.sub(r"<ref[^>]*>.*?</ref>", "", v, flags=re.S)
    v = re.sub(r"<ref[^>]*/>", "", v)
    v = re.sub(r"\{\{[Ww]\|([^}|]+)[^}]*\}\}", r"\1", v)      # {{W|X}} → X
    v = re.sub(r"\{\{[Ss]\|([^}|]+)[^}]*\}\}", r"(\1)", v)     # {{S|X}} → (X)
    v = re.sub(r"\{\{[^}|]+\|([^}|]+)[^}]*\}\}", r"\1", v)     # 其他模板取首参
    v = re.sub(r"\[\[[^\]|]*\|([^\]]*)\]\]", r"\1", v)
    v = re.sub(r"\[\[([^\]]*)\]\]", r"\1", v)
    v = re.sub(r"<br\s*/?>", " / ", v, flags=re.I)
    v = re.sub(r"<[^>]+>", "", v)
    v = re.sub(r"'{2,5}", "", v)
    return re.sub(r"\s+", " ", v).strip() or None


def extract_infobox_params(wt: str) -> dict[str, str]:
    """取 {{Stand Infobox}} 的参数。

    ★★ 为什么不能用正则截块 ★★
    实测踩坑：infobox 的 `image = {{Tabber|…}}` **嵌套了模板**，
    非贪婪的 `\\{\\{Stand Infobox(.*?)\\n\\}\\}` 会在**嵌套的 `}}` 处提前截断**
    → 后面的 ja_kanji / user / type 全部丢失。
    实测 Tusk / Wonder of U 就是这样丢了 owner 和日文名。

    → 改用**深度计数**找到最外层 `}}` 的位置（与 wash.py 同一教训）。
    """
    m = re.search(r"\{\{Stand(?: Infobox)?\s*\n", wt)
    if not m:
        return {}
    i = m.end()
    depth = 1                      # 已进入一层 {{ }}
    # 从 infobox 开头逐字符扫，统计嵌套深度
    j = m.start() + 2              # 跳过 "{{"
    n = len(wt)
    while j < n and depth > 0:
        if wt.startswith("{{", j):
            depth += 1
            j += 2
            continue
        if wt.startswith("}}", j):
            depth -= 1
            j += 2
            continue
        j += 1
    body = wt[i:j - 2] if depth == 0 else wt[i:i + 4000]

    params: dict[str, str] = {}
    for line in body.split("\n"):
        line = line.strip()
        if line.startswith("|") and "=" in line:
            k, v = line[1:].split("=", 1)
            params.setdefault(k.strip().lower(), v.strip())
    return params


def parse_stand(title: str) -> dict | None:
    wt = wikitext(title)
    if not wt:
        return None
    params = extract_infobox_params(wt)
    # 类型：用 Category:XXX Stands 分类，**排除 Part N Stands / Stands 本身**
    #   ★ 实测踩坑：`Part 8 Stands`（部数分类）排在前头，
    #     用 next() 取第一个会把类型写成 "Part 8"。
    cats = re.findall(r"\[\[Category:([^\]]+)\]\]", wt)
    #   ★ 注意：这里 c 是**完整分类名**（形如 "Part 8 Stands"），
    #     所以排除时正则必须带 " Stands" 后缀 ——
    #     早先写成 `Part \d+` 漏掉了后缀，导致类型变成 "Part 8Close-Range"。
    type_cats = [c[:-len(" Stands")] for c in cats
                 if c.endswith(" Stands")
                 and not re.fullmatch(r"Part \d+ Stands|Stands", c)]
    stype = "".join(type_cats) or None
    # 部数：同样从**替身页自己的分类**取，而不是「哪个角色页引用了它」
    #   ★ 实测踩坑：Tusk 被第 8 部角色页（Johnny Joestar）引用，
    #     但它实际是第 7 部 → 按引用页归部会错。
    pm = re.search(r"\[\[Category:Part (\d+) Stands\]\]", wt)
    wiki_part = int(pm.group(1)) if pm else None
    return {
        "title": title,
        "name_en": field(params.get("title", "")) or title,
        "name_ja": field(params.get("ja_kanji", "")),
        "name_romaji": field(params.get("ja_romaji", "")),
        "owner_name": field(params.get("user", "")),
        "reference": field(params.get("namesake", "")),
        "stand_type": stype,
        "manga_debut": field(params.get("mangadebut", "")),
        "anime_debut": field(params.get("animedebut", "")),
        "wiki_part": wiki_part,
        # ★ 六维：官方未评级 → 一律 None，不填 0
        "stats": {k: None for k in
                  ("destpower", "speed", "range",
                   "stamina", "precision", "potential")},
        "stats_available": bool(field(params.get("stats", ""))),
    }


def main() -> int:
    sys.path.insert(0, str(ROOT / "dataset" / "pipeline"))
    from encode import make_stand_id                    # noqa: E402

    stands = json.loads((PROC / "stands.json").read_text(encoding="utf-8"))
    known = {(s.get("name_en") or "").strip().lower() for s in stands}
    known_ids = {s.get("stand_id") for s in stands}

    added: list[dict] = []
    for part, (cat, part_name) in PARTS.items():
        chars = [c for c in category_members(cat)
                 if not c.startswith(("Template:", "Category:",
                                      "Minor", "Unnamed"))]
        print(f"\n=== 第 {part} 部：扫描 {len(chars)} 个角色页 ===")
        found: dict[str, str] = {}
        for c in chars:
            wt = wikitext(c)
            if not wt:
                continue
            for m in re.findall(r"\|\s*stand\s*=\s*\[\[([^\]|]+)", wt, re.I):
                found.setdefault(m.strip(), c)
            time.sleep(0.25)
        print(f"  角色页给出 {len(found)} 个替身")

        for stitle, owner in sorted(found.items()):
            if stitle.lower() in known:
                continue
            rec = parse_stand(stitle)
            if not rec:
                print(f"    – {stitle}：页面不存在，跳过")
                continue
            sid = make_stand_id(rec["name_en"])
            if sid in known_ids:
                print(f"    – {stitle}：id 已存在({sid})，跳过")
                continue
            # ★ 部数优先用替身页自己的 Part N 分类（比「哪个角色页引用它」可靠）
            rec_part = rec.pop("wiki_part", None) or part
            rec.update({"stand_id": sid, "part": rec_part,
                        "part_name_en": PARTS.get(rec_part, (None, part_name))[1],
                        "wiki_owner_page": owner})
            added.append(rec)
            known_ids.add(sid)
            known.add(stitle.lower())
            six = sum(1 for v in rec["stats"].values() if v)
            print(f"    ✓ {rec['name_en']:30s} ({sid})  "
                  f"第{rec_part}部  类型={rec['stand_type'] or '-'}  "
                  f"六维 {six}/6")
            time.sleep(0.3)

    OUT.write_text(json.dumps(added, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    n_stats = sum(1 for r in added if any(r["stats"].values()))
    print(f"\n{'='*62}")
    print(f"新增 {len(added)} 个替身（第8部 "
          f"{sum(1 for r in added if r['part']==8)} / 第9部 "
          f"{sum(1 for r in added if r['part']==9)}）")
    print(f"★ 其中有六维数据的：{n_stats} 个"
          f"（其余 {len(added)-n_stats} 个官方未评级 → 留空）")
    print(f"→ {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

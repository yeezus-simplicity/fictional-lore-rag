"""给 113 个正传地点取中文名（M65）。**每条都带来源与依据。**

输入：`dataset/processed/locations.json`
输出：`dataset/processed/location_name_zh.json`

== ★★ 为什么不能"自动跑一遍就完"（先测过才这么写）★★
先量了萌娘的命中率，结论与预期相反：
  · 用**中文名**查 → 命中率高（`恶魔掌心` / `勒索之路` / `杜王町` / `乔斯达宅邸` 都在）
  · 用**英文名 / 日文名**查（`titles` 精确）→ 16 个里只 1 个，且是误命中
    （`シンデレラ` → 灰姑娘，是童话不是那家美容院）
  · 用**英文名**走搜索 API → 少数能中（`Air Supplena Island` → `爱雅·萨弗雷拉岛`），
    多数返回无关条目（`Angelo Rock` → `鬼泣5`）
→ **"中文名"正是我们要解决的问题，而它恰恰是唯一好用的检索词**（鸡生蛋）。
   所以自动化只能覆盖一部分，**必须如实标注每个名字的来源与可信度**，
   而不是假装全自动。

== 本脚本的做法（★ 只有拿到"依据"才收）==
  对每个地点：
    1. 用英文名走萌娘搜索 → 取候选标题
    2. **校验候选条目的正文里是否出现该地点的英文名或日文名**
       —— 这是「凭什么认为这个中文条目就是它」的依据，与 location_mentions
          的「必须有语料证据」是同一条原则
    3. 通过校验 → 记为 `source=moegirl_search`（附证据片段）
  另外两条来源（可信度不同，分开标注）：
    · `source=ja_kanji` —— 日文名**未含假名**（纯汉字）且长度合理时可直接用
      （如 `香港`）。★ 含假名的一律不用：`アンジェロ岩` 不能切成「安杰罗岩」这种
      机械结果 —— M41 的教训就是自造词（「白金星」）会污染检索。
    · `source=manual` —— 人工补录（见 zh_locations_manual.json），**待复核**

用法：
    python dataset/sources/zh_names/fetch_location_zh.py            # 增量（跳过已抓的）
    python dataset/sources/zh_names/fetch_location_zh.py --refresh  # 全部重查
"""
from __future__ import annotations

import argparse
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
LOCATIONS = PROC / "locations.json"
OUT = PROC / "location_name_zh.json"
MANUAL = Path(__file__).resolve().parent / "zh_locations_manual.json"

API = "https://moegirl.uk/api.php"
_PX = os.environ.get("https_proxy") or os.environ.get("http_proxy")
_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({"http": _PX, "https": _PX}))
_OPENER.addheaders = [("User-Agent", "Mozilla/5.0 (research; rag-kb)")]

_HAN = re.compile(r"[\u4e00-\u9fff]")
_KANA = re.compile(r"[\u3040-\u30ff]")
# 候选标题里明显与地点无关的（消歧义/作品名），直接否掉
_BAD_TITLE = re.compile(r"^(鬼泣|心灵终结|炽天使|[《「])")

# ★★ 泛地理词：**有通行中文译名，不需要搜、不该搜** ★★
#   实测教训：这类词走萌娘搜索会**大量误命中** ——
#     用「全文含该英文名」做校验时，一个**声优页面**也会提到 Florida/Capri，
#     于是一堆无关条目被当成译名（`Capri → 荒浪和沙`、`Egypt → 埃及九荣神`、
#     `England → 战舰少女:纳尔逊`）。
#   ★ 这与 location_mentions 里 `Forever` 误命中是**同一个坑**，
#     我在那儿处理了（AMBIGUOUS 清单），在这里却忘了应用 —— 泛地名同样危险。
#   → 这类地点直接给标准译名（confidence=high），不进自动搜索。
GENERIC_ZH = {
    "Egypt": "埃及", "England": "英国", "Florida": "佛罗里达",
    "Cairo": "开罗", "Italy": "意大利", "Rome": "罗马", "India": "印度",
    "Hong Kong": "香港", "Singapore": "新加坡", "Germany": "德国",
    "Mexico": "墨西哥", "Greece": "希腊", "France": "法国",
    "China": "中国", "Japan": "日本", "United States": "美国",
    "Venice": "威尼斯", "Naples": "那不勒斯", "Capri": "卡普里岛",
    "Colosseum": "罗马斗兽场", "New York City": "纽约市",
    "Pakistan": "巴基斯坦", "Switzerland": "瑞士", "Tokyo": "东京",
    "South China Sea": "南海", "Red Sea": "红海", "San Diego Beach": "圣迭戈海滩",
}


def _api(params: dict) -> dict:
    u = API + "?" + urllib.parse.urlencode(params)
    return json.loads(_OPENER.open(u, timeout=40).read())


def search_titles(q: str, limit: int = 5) -> list[str]:
    try:
        d = _api({"action": "query", "list": "search", "srsearch": q,
                  "srlimit": str(limit), "format": "json", "formatversion": "2"})
        return [x["title"] for x in d.get("query", {}).get("search", [])]
    except Exception:                                          # noqa: BLE001
        return []


def page_text(title: str) -> str:
    try:
        d = _api({"action": "query", "prop": "revisions", "rvprop": "content",
                  "rvslots": "main", "titles": title,
                  "format": "json", "formatversion": "2"})
        pg = (d.get("query", {}).get("pages") or [{}])[0]
        if "missing" in pg:
            return ""
        return pg["revisions"][0]["slots"]["main"]["content"]
    except Exception:                                          # noqa: BLE001
        return ""


def verify(cand_title: str, en: str, ja: str, text: str) -> str | None:
    """候选条目的正文里必须出现该地点的英文名或日文名 —— 这是"凭什么"的依据。

    返回命中的那个名字（作为证据），否则 None。
    """
    if not text:
        return None
    for term in (en, ja):
        if not term or len(term) < 3:
            continue
        if _HAN.search(term) or _KANA.search(term):
            if term in text:                    # 中日文：直接子串
                return term
        else:
            pat = re.compile(r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])",
                             re.IGNORECASE)
            m = pat.search(text)
            if m:
                return term
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="忽略缓存重查")
    # ★ --fast：跳过萌娘候选搜索。候选**不被采纳**（见 ② 的说明），
    #   只是给人工复核的参考；实测搜一遍要 9 分钟，日常重建不需要。
    ap.add_argument("--fast", action="store_true", help="跳过候选搜索（快）")
    ap.add_argument("--delay", type=float, default=0.3)
    args = ap.parse_args()

    locdata = json.loads(LOCATIONS.read_text(encoding="utf-8"))
    kept = [e for e in locdata["locations"] if e.get("included")]
    manual_raw = (json.loads(MANUAL.read_text(encoding="utf-8"))
                  if MANUAL.exists() else {})
    # ★★ 取结构里的 entries —— 我第一版直接拿顶层 dict 当映射，
    #   而表的结构是 {"_note": …, "entries": {…}} → `lid in manual` **永远为假**，
    #   人工补录静默失效（输出"人工 0"，不报错）。
    #   ★ 与"日式写法""误命中"一样，属于"看着跑通了、其实没生效"那一类。
    manual = manual_raw.get("entries", manual_raw)
    manual = {k: v for k, v in manual.items() if not k.startswith("_")}
    print(f"地点 {len(kept)} 个 · 人工补录表 {len(manual)} 条")
    if not _PX:
        print("⚠ 未检测到代理环境变量，直连可能超时")

    cache: dict[str, dict] = {}
    if OUT.exists() and not args.refresh:
        try:
            old = json.loads(OUT.read_text(encoding="utf-8"))
            cache = {e["location_id"]: e for e in old.get("entries", [])}
            print(f"已有 {len(cache)} 条，增量续查")
        except (json.JSONDecodeError, KeyError):
            pass

    out: list[dict] = []
    stat = {"generic_zh": 0, "moegirl_search": 0, "ja_kanji": 0, "manual": 0, "none": 0}
    for i, L in enumerate(kept, 1):
        lid, en = L["location_id"], L["title"]
        ja = L.get("name_ja") or ""
        rec = cache.get(lid)
        if rec is None:
            rec = {"location_id": lid, "title": en, "name_ja": ja,
                   "name_zh": None, "source": None, "evidence": None,
                   "confidence": None}
            # ---- ① 泛地理词 → 标准译名（最可靠，且不该去搜）----
            if en in GENERIC_ZH:
                rec.update(name_zh=GENERIC_ZH[en], source="generic_zh",
                           evidence="通行中文译名（泛地理词，不走搜索）",
                           confidence="high")
            # ---- ② 自动搜索只产出**候选**，不直接采纳 ★★ ----
            #   实测：我的校验是「候选条目正文里含该地点的英文/日文名」，
            #   这对**泛地名无效** —— 一个声优页面也会提到 Florida/Capri，
            #   于是混进一堆无关条目：
            #     `Courtyard → 崩坏3-Elysium-Original Soundtrack`
            #     `Hawaii → 千叶瑞己` / `Pompeii → 海尔兄弟` / `Egypt → 埃及九荣神`
            #     `Libeccio → 舰队Collection:西南风` / `New Jersey → 碧蓝航线:新泽西`
            #   → 所以自动结果**必须人工确认后才可用**，这里只落 candidates。
            cands: list[dict] = []
            if not rec["name_zh"] and not args.fast:
                for cand in search_titles(en)[:4]:
                    if not _HAN.search(cand) or _BAD_TITLE.search(cand):
                        continue
                    ev = verify(cand, en, ja, page_text(cand))
                    if ev:
                        cands.append({"zh": cand, "from": "moegirl_search",
                                      "evidence": f"正文含「{ev}」"})
                    time.sleep(args.delay)
                # 日文纯汉字名 → 也**只是候选**：实测常是日式写法
                # （`女子監`≠女子监狱、`広瀬邸` 的「広」是日文简体字）
                if ja and _HAN.search(ja) and not _KANA.search(ja):
                    cands.append({"zh": ja, "from": "ja_kanji",
                                  "evidence": "日文名即纯汉字（★ 可能是日式写法）"})
            rec["candidates"] = cands
            # ---- ③ 人工确认（含**已复核过的**萌娘条目）----
            #     顺序是刻意的：用户要"萌娘自动 + 人工兜底"，
            #     而实测证明自动那半必须人工过一道，所以确认结果都放人工表。
            if not rec["name_zh"] and lid in manual:
                m = manual[lid]
                rec.update(name_zh=m["zh"], source="manual",
                           evidence=m.get("note", "人工补录"),
                           confidence=m.get("confidence", "medium"))
            if rec["name_zh"]:
                rec["confidence"] = rec["confidence"] or "medium"
                stat[rec["source"]] += 1
            else:
                stat["none"] += 1
            time.sleep(args.delay)
        out.append(rec)
        if i % 25 == 0 or i == len(kept):
            print(f"  [{i}/{len(kept)}] 中文名 {sum(1 for r in out if r['name_zh'])}")

    OUT.write_text(json.dumps(
        {"n_locations": len(kept),
         "n_with_zh": sum(1 for r in out if r["name_zh"]),
         "by_source": stat,
         "_note": ("★ 每条都带 source + evidence + confidence，可逐条复核。"
                   "moegirl_search=萌娘搜索且**候选条目正文含该地点英文/日文名**；"
                   "ja_kanji=日文名为纯汉字（可直接用）；manual=人工补录（待复核）。"
                   "★ 含假名的日文名**不**做机械转换（M41 教训：会造出自造词）。"),
         "entries": out},
        ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"\n{'='*62}")
    print(f"有中文名 {sum(1 for r in out if r['name_zh'])}/{len(kept)}")
    print(f"  泛地理标准译名 {stat['generic_zh']} · 萌娘(有依据) {stat['moegirl_search']} · "
          f"日文纯汉字(low) {stat['ja_kanji']} · 人工 {stat['manual']} · 仍缺 {stat['none']}")
    print(f"→ {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

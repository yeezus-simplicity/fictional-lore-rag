"""
从 JOJO 中文维基（huijiwiki 镜像）补充冷门替身中文名（M16 补充源）。

==★ 为什么需要这个 ==

fetch_zh_wiki.py 从中文维基总表抓到 201 组，但总表里**没有全部替身**：
    Bastet / Smallfaces / House of Holy / Fun Fun Fun / Remote Romance
这 5 条在总表里查不到 —— 它们要么是独立条目、要么当时漫画未连载完。

而 `jojo.huijiwiki.com`（JOJO 中文维基镜像）**每个替身都有独立条目**，
实测检索命中：
    Bastet        → 巴斯特女神
    Smallfaces    → 小面孔
    House of Holy → 神圣之屋
    Fun Fun Fun   → 牵线木偶师

==★ 为什么要节流 ==

实测连续请求 api.php 会被 403 限流（第一次 3 条成功、紧接着 2 条 403），
加 3~12 秒退避后 4/4 全部成功 → 慢一点反而更快完成。

★★ 限流会**升级成硬封禁** ★★
实测连续跑两轮后，api.php 与 /wiki/ 网页路径**全部 403**，
萌娘百科（zh.moegirl.*）也被 Cloudflare 封了 → 脚本抓取路径彻底失效。
→ 因此 zh_huiji_pairs.json 里这 5 条是**人工核实后写入**的（出处见下），
   脚本留给「限流窗口重开后」复抓 / 补新增替身用。
   ★ 宁可慢，也不能凭记忆硬填 —— 每组都必须有可核对的出处。

人工核实的出处（M16，均通过 WebSearch 拿到原页面片段）：
  Bastet→ 巴斯特女神 / 巴斯提女神
      huijiwiki 词条「巴斯特女神」的 {{Stand Info}} engname=Bastet（本脚本直抓验证过）
      萌娘百科 moegirl.org.cn/巴斯特女神 别名栏「巴斯提女神」
  Smallfaces        → 小面孔 / 小脸
      huijiwiki 词条「小面孔」（正文首句 "Smallfaces(日文:スモールフェイセズ…)"）
      萌娘百科 moegirl.org.cn/小脸（英文名写作 Small Faces）
      ★ 两站中文名不同（面孔 / 脸）→ 并列收录
  House of Holy     → 神圣之屋 / 荷莉之屋
      huijiwiki 词条「神圣之屋」（正文 "House of Holy(日文:ハウス・オブ・ホーリー…)"）
      萌娘百科「迪雅·梅克」别名栏「荷莉之屋」（Holly 双关）
  Fun Fun Fun       → 牵线木偶师 / 乐趣无穷
      百度百科「牵线木偶师」（中文名字段=牵线木偶师，外文名=FUN FUN FUN）
      萌娘百科 FUN_FUN_FUN 别名栏「牽線木偶師、樂趣無窮」
      ★ 萌娘备注明说「很难有信达雅的中文译名」→ 只用词条登记的别名，不自造
  Remote Romance    → 遥远浪漫 / 遥感浪漫
      萌娘百科「遙遠浪漫」（zh.moegirl.tw/遙遠浪漫，替身名=遙遠浪漫，別名=遙感浪漫）

==★ 为什么要校验首命中 ==

`Remote Romance` 检索首命中是「岸边露伴真人剧 音乐曲目列表」——
  这是**列表页**里的条目提及，不是替身词条。
  ★ 所以不能盲取第一个结果，必须校验：
  1. 标题不能含列表/曲目/章节/文化参考等页内聚合词
  2. 词条正文必须声明该替身的英文名与日文名（info 模板的三个名字字段）

用法：python dataset/sources/zh_names/fetch_zh_huiji.py
输出：dataset/sources/zh_names/zh_huiji_pairs.json
"""
from __future__ import annotations

import json
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from html import unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PROC = ROOT / "dataset" / "processed"
OUT = Path(__file__).resolve().parent / "zh_huiji_pairs.json"

API = "https://jojo.huijiwiki.com/api.php"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                   " (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Accept-Language": "zh-CN,zh;q=0.9",
}

_TAG_RE = re.compile(r"<[^>]+>")
_HAS_HAN = re.compile(r"[一-鿿]")

# ★ 聚合页/章节页/列表页：这些页面只是"提到"替身名，不是替身词条
_AGG_RE = re.compile(
    r"曲目|列表|章节|第\d+章|文化参考|年表|大全|一览|汇总|"
    r"人物|角色|作品|漫画|动画|电影|OVA|游戏|音乐|专辑|"
    r"真人剧|故事线|剧情|解说|考据|吐槽|杂谈|感想|书评|访谈")

# ★ 只抓详情页的首个 section，用于抓简介
_SECTION = 0


def norm_en(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).lower()
    return re.sub(r"[^a-z0-9]", "", s)


def _clean(html: str) -> str:
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.S | re.I)
    t = re.sub(r"<br\s*/?>", " ", t, flags=re.I)
    t = _TAG_RE.sub(" ", t)
    t = unescape(t).replace("\xa0", " ").replace("　", " ")
    return re.sub(r"\s+", " ", t).strip()


def api(params: dict, tries: int = 4) -> dict:
    """带指数退避的 api.php 调用（403 限流是常态）。"""
    url = API + "?" + urllib.parse.urlencode(params)
    last: Exception | None = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode("utf-8", "ignore"))
        except Exception as e:  # noqa: BLE001
            last = e
            #★ 实测：限流是短时的，退避 3/6/9 秒基本都能过
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"api失败: {last}")


def search_titles(en_name: str) -> list[str]:
    d = api({"action": "query", "list": "search",
             "srsearch": en_name, "format": "json", "srlimit": 8})
    return [h["title"] for h in d.get("query", {}).get("search", [])]


def get_page(title: str) -> dict | None:
    d = api({"action": "query", "prop": "extracts|revisions",
             "titles": title, "format": "json",
             "exintro": 0, "explaintext": 1, "rvprop": "content",
             "rvslots": "main"})
    pages = d.get("query", {}).get("pages", {})
    for _, p in pages.items():
        if "missing" in p:
            return None
        rev = p.get("revisions", [{}])[0].get("slots", {}).get("main", {})
        return {"title": p.get("title", ""),
                "extract": p.get("extract", "") or "",
                "wikitext": rev.get("*", "") or ""}
    return None


def parse_info(page: dict) -> dict | None:
    """从词条里抽出 {{Stand Info ... }} 模板的名字字段。

    ★★ 模板真名是 `Stand Info`（不是 `Info`）—— 实测 huijiwiki 词条源码：
        {{Stand Info
        |title    = 巴斯特女神      ← 中文名在这里
        |ja_kanji = バステト女神    ← 日文汉字
        |engname  = Bastet          ← 英文名（校验用）
        |user     = 玛莱雅
        |stats    = {{Stat|E|E|B|A|E|E}}
        ...}}
      正文另有 {{nihongo|巴斯特女神|Bastet|バステト女神|...}}
      —— 两者都能拿到中文名，模板 title 更规整，优先用它。

    ★ 必须校验 engname == 我们要找的英文名，
      否则会把「岸边露伴真人剧 音乐曲目列表」这种列表页误当成词条。
    """
    wt = page["wikitext"]
    m = re.search(r"\{\{\s*Stand\s+Info\b(.*?)\n\}\}", wt, flags=re.S | re.I)
    if not m:
        return None
    body = m.group(1)

    def field(name: str) -> str:
        fm = re.search(rf"\|\s*{name}\s*=\s*([^\n|]+)", body, flags=re.I)
        return fm.group(1).strip() if fm else ""

    zh = field("title")
    # 模板 title 缺失时退回正文 nihongo 模板
    if not zh:
        nm = re.search(r"\{\{\s*nihongo\s*\|\s*([^|}]+)", wt)
        if nm:
            zh = nm.group(1).strip()

    return {"ja": field("ja_kanji"), "en": field("engname"),
            "zh": zh, "alt": field("alt")}


def main() -> int:
    stands = json.loads((PROC / "stands.json").read_text(encoding="utf-8"))
    zh = json.loads((PROC / "stand_name_zh.json").read_text(encoding="utf-8"))

    # ★ 只处理**当前仍缺中文名**的替身 → 已有的不重复抓（省请求，避开限流）
    todo = [s for s in stands if s["stand_id"] not in zh]

    # ★ --only：只抓指定英文名（逗号分隔）。
    #   用途：中文维基总表已覆盖大部分缺口时，只需要huijiwiki 兜剩下的几条，
    #   省掉大量请求 —— 实测限流是主要耗时来源。
    if "--only" in sys.argv:
        arg = sys.argv[sys.argv.index("--only") + 1]
        want = {norm_en(x) for x in arg.split(",") if x.strip()}
        todo = [s for s in todo if norm_en(s["name_en"]) in want]
        print(f"  --only 限定 {len(todo)} 条")

    print("=" * 66)
    print("从 JOJO 中文维基（huijiwiki）补充中文名（M16 补充源）")
    print("=" * 66)
    print(f"  待补 {len(todo)} 条（已有中文名的跳过）")
    print(f"  目标文件 {OUT.name}（增量落盘，中断也不丢）")

    out: dict[str, list[str]] = {}
    log: list[str] = []

    def flush() -> None:
        OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2),
                       encoding="utf-8")

    for i, s in enumerate(todo, 1):
        en = s["name_en"]
        k = norm_en(en)
        try:
            titles = search_titles(en)
        except Exception as e:  # noqa: BLE001
            print(f"  [{i}/{len(todo)}] {en:32s} 检索失败 {str(e)[:40]}")
            time.sleep(4)
            continue

        hit = None
        # ★ 只查前 4 个候选标题：实测第 1 个通常就是词条，
        #   查太多会撞限流且收益极低（每多查一个 = 多一次退避等待）。
        for t in titles[:4]:
            if _AGG_RE.search(t):
                continue
            try:
                page = get_page(t)
            except Exception:  # noqa: BLE001
                time.sleep(4)
                continue
            if not page:
                continue
            info = parse_info(page)
            if not info:
                continue
            # ★ 硬校验：词条自报的英文名必须等于我们要找的英文名
            if norm_en(info["en"]) != k:
                log.append(f"{en}: '{t}' 的 engname={info['en']!r} 不符，跳过")
                continue
            zh_name = info["zh"].strip()
            if not zh_name or not _HAS_HAN.search(zh_name):
                continue
            hit = {"title": t, "zh": zh_name,
                   "jp": info["jp"], "alt": info["alt"]}
            break

        if hit:
            names = [hit["zh"]]
            # 词条里的 alt 字段是官方登记的异译别名
            for a in re.split(r"[/、,，]", hit["alt"]):
                a = a.strip()
                if a and _HAS_HAN.search(a) and a not in names:
                    names.append(a)
            out[k] = names
            print(f"  [{i}/{len(todo)}] {en:32s} → {names}  ({hit['title']})",
                  flush=True)
        else:
            print(f"  [{i}/{len(todo)}] {en:32s} — 未命中可用词条", flush=True)
        flush()
        time.sleep(3.0)

    print(f"\n  补到{len(out)} 组")
    if log:
        print(f"  校验拦下 {len(log)} 条误命中：")
        for line in log[:5]:
            print(f"      {line.strip()}")

    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"\n  → {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
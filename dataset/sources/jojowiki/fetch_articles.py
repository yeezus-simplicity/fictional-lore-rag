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
import re
import sys
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


def _api_json(params: dict) -> dict:
    u = API + "?" + urllib.parse.urlencode(params)
    return json.loads(_OPENER.open(u, timeout=60).read())


def fetch_location_titles() -> list[str]:
    """★ M65：地点清单 —— 反查所有用了 `{{Location Infobox}}` 的页面。

    为什么不用分类：实测 `Category:Locations` 只有 **30** 个成员，
    且几乎全是 `User:.../Sandbox` 与 `File:` —— **分类不可用**
    （`Category:Part 1 Locations` 11 个、`Part 3 Locations` 20 个，
    而 `Steel Ball Run Locations` / `JoJolion Locations` 均为 **0**）。
    → 用模板反查（`list=embeddedin`）能拿到完整清单：
      实测 177 项，剔除沙盒/模板/文件后 **159 个正文条目**。

    ★ 与替身/角色不同：地点名**带括号**（`Morioh (JoJolion)`），
      且括号是**区分同名不同地点的唯一依据** → 必须原样保留
      （见 `load_db._loc_id` 的说明）。
    """
    titles: list[str] = []
    cont: dict = {}
    while True:
        p = {"action": "query", "list": "embeddedin",
             "eititle": "Template:Location Infobox", "eilimit": "500",
             "format": "json", "formatversion": "2"}
        p.update(cont)
        d = _api_json(p)
        titles += [x["title"] for x in d.get("query", {}).get("embeddedin", [])]
        cont = d.get("continue") or {}
        if not cont:
            break
    bad = ("User:", "Template:", "Category:", "Talk:", "File:", "Module:", "Help:")
    return sorted({t for t in titles if not t.startswith(bad)})


def fetch_character_titles() -> list[str]:
    """★ M70：角色清单 —— 取 `Category:Part N Characters`（N=1..9）。

    == 为什么必须补这个清单 ==
    原来的角色名单是**从 `stands.json` 的 `owner_name` 反推**的
    （`build_targets` 的第二个循环）—— 即「谁是替身持有者就抓谁」。
    这在 M66 时看不出问题（168 个角色档案，主流角色都在），
    但它有一个**结构性盲区**：

        **不是任何替身持有者的角色，从来不会进抓取队列。**

    用户实测：`lisalisa 二乔的母亲` 检索不到。
    查证结果：`Lisa Lisa` 在源站**有独立条目**、在语料里被提到 35 次，
    但 `523` 个已抓页面里没有她，`characters` 表里也没有。
    同类还有 `Jonathan Joestar`、`Caesar Anthonio Zeppeli`、`Holly Joestar`
    —— 第 1、2 部的核心角色几乎全军覆没，因为波纹时代**没有替身**，
    也就没有任何 `owner_name` 会提到他们。

    ★ 这个盲区和 M65 地点那个坑是**同一个形状**：
      「用模板/分类反查」比「从业务字段反推」可靠。

    == 为什么用分类而不是 `Category:Characters` ==
    实测 `Category:Characters` 是**顶层容器**，只有 9 个成员且 8 个是子分类、
    1 个是 `Minor Characters` —— 直接取会得到近乎空的结果（与 M65 的
    `Category:Locations` 只有 30 个成员同一形状）。
    → 必须逐个取 `Category:Part N Characters`（用户口径：**只保留 9 部正传**），
      实测 9 个分类并集 = **382** 个 ns0 成员。

    == 分类里也不干净，必须过滤 ==
    实测这 382 个里混着三类非角色页：
      ① 列表页     `Minor Characters/Part 1`、`Unnamed Characters/Part 5`
      ② 替身页     `Foo Fighters`、`Weather Report`、`Anubis`（分类交叉）
      ③ 组织/条目页 `Locacaca Organization`、`List of Horses`
    → 用下面的排除规则处理；**判定不了的宁可留下**（多一个页面
      最多浪费一次请求，漏一个角色则是用户检索不到）。
    """
    titles: list[str] = []
    cont: dict = {}
    for part in range(1, 10):                  # ★ 用户口径：只 9 部正传
        cat = f"Category:Part {part} Characters"
        c: dict = {}
        while True:
            p = {"action": "query", "list": "categorymembers",
                 "cmtitle": cat, "cmnamespace": "0",
                 "cmlimit": "500", "format": "json",
                 "formatversion": "2"}
            p.update(c)
            d = _api_json(p)
            titles += [x["title"] for x in d.get("query", {}).get("categorymembers", [])]
            c = d.get("continue") or {}
            if not c:
                break

    bad_ns = ("User:", "Template:", "Category:", "Talk:", "File:",
              "Module:", "Help:", "MediaWiki:")
    out: set[str] = set()
    for t in titles:
        if t.startswith(bad_ns):
            continue
        # ① 列表页：斜杠形如 `Minor Characters/Part 1`
        if "/" in t and t.split("/")[-1].strip().startswith("Part"):
            continue
        # ② ★ M72：语言子页（`Urmd Avdul/fr`、`Gray Fly/es`）
        #    源站的 Part N Characters 分类里混着语言子页，实测 2 个。
        #    build_locations 早就有 LANG_SUFFIX 过滤，角色侧漏了。
        if LANG_SUFFIX.search(t):
            continue
        # ③ ★ M72：重定向页 —— 见下面 _redirect_targets() 的说明
        if t in _redirect_targets():
            continue
        out.add(t)
    return sorted(out)


# 语言子页后缀（与 build_locations.LANG_SUFFIX 同一套，不另写一份）
LANG_SUFFIX = re.compile(r"/(fr|zh|es|de|it|ja|pt|ru|pl|ko)$", re.I)


def _redirect_targets() -> set[str]:
    """M72：已知的**重定向**标题集合 —— 它们会被抓成正文的副本。

    ★ 为什么会有重定向混进名单：
      `fetch_wikitext()` 用 `redirects=1`，所以抓 `DIO` 会得到
      `Dio Brando` 的**正文**（125,941 字符，一模一样）。
      而源站的 `Category:Part N Characters` **同时列了重定向和它的目标**
      → 同一个角色被抓成两条，`characters` 表里出现
        `dio` 和 `dio_brando` 两行、篇章答案里同一个角色出现两次。

    实测 7 组 16 个页面，其中 12 个是重定向：
        DIO                        → Dio Brando
        Anjuro "Angelo" Katagiri   → Anjuro Katagiri
        Shigekiyo "Shigechi" Yangu → Shigekiyo Yangu
        Former Tama                → Tama
        Elder A. Phex Brother      → A. Phex Brothers
        Younger A. Phex Brother    → A. Phex Brothers
        Holy Joestar               → Holy Kujo
        Star Platinum: The World   → Star Platinum（段落锚点）
        Tusk ACT1/ACT2/ACT3/ACT4   → Tusk

    ★ 后 5 个是**替身**而不是角色 —— 它们的重复来自另一条路：
      `build_targets` 把 stands.json 里每个替身名都加成 target，
      而 Tusk ACT1-4 是 M1 遗留的「形态组未消解」问题
      （load_db 里为此专门建了聚合父记录）。
      → 替身本身仍按 M1 的模型登记（那是另一个问题），
        但**不该抓 5 份一模一样的页面**。

    ★ 判定方法：`action=query&prop=revisions` 拿**未经重定向解析**的原文，
      以 `#REDIRECT` 开头即为重定向。
      （★ 不要用 `action=parse` 判：它对 `redirects` 参数的行为不一致，
       实测同一个 `Holy Joestar` 两次调用返回不同 title。）
    ★ 只列**实测确认过**的，不做"猜哪个是规范名"的自动判断 ——
     宁可漏一个重定向，也不要把正文页误判成重定向而丢掉真数据。
    """
    return {
        # 角色侧（来自 Category:Part N Characters）
        "DIO", 'Anjuro "Angelo" Katagiri', 'Shigekiyo "Shigechi" Yangu',
        "Former Tama", "Elder A. Phex Brother",
        "Younger A. Phex Brother", "Holy Joestar",
        # 替身侧（来自 stands.json 的形态组，M1 遗留）
        "Star Platinum: The World",
        "Tusk ACT1", "Tusk ACT2", "Tusk ACT3", "Tusk ACT4",
    }


def fetch_arc_titles() -> list[str]:
    """★ M70：篇章页清单 —— 反查所有用了 `{{Story Arc Infobox}}` 的页面。

    为什么必须单独抓：用户实测「勒索之路的替身是什么」答得不对。
    根因是**同名歧义** —— 源站里 `Shakedown Road` 既是地点也是篇章
    （两个独立页面，页面顶部各有 `{{Hatnote}}` 互相指路）：

        地点页：For the story arc of the same name, see
                [["Shakedown Road" (Story Arc)]]
        篇章页：For the location of the same name, see [[Shakedown Road]]

    我们只建了地点 → 用户问「这一段的替身」，被答成「这个地点关联到的替身」。
    → 必须把篇章也建起来，才能识别同名并把两种口径都给出。

    ★ 用 `embeddedin` 模板反查而不是分类：`Category:Story Arc` 之类的分类
      在源站并不完整（与 M65 的 `Category:Locations` 只有 30 个成员同形状）。
    """
    titles: list[str] = []
    cont: dict = {}
    while True:
        p = {"action": "query", "list": "embeddedin",
             "eititle": "Template:Story Arc Infobox", "eilimit": "500",
             "format": "json", "formatversion": "2"}
        p.update(cont)
        d = _api_json(p)
        titles += [x["title"] for x in d.get("query", {}).get("embeddedin", [])]
        cont = d.get("continue") or {}
        if not cont:
            break
    bad = ("User:", "Template:", "Category:", "Talk:", "File:", "Module:", "Help:")
    return sorted({t for t in titles if not t.startswith(bad)})


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


# ★★ M68：角色页标题修正表（人工确认，逐条可复核）★★
#   背景：M67 修好 `stands.json` 的 owner 粘连后，`build_targets` 里冒出一批
#   **从没进过抓取队列**的角色 —— 旧 owner 粘成一串（`Unnamed Mountain Lodge
#   Owner` 那种），那个粘名字在 wiki 上不存在 → 永远抓不到 → 永久缺失。
#   其中一部分只是**写法不一致**（wiki 真实页面名少了 `Unnamed ` 前缀），
#   另一部分 wiki 上**真的没有独立条目**（见 `NO_PAGE`）。
#   ★ 与 M41「单字名不收」/ M65 中文名 / M67 中文名重键同一套做法：
#     **人工确认优先于自动猜测**，且每条附来源。
#   ★ 实测依据（2026-10-15，逐个查 api.php）：
#     `Unnamed Mountain Lodge Owner` → `Mountain Lodge Owner`（搜索第 1 命中）
#     其余 4 个（Four Unnamed mice / Unnamed boy / Cow /
#     Whitesnake's Unknown Victim）精确查与搜索**都没有**独立条目
#     （前三个只在汇总页 `Unnamed Characters/Part 3` 里被提及）。
TITLE_FIXUPS: dict[str, str] = {
    "Unnamed Mountain Lodge Owner": "Mountain Lodge Owner",
}

# wiki 上确认**没有独立条目**的角色（如实记录，不去抓、不造数据）
#   ★ 实测依据（2026-10-15，M68 补抓时逐个 api.php 确认）：
#     这 6 个在**精确查与搜索里都没有**独立条目。
#   ★ 注意：`Echoes (ACT1/2/3)` **不在这张表里** —— 它们是 kind=**stand**的
#     形态条目（M58 形态链把形态做成了独立替身条目），wiki 上没有独立页面
#     是正常的；DB 里也没有对应的 character 行。
NO_PAGE: frozenset[str] = frozenset({
    "Four Unnamed mice",      # 只在汇总页 Unnamed Characters/Part 3 提及
    "Unnamed boy",            # 无独立条目（Anubis 的持有者之一）
    "Cow",                    # 无独立条目（Anubis 的持有者之一）
    "Whitesnake's Unknown Victim",   # 是替身 Whitesnake 的受害者，无独立条目
    "Escape Room Player",     # 匿名角色（Smallfaces 的玩家），源站无独立条目
    "Unnamed man",            # 匿名角色（Milagro Man 的使用者），源站无独立条目
})


def build_targets() -> list[dict]:
    """从 stands.json 生成待抓页面清单（替身页 + 角色页）。"""
    stands = json.loads(STANDS.read_text(encoding="utf-8"))
    # ★★ 复用 load_db 的角色 id 规则，不自己另造一套 ★★
    #   实测踩坑：stands.json 的 `owner_id` **全部为 None**（154/154），
    #   早先版本直接取它 → 角色页块拿不到归属，character_id 全空，
    #   违反 chk_chunk_owner 约束（stand_id 与 character_id 不能都为空）。
    #   → 改用与 characters 表**完全相同**的 `_char_id(split_owner_names(...))`，
    #     否则生成的 id 对不上外键，入库会被拒。
    sys.path.insert(0, str(ROOT / "database"))
    from load_db import _char_id, _loc_id, split_owner_names   # noqa: E402

    def _arc_id(title: str) -> str:
        """篇章 id：剥掉源站标题的引号与 `(Story Arc)` 后缀后规范化。

        实测篇章页标题长这样：`"Shakedown Road" (Story Arc)`
        → 引号只是源站的排版惯例、`(Story Arc)` 是命名空间后缀，
          两者都不该进 id（否则用户打「勒索之路」就匹配不上）。
        """
        t = (title or "").strip()
        if t.startswith(chr(34)) and t.endswith(chr(34)):
            t = t[1:-1]
        t = re.sub(r"\s*\(\s*story arc\s*\)\s*$", "", t, flags=re.I)
        return re.sub(r"[^a-z0-9]+", "_", t.strip().lower()).strip("_")


    targets: list[dict] = []
    seen: set[tuple] = set()

    for s in stands:
        sid = s.get("stand_id")
        nm = (s.get("name_en") or "").strip()
        if nm and ("stand", nm) not in seen:
            seen.add(("stand", nm))
            targets.append({"title": nm, "kind": "stand", "entity_id": sid})

    for s in stands:
        raw = (s.get("owner_name") or "").strip()
        if not raw:
            continue
        for o in split_owner_names(raw):
            if not o or o.lower() in ("none", "unknown"):
                continue
            # ★ M68：标题修正 + 跳过确认无条目的
            #   ★ 注意顺序：先修正再查 NO_PAGE —— 修正后的名字仍可能无条目
            #     （目前没有这种情形，但顺序写对能防将来加错表）。
            orig = o
            if o in TITLE_FIXUPS:
                o = TITLE_FIXUPS[o]
            if o in NO_PAGE:
                continue
            if ("character", o) in seen:
                continue
            seen.add(("character", o))
            targets.append({
                # ★★ entity_id 必须按**原始名字**算（`orig`，不是修正后的 `o`）★★
                #   DB 的 `characters.character_id` 来自
                #   `_char_id(split_owner_names(owner_name))`，用的是 stands.json
                #   里的原名。这里若用修正后的名字算 id，会造出一个
                #   **DB 里不存在的 character_id** → 语料块外键对不上 → 入库被拒。
                #   （我第一版就是直接用了 `o`，犯了「改标题顺手改了主键」的错。）
                "title": o, "kind": "character",
                "entity_id": _char_id(orig),
                # 记录原名，便于下游/人复核「这个 id 对应的名字是什么」
                **({"name_in_stands": orig} if orig != o else {}),
            })

    # ★ M70：补上「分类名单」这条独立来源。
    #   上面那个循环只覆盖「替身持有者」，而 Lisa Lisa / Jonathan Joestar /
    #   Caesar Zeppeli 这类**不是任何替身持有者**的角色从来不会进队列
    #   （第 1、2 部没有替身 → 波纹时代的角色整体缺失）。
    #   → 再用 `Category:Part N Characters`（9 部正传）补一遍。
    #   ★ entity_id 一律按**页面标题**算：这些角色在 `stands.json` 里
    #     没有 owner_name，所以 DB 里也不会有对应行 —— 它们是**新角色**，
    #     id 由标题生成与 `load_db._char_id` 同规则。
    try:
        cat_chars = fetch_character_titles()
    except Exception as e:                                    # noqa: BLE001
        print(f"  ⚠ 角色分类清单获取失败（{type(e).__name__}），"
              f"本次只抓替身持有者：{str(e)[:60]}")
        cat_chars = []
    n_new = 0
    for t in cat_chars:
        if ("character", t) in seen:
            continue
        # 替身页已在第一轮抓过（kind=stand）→ 同一页面不重复抓
        if ("stand", t) in seen:
            continue
        if t in NO_PAGE:
            continue
        seen.add(("character", t))
        n_new += 1
        targets.append({
            "title": t, "kind": "character", "entity_id": _char_id(t),
            "source_of_truth": "category",   # ★ 可复核：这条来自分类而非 owner_name
        })
    if n_new:
        print(f"  M70 分类补入 {n_new} 个此前从未进队列的角色"
              f"（分类共 {len(cat_chars)} 个）")

    # ★ M70：篇章页 —— 解决「同名歧义」（勒索之路既是地点也是篇章）
    try:
        arcs = fetch_arc_titles()
    except Exception as e:                                    # noqa: BLE001
        print(f"  ⚠ 篇章清单获取失败（{type(e).__name__}），本次跳过篇章："
              f"{str(e)[:60]}")
        arcs = []
    n_arc_new = 0
    for t in arcs:
        if ("arc", t) in seen:
            continue
        seen.add(("arc", t))
        n_arc_new += 1
        targets.append({"title": t, "kind": "arc",
                        "entity_id": _arc_id(t)})
    if n_arc_new:
        print(f"  M70 篇章页 {n_arc_new} 个"
              f"（用于同名消歧：地点 ↔ 篇章）")

    # ★ M65：地点页 —— 这是「第 1、2 部也能有内容」的关键。
    #   替身天然只覆盖第 3–9 部（第 1、2 部是波纹时代，**没有替身**，
    #   `stands.part` 实测第 1/2 部各 0 个），而第 1、2 部**有地点**
    #   （Ogre Street / Joestar Mansion / Air Supplena Island …）。
    try:
        locs = fetch_location_titles()
    except Exception as e:                                    # noqa: BLE001
        print(f"  ⚠ 地点清单获取失败（{type(e).__name__}），本次跳过地点："
              f"{str(e)[:60]}")
        locs = []
    for t in locs:
        if ("location", t) in seen:
            continue
        seen.add(("location", t))
        targets.append({"title": t, "kind": "location",
                        "entity_id": _loc_id(t)})
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
          f"角色 {sum(1 for t in targets if t['kind']=='character')} / "
          f"地点 {sum(1 for t in targets if t['kind']=='location')}）\n")

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

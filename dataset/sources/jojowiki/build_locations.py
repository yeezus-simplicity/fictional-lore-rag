"""把抓到的地点页解析成结构化地点表（M65）。

输入：`dataset/sources/jojowiki/articles.json` 里 `kind == "location"` 的页面
输出：`dataset/processed/locations.json`

== 为什么要有这一层 ==
地点页的 `{{Location Infobox}}` 是结构化模板，但**只有 159 个地点、
且字段齐整度不一**（实测 Angelo Rock 有 12 个字段，Devil's Palm 只有 2 个）。
把「模板解析」与「入库」分开，好处是：
  · 判据、排除原因可追溯、可复核（`part_basis` / `exclude_reason` 都落盘）
  · 排除后想改口径不用重抓（源数据 159 个全留着）

== ★★ 判据：怎么知道一个地点属于第几部 ★★
源站**没有**可靠的分部分类（实测 `Category:Steel Ball Run Locations` = 0 个），
所以只能从 `mangadebut` 推：

    带前缀：  `SBR Chapter 12` → 第7部 / `JJL …` → 第8部 / `TJL …` → 第9部
              `SO …` → 第6部（第6部两种写法都存在）
    无前缀：  `Chapter 272` —— **编号连续贯穿第 1–6 部**，按区间判：
              1–44 / 45–116 / 117–265 / 266–439 / 440–594 / 595–752

★★ 我第一版把这里写错了：正则写成「前缀 + Chapter」，而**无前缀的情况有 84 个
   （占了一半以上）** → 它们全部落到"无法解析"，第 1–6 部集体消失，
   而输出还是一个看着合理的"正传可判部 44"。
   → 现在先处理无前缀，再处理带前缀。

== 排除规则（用户口径：**只保留 9 部正传漫画**）==
  ① 命名空间页（`JoJo Wiki:` 等）—— 不是内容页
  ② 语言子页（`X/fr`）—— 同一地点的其它语言版本
  ③ 非正传前缀 / 其它作品（Baoh / TSKR / Rohan au Louvre / Deadman's Questions /
     Cool Shock B.T. / Eccentrics / Kishibe Rohan Meets Gucci …）
  ④ **无 `mangadebut`** —— 没有正传漫画出场证据（实测 29 个，也都没有动画出场）

== ★★ 解析里踩的三个坑（每一个都会产出"看着合理但是错的"数据）★★
【1】**用非贪婪正则截模板 → 在嵌套模板处提前截断**
     `{{Location Infobox …}}` 里常嵌套 `{{Tabber}}`，而它的结束 `}}` 会独占一行
     → `{{Location Infobox(.*?)` + 换行 + `}}` 在那里就停了。
     实测后果：`Devil's Palm` 被截成只剩 `title`/`image` 两个字段，
     我据此判定"它的 Infobox 不完整、字段在正文里" —— **前提本身就是错的**，
     而 `place` / `mangadebut` / `animedebut` 明明就在 Infobox 里（L12-14）。
     → 截模板必须**数括号**（见 `infobox()`）。
     ★ 一次错误的前提会连带一整串错误结论，这是最难自查的一类。
【2】**只取 wiki 链接 → 丢掉裸文本与模板里的值**
     `{{W|United States}}` 不是链接、`Japan` 是裸文本 →
     Angelo Rock 的层级少一级、Devil's Palm 只剩 Morioh。
【3】**`<br />` 是并列的第二处归属，不是同一路径的分隔**
     Devil's Palm 的 Infobox 同时写了美国（SBR 主线）与杜王町（JJL 提及）。
     混成一条链会得到 `United States → Morioh → Japan` 这种**不存在的路径**。
     ★ 我一度把 [2] 的结果解读成"层级被错取成杜王町" —— 其实源站**确实标了两处**，
       错的只是我的解析与解读。

== ★ 已知局限（写在这里，别在界面上假装没有）==
  `mangadebut` 只记**首次**出场，所以「跨部地点」只能算在最早期数上：
    Naples / Rome / Venice 首次出现在**第 2 部**（Chapter 89 等），
    于是它们不计入第 5 部 —— 第 5 部因此只有 7 个。
  这不是抓取错误，是**源站单值字段的固有局限**，界面上要说明。

用法：
    python dataset/sources/jojowiki/build_locations.py
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC = Path(__file__).resolve().parent / "articles.json"
OUT = ROOT / "dataset" / "processed" / "locations.json"

# 无前缀 `Chapter N` 的编号区间 → 部（jojowiki 编号连续贯穿第 1–6 部）
BOUNDS = [(1, 44, 1), (45, 116, 2), (117, 265, 3),
          (266, 439, 4), (440, 594, 5), (595, 752, 6)]
# 带前缀的部号映射（第 6 部两种写法都存在，实测 SO 前缀有 8 个）
PREFIX_PART = {"SO": 6, "SBR": 7, "JJL": 8, "TJL": 9}
# 命名空间（非内容页）
NS = ("JoJo Wiki", "Template", "Category", "User", "File", "Help", "Module")
# 语言子页后缀
LANG_SUFFIX = re.compile(r"/(fr|zh|es|de|it|ja|pt|ru|pl|ko)$", re.I)


def infobox(wt: str) -> str:
    """截出 `{{Location Infobox …}}` 的范围 —— **用括号配对，不用正则**。

    ★★ 为什么不能用 `\\{\\{Location Infobox(.*?)\\n\\}\\}` ★★
      Infobox 里常嵌套 `{{Tabber}}` / `{{W|}}`，而嵌套模板的结束 `}}`
      **可能独占一行** —— 非贪婪正则会在那里**提前截断**。
      实测后果：`Devil's Palm` 的 Infobox 被截成只剩 `title`/`image` 两个字段，
      于是我以为"它的 Infobox 不完整、字段在正文里"，并据此写了错误的注释；
      而实际上是 **`place` / `mangadebut` / `animedebut` 都在 Infobox 里**（L12-14）。
      → 一次错误的前提，会连带一串错误的结论。**截模板必须数括号。**
    """
    key = "{{Location Infobox"
    i = wt.find(key)
    if i < 0:
        return ""
    depth, j = 0, i
    while j < len(wt):
        if wt.startswith("{{", j):
            depth += 1
            j += 2
            continue
        if wt.startswith("}}", j):
            depth -= 1
            j += 2
            if depth == 0:
                return wt[i + len(key):j - 2]
            continue
        j += 1
    return wt[i + len(key):]          # 未闭合时返回剩余全部（宁可多，不可少）


_TPL_DROP = re.compile(r"\{\{\s*(Flag|ImageSize|S|Ref)\s*\|[^}]*\}\}", re.I)


def clean_text(s: str | None) -> str | None:
    """拆掉 wiki 模板，留下可读文本。

    实测遇到的三种：
      `{{Ruby|食屍鬼街|オウガーストリート}}` → `食屍鬼街`（★ 日文汉字名就在这里）
      `{{W|Landform|landform}}` / `{{W|Rookery (slum)}}` → `Landform` / `Rookery (slum)`
      `{{S|…}}` / `{{Flag|Japan}}` / `{{ImageSize}}` → 丢弃
    """
    if not s:
        return None
    # ★ `<ref>` 清理放在**这里**（最底层），这样所有走 clean_text 的字段都受益 ——
    #   实测只加在 place_paths 里时，`type` 字段仍带着
    #   `Ski resort<ref name="JJL65P4647>, p.46-47</ref>` 漏到数据里。
    s = re.sub(r"<ref[^>]*>.*?</ref>", "", s, flags=re.I | re.S)
    s = re.sub(r"<ref[^>]*/?>", "", s, flags=re.I)
    s = re.sub(r"<br\s*/?>", " ", s, flags=re.I)      # 残余 br → 空格
    s = re.sub(r"\{\{\s*Ruby\s*\|([^|}]+)\|[^}]*\}\}", r"\1", s, flags=re.I)
    s = re.sub(r"\{\{\s*W\s*\|([^|}]+)(?:\|[^}]*)?\}\}", r"\1", s, flags=re.I)
    s = _TPL_DROP.sub("", s)
    s = re.sub(r"\{\{[^}]*\}\}", "", s)                     # 其余未知模板
    s = re.sub(r"\[\[([^\]|]+)\|([^\]]*)\]\]", r"\2", s)     # [[A|B]] → B
    s = re.sub(r"\[\[([^\]]+)\]\]", r"\1", s)                # [[A]] → A
    s = re.sub(r"'''?|''?", "", s)                           # 粗体/斜体
    return s.strip() or None


def field(ib: str, key: str) -> str | None:
    """取 Infobox 内的单个字段值（在**已截范围**的文本上搜）。

    ★★ 空白类必须是 [^\\S\\n]（**不含换行**），不能用 \\s ★★
      实测踩坑（真 bug，25 个地点的 `animedebut` 全被污染成另一字段）：
        `|animedebut = `   ← 空值，行尾就是换行
        `|colors     = JoJolion`
      而 `\\s*` 会**吃掉换行**，于是 `(.+)` 把**下一行整行**当成了这个字段的值
      → 页面上出现「动画：]colors = JoJolion」这种东西。
      ★ 它是"看着有值、其实是脏的"那一类：不报错，只是安静地把 A 字段
        写进了 B 字段（同 §5 的四个坑同一个家族）。
    """
    m = re.search(r"^\|[^\S\n]*%s[^\S\n]*=[^\S\n]*(.+)$"
                  % re.escape(key), ib, re.M)
    return m.group(1).strip() if m else None


def first_link(text: str) -> str:
    """`[[A|B]]` / `[[A]]` → A；无链接则原样返回。"""
    m = re.search(r"\[\[([^\]|]+)", text or "")
    return (m.group(1) if m else (text or "")).strip()


def links(text: str) -> list[str]:
    """取出所有 wiki 链接的**目标名**（用于 residents 这类多值字段）。"""
    return [x.strip() for x in
            re.findall(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]", text or "")]


def _chain_of(seg_group: str) -> list[str]:
    """把一段「逗号分隔的归属」解析成层级链。"""
    out: list[str] = []
    for seg in seg_group.split(","):
        seg = seg.strip()
        if not seg:
            continue
        m = re.match(r"\[\[([^\]|]+)(?:\|([^\]]*))?\]\]", seg)
        out.append((m.group(2) or m.group(1)).strip() if m
                   else (clean_text(seg) or seg))
    return [x for x in out if x]


def split_multi(s: str | None) -> list[str]:
    """把 `<br />` 分隔的字段拆成列表。

    ★ 实测 37/113 个地点有这种情况：`Cairo` 的 mangadebut 是
      `Chapter 117 <br />Chapter 122`（多次出场）、`Colosseum` 的 type 是
      `Amphitheatre<br />Exhibit`（两个类型）。
      源站用 `<br />` 表示**多个值**，不是"一个值里带换行" ——
      原样入库会让界面显示成一行带标签的乱码。
    """
    if not s:
        return []
    return [x for x in (clean_text(p)
                        for p in re.split(r"<br\s*/?>", s, flags=re.I)) if x]


def place_paths(place_raw: str | None) -> list[list[str]]:
    """地点归属 → **多条**层级路径。

    `[[Jozenji]], [[Morioh]], Japan {{Flag|Japan}}`
        → [['Jozenji', 'Morioh', 'Japan']]
    `{{W|United States}} {{Flag|USA}}<br />[[Morioh (JoJolion)|Morioh]] {{Flag|Japan}}`
        → [['United States'], ['Morioh', 'Japan']]

    ★★ 两个真坑（我先后踩了）★★
    【1】只取 wiki 链接 → 丢掉**裸文本与模板里的值**：
        `{{W|United States}}` 不是链接，`Japan` 也是裸文本 →
        Angelo Rock 的路径少一级、Devil's Palm 只剩 Morioh。
    【2】`<br />` 是**并列的第二处归属**，不是同一路径的分隔：
        Devil's Palm 的 Infobox 同时写了美国（SBR 主线）与杜王町（JJL 提及），
        混成一条链会得到 `United States → Morioh → Japan` 这种**不存在的路径**。
        ★ 我一度把结果解读成"层级被错取"，其实是源站**确实标了两处** ——
          错误结论来自我没先看清字段原文。
    """
    if not place_raw:
        return []
    # ★ 模板处理顺序：先清引用/转换（Ruby / W 里有**真值**）→ 再删 Flag → 最后删其余
    #   实测踩坑：直接把所有 `{{…}}` 删掉，`{{W|United States}}` 就没了
    #   → Devil's Palm 少了一条"美国"路径，只剩 Morioh。
    #   ★ `<ref>` 必须在**分段之前**清掉：`Ocean Liner to America` 的原文是
    #     `Atlantic Ocean<ref name="42p2">, p.2</ref>` —— **逗号在 ref 内部**，
    #     先按逗号切就会切出一个 `Atlantic Ocean<ref name="42p2">` 这种半截值。
    s = re.sub(r"<ref[^>]*>.*?</ref>", "", place_raw, flags=re.I | re.S)
    s = re.sub(r"<ref[^>]*/?>", "", s, flags=re.I)
    s = re.sub(r"\{\{\s*Ruby\s*\|([^|}]+)\|[^}]*\}\}", r"\1", s, flags=re.I)
    s = re.sub(r"\{\{\s*W\s*\|([^|}]+)(?:\|[^}]*)?\}\}", r"\1", s, flags=re.I)
    s = re.sub(r"\{\{\s*Flag\s*\|[^}]*\}\}", "", s, flags=re.I)
    s = re.sub(r"\{\{[^}]*\}\}", "", s)
    out = []
    for g in re.split(r"<br\s*/?>", s, flags=re.I):
        chain = _chain_of(g)
        if chain:
            out.append(chain)
    return out


def place_chain(place_raw: str | None) -> list[str]:
    """第一条层级路径（给界面做单行展示）。完整的多路径见 `place_paths`。"""
    paths = place_paths(place_raw)
    return paths[0] if paths else []


def part_of(mangadebut: str | None) -> tuple[int | None, str]:
    """`mangadebut` → (部号, 判据)。部号为 None 表示非正传或判不出。"""
    if not mangadebut:
        return None, "无 mangadebut"
    txt = first_link(mangadebut)
    # ① 带前缀：`SBR Chapter 12`
    pm = re.match(r"^([A-Za-z'’ .]+?)\s+Chapter\s+(\d+)", txt)
    if pm:
        pre = pm.group(1).strip()
        if pre in PREFIX_PART:
            return PREFIX_PART[pre], f"{pre} Chapter {pm.group(2)}"
        return None, f"非正传前缀 {pre}"
    # ② 无前缀：`Chapter 272`（★ 必须先处理这条，占一半以上）
    pm2 = re.match(r"^Chapter\s+(\d+)", txt)
    if pm2:
        num = int(pm2.group(1))
        for lo, hi, pt in BOUNDS:
            if lo <= num <= hi:
                return pt, f"Chapter {num}"
        return None, f"Chapter {num} 超出第 1–6 部区间"
    return None, f"其它作品: {txt[:40]}"


def main() -> int:
    if not SRC.exists():
        print(f"✗ 缺少 {SRC}（先跑 fetch_articles.py）")
        return 1
    sys.path.insert(0, str(ROOT / "database"))
    from load_db import _loc_id                       # noqa: E402

    data = json.loads(SRC.read_text(encoding="utf-8"))
    locs = [p for p in data.get("pages", []) if p.get("kind") == "location"]
    print(f"读到地点页 {len(locs)} 个")

    entries, stats = [], {"kept": 0, "ns": 0, "lang": 0, "other": 0, "no_md": 0}
    for p in locs:
        title, wt = p["title"], p["wikitext"]
        ib = infobox(wt)                       # ★ 先截范围，再取字段
        # ★ 多值字段拆成列表（源站用 `<br />` 表示"多次/多个"）
        debuts = split_multi(field(ib, "mangadebut"))
        animes = split_multi(field(ib, "animedebut"))
        types = split_multi(field(ib, "type"))
        e = {
            "location_id": _loc_id(title),
            "title": title,
            "name_ja": clean_text(field(ib, "ja_kanji")),
            "name_romaji": clean_text(field(ib, "ja_romaji")),
            "type": types[0] if types else None,          # 主类型（界面单行展示）
            "types": types,                                # 全部类型
            "place_raw": field(ib, "place"),               # 原始值，保留以便追溯
            "place_paths": place_paths(field(ib, "place")),
            "place_chain": place_chain(field(ib, "place")),
            "residents": links(field(ib, "residents") or ""),
            "mangadebut": debuts[0] if debuts else None,   # 最早一次（判部依据）
            "mangadebuts": debuts,
            "animedebut": animes[0] if animes else None,
            "animedebuts": animes,
            "n_infobox_fields": len(re.findall(r"^\|\s*\w+\s*=", ib, re.M)),
            "part": None, "part_basis": "",
            "included": False, "exclude_reason": None,
        }
        # ---- 排除规则（顺序即优先级）----
        if any(title.startswith(x + ":") for x in NS):
            e["exclude_reason"] = "命名空间页"
            stats["ns"] += 1
        elif LANG_SUFFIX.search(title):
            e["exclude_reason"] = "语言子页"
            stats["lang"] += 1
        else:
            pt, why = part_of(e["mangadebut"])
            e["part"], e["part_basis"] = pt, why
            if pt is None:
                e["exclude_reason"] = ("无正传漫画出场" if why == "无 mangadebut"
                                       else why)
                stats["no_md" if why == "无 mangadebut" else "other"] += 1
            else:
                e["included"] = True
                stats["kept"] += 1
        entries.append(e)

    out = {
        "n_source_pages": len(locs),
        "n_included": stats["kept"],
        "n_excluded": len(locs) - stats["kept"],
        "exclude_stats": stats,
        "_note": ("用户口径：只保留 9 部正传漫画。"
                  "`part_basis` 记录判据、`exclude_reason` 记录排除原因，均可复核。"
                  "★ mangadebut 只记首次出场 → 跨部地点只算最早部"
                  "（Naples/Rome/Venice 记在第 2 部，故第 5 部偏少）。"),
        "locations": entries,
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"\n{'='*62}")
    print(f"保留 {stats['kept']} / 排除 {len(locs)-stats['kept']}"
          f"（命名空间 {stats['ns']} · 语言子页 {stats['lang']} · "
          f"其它作品 {stats['other']} · 无漫画出场 {stats['no_md']}）")
    dist = Counter(e["part"] for e in entries if e["included"])
    print("按部：" + " ".join(f"第{k}部 {dist[k]}" for k in sorted(dist)))
    have_ja = sum(1 for e in entries if e["included"] and e["name_ja"])
    have_ch = sum(1 for e in entries if e["included"] and e["place_chain"])
    print(f"字段覆盖（保留的 {stats['kept']} 个）："
          f"日文名 {have_ja} · 有层级 {have_ch}")
    print(f"\n→ {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

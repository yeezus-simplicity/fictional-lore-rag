"""
补全替身中文名（M16）—— 从中文维基百科「替身 (JoJo的奇妙冒险)」总表抓取。

==★ 为什么改用中文维基 ==

M15 用jojogh.jojo6.com 作为C 级来源，补到 130/154。剩下 24 条里：
  - 4 条是形态派生（Echoes ACT1/2/3、Star Platinum: The World）
  - 20 条是 SBR/JoJolion 冷门替身

实测 jojowiki 单页里 `Chinese` / `中文` 命中 **0 次** —— 那些页面根本没中文译名，
所以「继续从 jojowiki 抓」这条路是走不通的，不是没抓够。

而中文维基百科有一张**结构化的英中对照总表**（28 张表，含英文名/中文名/本体/能力），
实测覆盖：Paisley Park、Nut King Call、Echoes、Manic Depression、Dolly Dagger、
Voodoo Child、Sky High、Achtung Baby、Nightbird Flying 均有中文名。

★ 关键区别：维基是**人工编纂的对照表**，不是机器翻译 ——
  「纳京高」这种译名是编者查证后写上去的约定俗成叫法，
  正是我们要的（用户就是这么输入的）。

==★ 三条硬约束 ==

1. **只从表格单元格取值，不做翻译、不做音译、不做规则拼接**
   抓到空就是空 —— 没抓到就是中文圈确实没有通行译名，如实留缺口。
   （M15 已实测：猜译名会造出用户不会输入的词，反而拉低匹配质量）

2. **同一个英文名抓到多个中文名 → 全部作为异译别名保留**
   实测「Nut King Call」维基写「纳京高」、搜狗百科写「螺丝之王」；
   用户两种都会输入 → 并列收录，由build_zh_map 合成name_zh_variants。

3. **形态派生单独处理，不走抓取**
   Echoes ACT1/2/3 的中文名是「回声ACT1」这种**拼接**结果，
   维基总表里没有独立行 → 由 build_zh_map 按母体名+形态后缀生成，
   并标level=D（派生，非来源直取）以便审阅。

用法：python dataset/sources/zh_names/fetch_zh_wiki.py
输出：dataset/sources/zh_names/zh_wiki_pairs.json
"""
from __future__ import annotations

import json
import re
import sys
import time
import unicodedata
import urllib.request
from html import unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent / "zh_wiki_pairs.json"

WIKI_URL = ("https://zh.wikipedia.org/wiki/"
            "%E6%9B%BF%E8%BA%AB_(JoJo%E7%9A%84%E5%A5%87%E5%A6%99%E5%86%92%E9%9A%AA)")

# ★ jojowiki / 萌娘百科直连返回 403（无 UA 或 UA 被拒）→ 必须带浏览器 UA
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                   " (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Accept-Language": "zh-CN,zh;q=0.9",
}

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def norm_en(s: str) -> str:
    """英文名规范化：只保留小写字母与数字（与 build_zh_map 保持一致）。"""
    s = unicodedata.normalize("NFKC", s).lower()
    return re.sub(r"[^a-z0-9]", "", s)


def clean_cell(html: str) -> str:
    """把一个<td> 单元格变成纯文本。

    ★ 必须先 unescape 再去标签：表格里中文名常写成
      `纳京高` 或带 <a> 链接 / <br> 换行 / 全角空格，
      直接去标签会留下 `纳 京高` 这类断裂文本 → 匹配不上。
    """
    t = html
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", t, flags=re.S | re.I)
    t = re.sub(r"<br\s*/?>", " ", t, flags=re.I)
    t = re.sub(r"<sup[^>]*>.*?</sup>", "", t, flags=re.S | re.I)
    t = _TAG_RE.sub(" ", t)
    t = unescape(t)
    t = t.replace(" ", " ").replace("　", " ")
    t = _WS_RE.sub(" ", t).strip()
    return t.strip(" ·—、,，/|")


def parse_tables(html: str) -> list[list[list[str]]]:
    """把所有 <table> 拆成 [[cell,...], ...] 的二维表。"""
    tables: list[list[list[str]]] = []
    for tm in re.finditer(r"<table\b.*?</table>", html, flags=re.S | re.I):
        rows: list[list[str]] = []
        for rm in re.finditer(r"<tr\b.*?</tr>", tm.group(0), flags=re.S | re.I):
            cells = [clean_cell(c.group(0))
                     for c in re.finditer(r"<t[hd]\b.*?</t[hd]>",
                                          rm.group(0), flags=re.S | re.I)]
            if cells:
                rows.append(cells)
        if rows:
            tables.append(rows)
    return tables


#★ 中文名必须以中文为主字符，且不能是纯符号
_HAS_HAN = re.compile(r"[一-鿿]")

# ★ 明显不是替身名的噪声词（表头、能力描述、部位名）
_NOISE = {
    "替身名", "中文名称", "英文名称", "本体", "能力", "破坏力", "速度",
    "射程距离", "持续力", "精密性", "成长性", "替身", "无", "－", "-",
    "名字", "名称", "日文", "英文", "中文", "简介", "备注", "OVA",
    "替身六维", "六维数据", "图示", "暂无六维", "类型", "分类",
}

#★ 六维属性表被误抓：英文列是 `A`/`B`/`C`/`D`/`E`（破坏力~成长性等级），
#   中文列是 `A(射程数米)` 这种等级+备注 → 整对都是噪声。
#   实测误抓出 a/b/c/d/e 五个 key共 16 条，必须挡掉。
_MIN_EN_LEN = 2
_RANK_CELL = re.compile(r"^[A-Ea-e](\s*[（(].*)?$")

# ★ 描述性长句（能力简介）不可能是名字，用来挡住误抓
_MAX_ZH_LEN = 24


def split_zh_cell(zh: str) -> list[str]:
    """把 `软又湿（柔软且湿润）` 拆成 ['软又湿', '柔软且湿润']。

    ★ 括号在维基里不是噪声，而是**异译别名**：
        Soft & Wet  → 软又湿（柔软且湿润）
        Red Hot Chili Pepper → 呛辣红椒（辛红辣椒）
      括号内外都是社区流通的叫法，用户两种都会输入 → 必须都收录。
      另有一种形态`极易实施的肮脏行径（D4C）•爱之列车`，用 • 分隔，也一并拆。

    ★ 反例（D4C 那条）说明不能无脑拆：
      括号里可能只有缩写 `D4C`，拆出来是纯拉丁字母 → 由调用方过滤。
    """
    parts: list[str] = []
    # 按括号拆：头部 + （...）里的别名；先把 • 当分隔符换成逗号
    for m in re.finditer(r"([^（()]+)（([^（()]*)）|\(?([^(（()]+)\)?",
                         zh.replace("•", "，")):
        head, inner, plain = m.group(1), m.group(2), m.group(3)
        for cand in (head, inner, plain):
            if not cand:
                continue
            c = cand.strip(" ·—、,，/|")
            if c:
                parts.append(c)
    return parts


def _valid_zh_name(zh: str) -> bool:
    """一个单元格值是否可作为中文名。"""
    if not zh or zh in _NOISE or len(zh) > _MAX_ZH_LEN:
        return False
    if not _HAS_HAN.search(zh):
        return False
    # ★ 必须是「以汉字为主」：允许夹一两个分隔点（纳·京·高），
    #   但不能是「D4C」这种纯拉丁缩写，也不是「A(射程)」等级格。
    han = len(_HAS_HAN.findall(zh))
    if han < 2:
        return False
    if _RANK_CELL.match(zh):
        return False
    return True


def fetch(url: str = WIKI_URL, retries: int = 3) -> str:
    last: Exception | None = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", "ignore")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"抓取失败: {last}")


def extract_pairs(html: str) -> tuple[dict[str, list[str]], list[str]]:
    """从所有表格里抽 英名→[中文名...]。

    返回 (pairs, rejects)
      pairs   : 规范化英文名 -> 去重后的中文译名列表
      rejects : 被判定为噪声而丢弃的样本（用于人工复查）
    """
    pairs: dict[str, list[str]] = {}
    rejects: list[str] = []

    for rows in parse_tables(html):
        for cells in rows:
            # ★ 英中相邻是维基这类表最常见的排法：英文名 | 中文名 | 本体 | 能力
            for a, b in zip(cells, cells[1:]):
                en, zh_raw = a.strip(), b.strip()
                if not en or not zh_raw:
                    continue
                # 英文侧必须像英文名（允许 & . : ' 空格 数字）
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 &\-'\.:,\.!/]*", en):
                    continue
                # ★ 六维属性表：英文列只有 A/B/C/D/E 一字母 → 整行跳过
                if _RANK_CELL.match(en) or len(en) < _MIN_EN_LEN:
                    continue
                k = norm_en(en)
                if not k:
                    continue
                # 括号里是异译别名，拆开后逐个校验
                cands = split_zh_cell(zh_raw)
                good = [c for c in cands if _valid_zh_name(c)]
                if not good:
                    rejects.append(f"{en} -> {zh_raw[:40]}")
                    continue
                for c in good:
                    pairs.setdefault(k, [])
                    if c not in pairs[k]:
                        pairs[k].append(c)
    return pairs, rejects


def main() -> int:
    print("=" * 66)
    print("从中文维基百科抓取替身英中对照（M16）")
    print("=" * 66)
    print(f"  来源 {WIKI_URL}")

    html = fetch()
    print(f"  页面 {len(html):,} 字节 / {html.count('<table')} 张表")

    pairs, rejects = extract_pairs(html)
    print(f"\n  抽出对照 {len(pairs)} 组")
    multi = {k: v for k, v in pairs.items() if len(v) > 1}
    print(f"  其中多译名 {len(multi)} 组（将并列保留为异译别名）")

    if rejects:
        print(f"  噪声丢弃 {len(rejects)} 条，样例：")
        for r in rejects[:5]:
            print(f"      {r}")

    OUT.write_text(json.dumps(pairs, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"\n  → {OUT.relative_to(ROOT)}")
    print("\n  ★ 只取维基表格里的现成译名，不翻译、不音译。")
    print("    抓不到的 =中文圈确实没有通行译法 → 如实留缺口。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
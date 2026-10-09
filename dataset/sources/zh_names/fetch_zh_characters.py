"""补全**角色（替身使者）中文名**（M21）—— 从中文维基替身表的本体列提取。

==★ 数据来源与可行性 ==

中文维基「替身 (JoJo的奇妙冒險)」有一张结构化总表，列序：
    英文名稱 || 中文名稱 || 本體（替身使者） || 能力 || 破坏力 … 成长性

★ 本体列就是**替身使者的中文名**，且它与「替身英文名」同行 ——
  于是可以用替身英文名把维基行**对回 DB 的 stand → owner**，
  从而拿到 character_id 的中文名。这是本项目最稳的角色名来源。

★ 为什么不从 jojowiki 抓角色中文名：
  M16 已实测 jojowiki 页面里 `Chinese`/`中文` 命中 0 次（页面没有中文译名）。
★ 为什么不从灰机 wiki 抓：
  M16 实测连续请求 api.php 会被限流并**升级成硬封禁**（api.php + /wiki/
  全 403，萌娘百科被 Cloudflare 封），不可作主力来源。

==★ 三条硬约束（继承 M16 的口径）==

1. **只从表格单元格取值** —— 不做翻译、不做音译、不凭记忆填。
   抓不到就是中文圈没有通行译名，如实留缺口。
2. **繁简转换用 opencc**，不自己造表。
   ★ 实测踩坑：项目自带的 `simplify_zh()` 只有 **16 个字符**的映射
   （M16 补替身名时恰好没撞上需要转换的字，所以没暴露），
   拿它转角色名会漏一大堆（「阿布德爾」→ 不动）。
3. **一个人的多个写法都保留**（如「DIO/迪奧·布蘭度」→ 两个别名）。

用法：
    python dataset/sources/zh_names/fetch_zh_characters.py
输出：
    dataset/sources/zh_names/zh_char_pairs.json
    形如 {"character_id": ["中文名", "异译", ...], ...}
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent / "zh_char_pairs.json"
CACHE = Path(__file__).resolve().parent / "_cache_wiki_stands.wikitext"

WIKI_PAGE = "替身 (JoJo的奇妙冒險)"
WIKI_API = ("https://zh.wikipedia.org/w/api.php?action=parse&page="
            + urllib.parse.quote(WIKI_PAGE)
            + "&prop=wikitext&format=json&formatversion=2")
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"),
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# 数字 → 英文词（替身名归一化用；"Death 13" vs "Death Thirteen"）
_DIGIT = {"0": "Zero", "1": "One", "2": "Two", "3": "Three", "4": "Four",
          "5": "Five", "6": "Six", "7": "Seven", "8": "Eight", "9": "Nine"}


def norm(x: str) -> str:
    """替身名归一化，用于跨源匹配。

    ★ 实测两侧写法有差异：DB 'Death Thirteen' ↔ 维基 'Death 13'；
      DB 的 stand 名可能带 ':' '·' 等符号。
      → 数字统一成英文词 + 只保留字母。
    """
    s = x.lower()
    s = re.sub(r"(\d)", lambda m: _DIGIT[m.group(1)], s)
    return re.sub(r"[^a-z]", "", s)


def to_simplified(text: str) -> str:
    """繁体 → 简体。

    ★ 优先 opencc；没装则退回项目自带的 simplify_zh（覆盖很少，会警告）。
      （simplify_zh 只有 16 个字符映射，实测转不动「阿布德爾」这类。）
    """
    try:
        import opencc
        return _OPENCC.convert(text)
    except ImportError:
        sys.path.insert(0, str(ROOT / "services"))
        from aliases import simplify_zh
        print("  ! opencc 未安装，退回 simplify_zh（覆盖极少，结果可能仍为繁体）")
        return simplify_zh(text)


try:
    import opencc
    _OPENCC = opencc.OpenCC("t2s")
except ImportError:  # pragma: no cover
    _OPENCC = None


def fetch_wikitext(use_cache: bool = True) -> str:
    """抓维基 wikitext，带本地缓存。"""
    if use_cache and CACHE.exists():
        txt = CACHE.read_text(encoding="utf-8")
        if len(txt) > 5000:
            print(f"  用缓存 {CACHE.name}（{len(txt)} 字符）")
            return txt
    px = os.environ.get("https_proxy") or os.environ.get("http_proxy")
    op = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": px, "https": px}))
    print("  从维基抓取…")
    txt = json.loads(op.open(urllib.request.Request(WIKI_API, headers=HEADERS),
                             timeout=90).read())["parse"]["wikitext"]
    CACHE.write_text(txt, encoding="utf-8")
    return txt


def _fullwidth_to_half(s: str) -> str:
    """全角标点 → 半角。

    ★ 实测踩坑：维基里写着「The World－Over Heaven」（**全角减号** U+FF0D），
      而英文名正则只允许半角 `-` → 整行被判为"不像英文名"而丢弃，
      连带把该行的中文名（Dio/迪奧·布蘭度）也丢了 → DIO 补不到中文名。
    """
    out = []
    for ch in s:
        o = ord(ch)
        if o == 0x3000:                      # 全角空格
            out.append(" ")
        elif 0xFF01 <= o <= 0xFF5E:          # 全角 ASCII 区
            out.append(chr(o - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out)


def clean_cell(s: str) -> str:
    """清理表格单元格：去 ref / 模板 / HTML / 链接标记 / 繁简标签。"""
    s = re.sub(r"<ref[^>]*>.*?</ref>", "", s, flags=re.S)
    s = re.sub(r"<ref[^>]*/>", "", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\{\{[^}]*\}\}", "", s)
    # ★ 维基繁简标签：-{zh-hans:简;zh-hant:繁}- → 取简体面
    s = re.sub(r"-\{[^{}]*?zh-hans:([^;{}]*)[;{}][^{}]*?-\}", r"\1", s)
    s = re.sub(r"-\{([^{}]*)\}-", r"\1", s)
    s = re.sub(r"\[\[[^\]|]*\|([^\]]*)\]\]", r"\1", s)
    s = re.sub(r"\[\[([^\]]*)\]\]", r"\1", s)
    s = re.sub(r"''+", "", s)
    s = _fullwidth_to_half(s)
    return s.strip(" \t\n|·＊*")


def split_tables(wt: str) -> list[str]:
    """按 `{|` … `|}` 配对切出表格。

    ★★ 为什么不能用 `re.findall(r"\\{\\|.*?\\n\\|\\}", wt, re.S)`（实测踩坑）★
      非贪婪 `.*?` 遇到**第一个**换行+`|}` 就停。维基表格里存在嵌套结构
      （单元格内嵌模板/表格），于是大表被切成碎片 ——
      实测切出 24 个表、大多数只有 1-4 行，
      连带把「Sticky Fingers → 布羅諾·布加拉提」这类行整段丢掉，
      导致这些角色补不到中文名。
    → 改成按行扫描 + 深度计数配对。
    """
    tables: list[str] = []
    depth = 0
    buf: list[str] = []
    for line in wt.split("\n"):
        stripped = line.strip()
        if stripped.startswith("{|"):
            if depth == 0:
                buf = []
            depth += 1
        if depth > 0:
            buf.append(line)
        if stripped.startswith("|}") or stripped == "|}":
            depth -= 1
            if depth == 0 and buf:
                tables.append("\n".join(buf))
                buf = []
    return tables


def parse_rows(wt: str) -> list[list[str]]:
    """把 wikitable 解析成行（含 rowspan 继承）。

    ★★ rowspan 必须用**列指针**处理（实测踩坑，连错两版）★★

      第一版：遇到 `rowspan=2 | X` 就把 X 追加到 cells 末尾，等下一行时
        再把 pending 的值 append 到新 cells 前面。后果：
        下一行已有 3 个 placeholder，新行的第一个单元格被追加到
        cells[3] 而不是 cells[0] → **整表列错位**，
        实测「Sticky Fingers → 布羅諾·布加拉提」这类行全部解析不出来。

      第二版（本版）：维护 col 指针。每放一个单元格就 col+1；
        遇到被上方 rowspan 占用的列就跳过（直接用上方的值）。
        这才是 wikitable rowspan 的正确语义。

      维基表里 rowspan 用于「一个使用者对应多个替身形态」，很常见，
      所以这个 bug 会大面积丢行（不是个别）。
    """
    out: list[list[str]] = []
    for tbl in split_tables(wt):
        pending: dict[int, tuple[int, str]] = {}   # 列号 → (剩余行, 值)
        cur: list[str] = []
        for line in tbl.split("\n"):
            if line.startswith("|-"):
                if cur:
                    out.append(cur)
                cur = []
                # 新行开始：先把被 rowspan 占用的列填上
                col = 0
                while col in pending:
                    left, val = pending[col]
                    cur.append(val)
                    pending[col] = (left - 1, val)
                    if left - 1 <= 0:
                        del pending[col]
                    col += 1
                continue
            if not line.startswith("|") or line.startswith("|}"):
                continue
            for part in re.split(r"\|\||!!", line[1:]):
                # 跳过被 rowspan 占用的列
                col = len(cur)
                while col in pending:
                    left, val = pending[col]
                    cur.append(val)
                    pending[col] = (left - 1, val)
                    if left - 1 <= 0:
                        del pending[col]
                    col += 1
                m = re.match(r'^\s*rowspan\s*=\s*"?(\d+)"?\s*\|(.*)$',
                             part, re.I)
                if m and int(m.group(1)) > 1:
                    n, val = int(m.group(1)), m.group(2)
                    cur.append(val)
                    pending[len(cur) - 1] = (n - 1, val)
                else:
                    cur.append(m.group(2) if m else part)
        if cur:
            out.append(cur)
    return out


def split_owners(raw: str) -> list[str]:
    """本体列可能含多个人 / 中英并列，拆成候选中文名列表。

    实测形态：
        'DIO'                                 → []（无中文，丢弃）
        '喬魯諾·喬巴拿（本名“汐华初流乃”)'          → ['乔鲁诺·乔巴拿', '汐华初流乃']
        'Dio/迪奧·布蘭度(遊戲:天國之眼最終形態)'      → ['迪奥·布兰度']
        'Gray Fly/格雷普萊'                     → ['格雷普莱']
        '瓦尼拉·艾斯'                            → ['瓦尼拉·艾斯']
    """
    raw = to_simplified(raw)
    # 括号里的内容（含中英括号）→ 作为附加别名
    extra = re.findall(r"[（(]([^（()）]*)[）)]", raw)
    main = re.sub(r"[（(][^（()）]*[）)]", "", raw)
    cands: list[str] = []
    for chunk in [main] + extra:
        for seg in re.split(r"[/／、,，;；\n]", chunk):
            seg = seg.strip(" ·-·・\"'“”「」")
            if not seg:
                continue
            # ★ M21：过滤**说明性片段**，它们不是人名
            #   实测噪声：
            #     '游戏:天国之眼最终形态'（括号里的出处说明）
            #     '本名"汐华初流乃"'（说明性）
            #   → 判据：含冒号 / 说明性关键词 / 过长。
            if re.search(r"[:：]", seg):
                continue
            if re.search(r"(本名|遊戲|游戏|形態|形态|最終|最终|登場|登场|"
                         r"自稱|自称|別名|别名|僅|仅|註|注)", seg):
                continue
            # 中文人名一般 2-10 字（过长的多半是描述句）
            if not re.search(r"[\u4e00-\u9fff]", seg) or len(seg) > 10:
                continue
            cands.append(seg)
    # 去重保序
    seen: set[str] = set()
    out: list[str] = []
    for x in cands:
        # ★ 二次清理：去掉残留的括号片段与斜杠片段
        #   实测残留：'乌龟(Coco Jumbo)'、'静·乔斯达/透明宝宝'
        #   （原始单元格格式不规则，一次拆分拆不干净）
        x = re.sub(r"[（(][^（()）]*[）)]", "", x)
        for piece in re.split(r"[/／]", x):
            piece = piece.strip(" ·-·・\"'“”「」")
            if not piece or piece in seen:
                continue
            if not re.search(r"[\u4e00-\u9fff]", piece) or len(piece) > 10:
                continue
            seen.add(piece)
            out.append(piece)
    return out


def split_owner_en(raw: str) -> list[str]:
    """从本体列里抽出**英文名**候选（供第二条匹配路径用）。

    ★ 为什么要这条路径（实测）：
      有些角色的**主替身行**本体列只写了英文（如 The World → "DIO"），
      但**派生行**给了中文（The World－Over Heaven → "Dio/迪奧·布蘭度"）。
      只看主替身会漏掉这些角色 → 需要拿英文名去 characters.name_en 直接对。
    """
    raw = re.sub(r"<ref[^>]*>.*?</ref>", "", raw, flags=re.S)
    raw = re.sub(r"[（(][^（()）]*[）)]", "", raw)
    out: list[str] = []
    for seg in re.split(r"[/／、,，;；\n]", raw):
        # ★ 去掉中文字符（含间隔号），留下英文部分
        #   注意是 \s* 不是 s*（手误会让正则变成「中文+s字符」）
        seg = re.sub(r"[\u4e00-\u9fff'’·・]\s*", " ", seg).strip(" .-")
        # 英文名：字母开头，允许点/空格/连字符（J. Geil / D an G）
        if re.match(r"^[A-Za-z][A-Za-z0-9. '\-]*$", seg) and 2 <= len(seg) <= 30:
            out.append(seg)
    return out


# ===============================================================
# 数据源 2：各「部」条目的 {{nihongo|中文|日文|英文}} 模板
# ===============================================================
# ★ 为什么需要第二个源（实测）：
#   替身表里有些角色的「本体」列**只写了英文**（Strength 行 owner 是
#   "Forever"、Death 13 行是 "Mannish Boy"），维基就是没给中文 →
#   只靠替身表最多 113/144。
#   而各部条目（星塵鬥士/不滅鑽石/黃金之風…）用 nihongo 模板列角色，
#   格式规范且带中文名。实测 8 部共 495 个模板，足够补缺口。
PARTS = ["幻影血脈", "戰鬥潮流", "星塵鬥士", "不滅鑽石",
         "黃金之風", "石之海", "飆馬野郎", "JoJolion"]
PARTS_CACHE = Path(__file__).resolve().parent / "_cache_wiki_parts.json"


def fetch_parts_wikitext() -> dict[str, str]:
    """抓各部条目的 wikitext（带缓存；SSL 偶发中断会重试）。"""
    if PARTS_CACHE.exists():
        try:
            d = json.loads(PARTS_CACHE.read_text(encoding="utf-8"))
            if d and all(len(v) > 1000 for v in d.values()):
                print(f"  用缓存 {PARTS_CACHE.name}（{len(d)} 部）")
                return d
        except (OSError, json.JSONDecodeError):
            pass
    px = os.environ.get("https_proxy") or os.environ.get("http_proxy")
    op = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": px, "https": px}))
    out: dict[str, str] = {}
    for pg in PARTS:
        url = ("https://zh.wikipedia.org/w/api.php?action=parse&page="
               + urllib.parse.quote(pg)
               + "&prop=wikitext&format=json&formatversion=2")
        for attempt in range(3):
            try:
                out[pg] = json.loads(op.open(
                    urllib.request.Request(url, headers=HEADERS),
                    timeout=60).read())["parse"]["wikitext"]
                print(f"    {pg} {len(out[pg])} 字符")
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 2:
                    print(f"    {pg} 失败：{str(e)[:40]}")
    if out:
        PARTS_CACHE.write_text(json.dumps(out, ensure_ascii=False),
                               encoding="utf-8")
    return out


def _balanced(wt: str, start: int) -> tuple[str, int]:
    """从 `{{` 之后开始，找到配对的 `}}`，返回 (内部内容, 结束下标)。"""
    depth = 1
    i = start
    while i < len(wt):
        if wt.startswith("{{", i):
            depth += 1
            i += 2
            continue
        if wt.startswith("}}", i):
            depth -= 1
            if depth == 0:
                return wt[start:i], i + 2
            i += 2
            continue
        i += 1
    return wt[start:], len(wt)


def split_template_args(body: str) -> list[str]:
    """按顶层 `|` 拆模板参数（忽略嵌套 {{}} / [[]] 内的竖线）。"""
    args: list[str] = []
    buf: list[str] = []
    depth = 0
    i = 0
    while i < len(body):
        if body.startswith("{{", i) or body.startswith("[[", i):
            depth += 1
            buf.append(body[i:i + 2])
            i += 2
            continue
        if body.startswith("}}", i) or body.startswith("]]", i):
            depth -= 1
            buf.append(body[i:i + 2])
            i += 2
            continue
        if body[i] == "|" and depth == 0:
            args.append("".join(buf))
            buf = []
        else:
            buf.append(body[i])
        i += 1
    args.append("".join(buf))
    return args


def extract_nihongo(wt: str) -> list[tuple[str, str]]:
    """抽 {{nihongo|中文|日文|英文}} → [(中文, 英文)]。

    实测形态：
        {{nihongo|支倉未起隆|支倉 未起隆（はぜくら みきたか）|Hazekura Mikitaka}}
        {{nihongo|空条承太郎|空条 承太郎（くうじょう…）|Jotaro Kujo|}}
        {{nihongo|迪奧·布蘭度|ディオ・ブランドー|Dio Brando}}
    → 参数 0=中文、2=英文（英文可能缺失）。
    """
    out: list[tuple[str, str]] = []
    for m in re.finditer(r"\{\{\s*nihongo\s*\|", wt, re.I):
        inner, _ = _balanced(wt, m.end())
        args = split_template_args(inner)
        if len(args) < 2:
            continue
        zh = clean_cell(args[0])
        en = clean_cell(args[2]) if len(args) >= 3 else ""
        if zh and re.search(r"[\u4e00-\u9fff]", zh):
            out.append((zh, en))
    return out


def name_key(en: str) -> str:
    """人名归一化键：拆词 → 去冠词 → 排序。

    ★ 为什么要做三步（每一步都是实测踩出来的）：
      1) **排序**：维基写 `Hazekura Mikitaka`（日文罗马字，姓在前），
         DB 是 `Mikitaka Hazekura`（名在前）→ 不排序永远不相等。
      2) **去冠词**：维基 nihongo 写 `The Eleven Men`，DB 是 `Eleven Men`
         → 不去 the 就漏。
      3) **去符号**：`L.A. Boomboom` / `D an G` / `J. Geil` 的点号空格要统一。
    """
    s = re.sub(r"[^A-Za-z\s.·・]", " ", en.lower())
    words = [w.strip(".").lower() for w in re.split(r"[\s.·・]+", s)]
    words = [w for w in words if w and w not in ("the", "a", "an")]
    return " ".join(sorted(words))


def loose_key(en: str) -> str:
    """更松的键：分词 → 去冠词 → 只留字母（**不排序**）。

    ★ 用途有两个，且必须**不排序**：
      1) 比较姓名序不同的写法时，用 name_key（排序）——
         但包含匹配（下面的兜底2）必须在**原始顺序**上做，
         否则 "AndreBoomboom" 无法成为 "AndreBoomboomLABoomboom" 的子串。
      2) 别用 `replace("the","")` 去冠词 —— 会误伤单词内部
         （"Catherine" → "Carine"）。必须**先分词**再逐词剔除。
    """
    s = re.sub(r"[^A-Za-z\s]", " ", en.lower())
    words = [w for w in s.split() if w and w not in ("the", "a", "an")]
    return "".join(words)


def clean_name(s: str) -> str:
    """候选人名统一收口清理（M21）。

    ★ 为什么要单独一个函数：三条路径（替身表本体列 / 英文名跨行 /
      各部 nihongo 模板）各自产出的字符串格式不一样，
      若只在某一条里清理，另外两条的脏值会漏进最终词典。
      实测漏进来的：'乌龟(Coco Jumbo)'、'静·乔斯达/透明宝宝'、
      '佩特夏/宠物店'（括号/斜杠没拆干净）。
      → 所有写入 result 的名字**统一走这里**。
    """
    s = re.sub(r"[（(][^（()）]*[）)]", "", s or "")
    s = re.split(r"[/／]", s)[0]
    s = s.strip(" ·-·・\"'“”「」")
    if not s or not re.search(r"[\u4e00-\u9fff]", s) or len(s) > 10:
        return ""
    return s


def main() -> int:
    print("=" * 64)
    print("补全角色中文名（M21：中文维基替身表 → 本体列）")
    print("=" * 64)

    if _OPENCC is None:
        print("  ! 建议先装 opencc（否则繁简转换不全）：")
        print("    pip install opencc-python-reimplemented")

    wt = fetch_wikitext()
    rows = [r for r in parse_rows(wt) if len(r) >= 3]
    print(f"  解析表格行 {len(rows)} 行")

    # 维基行 → (替身英文名, 替身中文名, 本体原文)
    wiki: dict[str, str] = {}
    wiki_zh: dict[str, str] = {}
    for r in rows:
        en = clean_cell(r[0])
        zh = clean_cell(r[1])
        owner = clean_cell(r[2])
        if not re.match(r"^[A-Za-z][A-Za-z0-9'’.\-·: ]*$", en):
            continue
        if not owner or len(owner) > 60:
            continue
        wiki.setdefault(en, owner)
        if zh:
            wiki_zh.setdefault(en, zh)
    print(f"  提取替身→本体 {len(wiki)} 条（含中文名 {len(wiki_zh)}）")

    # DB：替身 → 角色；以及全部角色
    sys.path.insert(0, str(ROOT / "database"))
    sys.path.insert(0, str(ROOT))
    import psycopg2
    from load_db import PG
    conn = psycopg2.connect(**PG)
    cur = conn.cursor()
    cur.execute("""SELECT s.name_en, c.character_id, c.name_en, c.part
                   FROM stands s JOIN characters c
                     ON c.character_id = s.owner_id""")
    db = cur.fetchall()
    cur.execute("SELECT character_id, name_en FROM characters")
    all_chars = {r[0]: r[1] for r in cur.fetchall()}
    cur.close()
    conn.close()

    # 路径A 索引：替身英文名 → (角色)
    by_norm: dict[str, tuple] = {}
    for row in db:
        by_norm.setdefault(norm(row[0]), row)
    # 路径B 索引：角色英文名 → character_id
    char_by_norm: dict[str, str] = {}
    for cid, en in all_chars.items():
        if en:
            char_by_norm.setdefault(norm(en), cid)

    # ★★ 路径C 索引：替身**中文名** → stand_id ★★
    #   为什么需要（实测）：维基与 DB 的替身英文名常有拼写差异
    #     （维基 'Scary Monster' ↔ DB 'Scary Monsters'；
    #       维基 'Bast' ↔ DB 'Bastet'；'Echoes Act 1' ↔ 'Echoes ACT1'）
    #   → 英文名对不上时，改用**中文名**当桥梁：
    #     维基行的替身中文名 ↔ 项目已有的 stand_name_zh.json（154 条）
    #     中文名是人工译名，两边的差异比英文名小得多。
    zh_bridge: dict[str, str] = {}
    zh_src = ROOT / "dataset" / "processed" / "stand_name_zh.json"
    if zh_src.exists():
        try:
            zmap = json.loads(zh_src.read_text(encoding="utf-8"))
            for sid, rec in zmap.items():
                if not isinstance(rec, dict):
                    continue
                vals = [rec.get("name_zh")] + list(
                    rec.get("name_zh_variants") or [])
                for v in vals:
                    if v:
                        # ★ 两侧都归一成简体 + 去空格再比，
                        #   否则维基的「鋼鏈手指」对不上库里的「钢链手指」（实测）
                        zh_bridge.setdefault(
                            to_simplified(re.sub(r"\s", "", v)), sid)
        except (OSError, json.JSONDecodeError):
            pass
    print(f"  中文名桥（替身中文名→stand_id）：{len(zh_bridge)} 条")
    # stand_id → character_id（用于路径C 反查 owner）
    stand2char: dict[str, str] = {}
    for row in db:
        stand2char.setdefault(row[0], row[1])

    result: dict[str, list[str]] = {}

    def add(cid: str, names: list[str]) -> None:
        if not cid or not names:
            return
        bucket = result.setdefault(cid, [])
        for n in names:
            # ★ 统一走 clean_name 收口（三条路径的脏值都在这挡掉）
            n = clean_name(n)
            if n and n not in bucket:
                bucket.append(n)

    # ★★ 先扫一遍全表，建「本体英文名 → 中文名」的**全局**映射 ★★
    #   为什么必须先全局收集（实测踩坑）：
    #     DIO 的主替身行（The World）本体列只写了 "DIO"（无中文），
    #     而派生行（The World－Over Heaven）写的是 "Dio/迪奧·布蘭度"。
    #     若只在"当前行有中文"时才用英文名匹配，就永远补不到 DIO。
    #   → 先把所有行的 (英文名 → 中文名) 都收起来，再回头补。
    en2zh: dict[str, list[str]] = {}
    for en, owner_raw in wiki.items():
        zh_names = split_owners(owner_raw)
        if not zh_names:
            continue
        for oen in split_owner_en(owner_raw):
            bucket = en2zh.setdefault(norm(oen), [])
            for n in zh_names:
                if n not in bucket:
                    bucket.append(n)

    stats = {"A": 0, "B": 0, "C": 0, "A_fallback": 0}
    matched_en: set[str] = set()
    for en, owner_raw in wiki.items():
        row = by_norm.get(norm(en))
        names = split_owners(owner_raw)
        # ★ 路径C：替身英文名对不上时，用**替身中文名**做桥
        if not row:
            zh_cell = wiki_zh.get(en, "")
            key = to_simplified(re.sub(r"\s", "", zh_cell)) if zh_cell else ""
            sid = zh_bridge.get(key) if key else None
            cid = stand2char.get(sid) if sid else None
            if cid and names:
                add(cid, names)
                stats["C"] += 1
            continue
        matched_en.add(en)
        if names:
            add(row[1], names)
            stats["A"] += 1
            continue
        # 本行本体列没中文 → 用全局表按英文名兜底
        for oen in split_owner_en(owner_raw):
            names = en2zh.get(norm(oen), [])
            if names:
                add(row[1], names)
                stats["A_fallback"] += 1
                break

    # ★ 路径B：拿角色的英文名，去全局表查中文名（覆盖那些主替身行没给中文的）
    for cid, cen in all_chars.items():
        if cid in result or not cen:
            continue
        names = en2zh.get(norm(cen))
        if names:
            add(cid, names)
            stats["B"] += 1

    # ---- 数据源 2：各部条目的 nihongo 模板 ----
    parts_wt = fetch_parts_wikitext()
    nihongo: list[tuple[str, str]] = []
    for pg, wtxt in parts_wt.items():
        nihongo.extend(extract_nihongo(wtxt))
    char_by_namekey: dict[str, str] = {}
    char_by_loose: dict[str, str] = {}
    for cid, cen in all_chars.items():
        if cen:
            char_by_namekey.setdefault(name_key(cen), cid)
            char_by_loose.setdefault(loose_key(cen), cid)
    stats["D"] = 0
    for zh, en in nihongo:
        if not en:
            continue
        # ① 严格键（排序 + 去冠词）
        cid = char_by_namekey.get(name_key(en))
        # ② 松键（去标点空格 + 去冠词，不排序）
        if not cid:
            cid = char_by_loose.get(loose_key(en))
        # ③ 包含匹配 —— 处理 DB 的**粘连名**
        #   实测 DB `andre_boombooml_a_boomboom` 的 name_en 是
        #   "Andre BoomboomL.A. Boomboom"（两个名字粘一起），
        #   维基是两个独立条目 → 前两步都对不上。
        #
        #   ★★ 两边都要够长（实测踩坑）：只限制 lk 长度会误伤 ——
        #     'Radio Gaga' 的 lk='radiogaga'，而 DB 的 'DIO' 的 k='dio'，
        #     'dio' 恰好是 'ra**dio**gaga' 的子串 → 维基的「嘎嘎电台」
        #     被错误挂到 DIO 名下。**短名字做子串匹配必然误伤**。
        if not cid:
            lk = loose_key(en)
            if len(lk) >= 6:
                for k, v in char_by_loose.items():
                    if len(k) >= 5 and k != lk and (lk in k or k in lk):
                        cid = v
                        break
        if cid:
            before = len(result.get(cid, []))
            add(cid, [to_simplified(zh)])
            if len(result.get(cid, [])) > before:
                stats["D"] += 1
    print(f"  nihongo 模板 {len(nihongo)} 个 → 命中 {stats['D']} 条")

    print(f"\n  路径A（替身名→stand→owner，本行有中文）：{stats['A']} 条"
          f"（替身名匹配 {len(matched_en)}/{len(wiki)}）")
    print(f"  路径A兜底（本行无中文，靠英文名跨行取）：{stats['A_fallback']} 条")
    print(f"  路径C（替身中文名做桥）：{stats['C']} 条")
    print(f"  路径B（角色英文名反查）：{stats['B']} 条")
    print(f"  ★ 覆盖角色：{len(result)} / {len(all_chars)}")
    missing = [c for c in all_chars if c not in result]
    print(f"  仍缺：{len(missing)}")

    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2,
                              sort_keys=True), encoding="utf-8")
    print(f"\n  已写 {OUT.relative_to(ROOT)}")

    print("\n  样本 14 条：")
    for cid, names in list(result.items())[:14]:
        print(f"    {all_chars[cid]:26s} -> {names}")

    if missing:
        print(f"\n  缺口清单（{len(missing)}）：")
        for cid in sorted(missing, key=lambda c: (all_chars[c] or "")):
            print(f"    {cid:26s} {all_chars[cid]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
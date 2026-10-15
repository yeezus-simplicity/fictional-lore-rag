"""从 jojowiki 角色页构建结构化角色数据（M66：人物背景）。

== 为什么要做 ==
用户最初的诉求是「补一下人物的背景 这样12部都能加进去了」。
M65 做了**地点**；替身天然只覆盖第 3–9 部，**第 1、2 部只有人物与地点**。
角色页早就在语料里（168 页、中位 2.2 万字符），但 `characters` 表只有 5 个字段
（character_id / name_en / name_ja / part / stand_ids），**没有简介**，
所以「东方仗助是谁」答不了。

== 数据里有什么（都是源站原文，不加工）==
· Infobox（**字段极全**）：ja_kanji / ja_romaji / alias / stand / age / birthday /
  gender / height / weight / blood / nation / occupation / family / affiliation /
  status / mangadebut / mangafinal / animedebut / seiyuu / voiceactor / namesake
· 正文分段：**lead（首段就是"背景"）**、`Background`、`Personality`、
  `Appearance`、`Abilities`、`Relationships` …
  实测 lead 形如：
    "Mariah is a secondary antagonist featured in the third part of the
     JoJo's Bizarre Adventure series, Stardust Crusaders, specifically …"
  —— 这正是「人物背景」本身，**不需要我改写**。

== ★★ 本项目的两条硬规则在这里怎么落地 ★★
  ① **不机翻**：简介保留英文原文，中文只给**结构化事实**（中文名/替身/部数）。
     这与 M32 中文摘要卡同一口径 —— 机器翻译会破坏「证据可追溯」。
  ② **每一条都要能指出语料块**：`bio_chunk_id` / `background_chunk_id` /
     `personality_chunk_id` 直接来自 `article_chunks.json` 的块 id
     （角色页切块时 `character_id` 已落在块上）。前端点开就能看原文。

用法：
    python dataset/sources/jojowiki/build_characters.py
输出：
    dataset/processed/characters_ext.json
"""
from __future__ import annotations

import collections
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from wash import wash, split_sections            # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
PROC = ROOT / "dataset" / "processed"
ARTICLES = HERE / "articles.json"
CHUNKS = PROC / "article_chunks.json"
STANDS = PROC / "stands.json"
OUT = PROC / "characters_ext.json"

sys.path.insert(0, str(ROOT / "database"))
from load_db import _char_id                      # noqa: E402

# ★ 判部逻辑直接复用地点那边的（同一套编号区间），**不复制一份** ——
#   复制会让两处慢慢分叉（M65 已经为"口径必须唯一"吃过一次亏）。
from build_locations import part_of               # noqa: E402


def _read_json(path: Path):
    """读 JSON；文件不存在就返回 None（不抛异常，便于在 CI 上少文件时跑）。"""
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


# Infobox 里值得落到界面上的字段（其余保留在 infobox 原始串里，不猜）
SHOW_FIELDS = ("ja_kanji", "ja_romaji", "alias", "stand", "age", "birthday",
               "gender", "height", "weight", "blood", "nation", "occupation",
               "family", "affiliation", "status", "mangadebut", "mangafinal",
               "animedebut", "seiyuu", "voiceactor", "namesake", "residence")
# 多值字段（源站用 `<br />` 并列）→ **显式给出复数名**。
#   ★ 不用 f"{k}s" 机械拼：`alias` → `aliass` 这种拼法是错的
#     （与 M41「机械转换会造出自造词」是同一类问题）。
MULTI_FIELDS = {"alias": "aliases", "nation": "nations",
                "occupation": "occupations", "family": "families",
                "seiyuu": "seiyuus", "voiceactor": "voiceactors",
                "affiliation": "affiliations", "namesake": "namesakes",
                "stand": "stands"}

_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]")


# ---------------------------------------------------------------
# 清洗
# ---------------------------------------------------------------
def infobox(wt: str) -> str:
    """截出 `{{Character Infobox …}}`（**数括号配对**）。

    ★★ 必须数括号，不能用非贪婪正则 ★★
      M65 就是在这里栽的：Infobox 里嵌 `{{Tabber}}`，其 `}}` 独占一行
      → `(.*?)\\n}}` 提前截断 → 我据此判定"字段不在 Infobox 里"，
      **而那个前提本身就是错的**，连带一串错误结论。
    """
    m = re.search(r"\{\{\s*Character(?:\s+Infobox)?\s*[|}]", wt)
    if not m:
        return ""
    i = wt.index("{{", m.start())
    depth = 0
    j = i
    while j < len(wt):
        if wt.startswith("{{", j):
            depth += 1
            j += 2
            continue
        if wt.startswith("}}", j):
            depth -= 1
            j += 2
            if depth == 0:
                break
            continue
        j += 1
    return wt[i:j]


def clean(s: str | None) -> str | None:
    """把 Infobox 的字段值清成**人能读的纯文本**。

    ★ 顺序很重要（M65 的教训）：**先把有真值的模板转成文本，再删噪声**。
      若先 `re.sub(r"\\{\\{[^}]*\\}\\}", "", s)` 一刀切，
      `{{Gender|Male}}` / `{{cvt|180|cm}}` 里的真值会一起没了。
    ★ 空白类一律 `[^\\S\\n]`（**不含换行**）—— M65 的坑 5：
      `\\s*` 会吃掉换行，把下一行整行当成这个字段的值。
    """
    if not s:
        return None
    t = s
    # ① 引用与注记（无信息量）
    #   ★ 外部链接也要清：实测 seiyuu 字段里是
    #     `[http://myanimelist.net/people/12345 Yuki Ono]` 这种写法 ——
    #     不清就会在界面上出现一整条 URL（M65 的"脏值"同类问题）。
    t = re.sub(r"\[https?://[^\s\]]+\s*([^\]]*)\]",
               lambda m: (m.group(1) or "").strip(), t)
    t = re.sub(r"https?://[^\s\]|]+", "", t)
    t = re.sub(r"<ref[^>]*/>", "", t, flags=re.I)
    t = re.sub(r"<ref[^>]*>.*?</ref>", "", t, flags=re.I | re.S)
    #   ★ `{{S|…}}` / `{{Ch|…}}` / `{{Ref-Note|…}}` **不在这里单独删** ——
    #     它们是"注记"（无信息量），但**可能嵌着别的模板**（见 _strip_templates），
    #     逐层正则会只吃掉一半、留下 `]] only}}` 这种残句。
    #     统一交给下面的配对扫描处理（实测就是这么修好 DIO 的 voiceactor 的）。
    # ② 有真值的模板 → 取真值；其余模板整个丢弃（**一次配对扫描**，见上）
    t = _expand_simple_templates(t)
    # ③ 兜底：源站偶有**多余括号**的写法（实测
    #    `|status = Retired {{S|Part 7}}}}` —— 4 个右括号），
    #    扫描器吃掉配对的 2 个，会剩一对孤儿 `}}` → 这里收掉。
    t = _strip_templates(t)
    # ⑤ wiki 标记
    t = re.sub(r"\[\[([^\]|]*)\|([^\]]*)\]\]", r"\2", t)
    t = re.sub(r"\[\[([^\]]*)\]\]", r"\1", t)
    # ★ 孤儿链接括号（模板被剥掉后可能剩下半截 `]]`）
    t = re.sub(r"\[\[|\]\]", "", t)
    t = t.replace("'''", "").replace("''", "")
    t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I)
    t = re.sub(r"</?[a-z][^>]*>", "", t, flags=re.I)
    # ⑥ 空白归一（★ 只碰空格/制表，不碰换行 —— 换行是多值分隔符）
    t = re.sub(r"[^\S\n]+", " ", t)
    t = re.sub(r"\n{2,}", "\n", t)
    return t.strip(" \n|") or None


def split_multi(s: str | None) -> list[str]:
    """多值字段（alias / family / seiyuu…）→ 列表。源站用 `<br />` 分隔。"""
    if not s:
        return []
    out = []
    for seg in s.split("\n"):
        seg = seg.strip(" ·,;")
        if seg:
            out.append(seg)
    return out


def links(text: str | None) -> list[str]:
    """取 wiki 链接的**目标名**（用于 `stand = [[Crazy Diamond]]`）。"""
    return [x.strip() for x in re.findall(r"\[\[([^\]|]+)", text or "")]


def _strip_templates(s: str) -> str:
    """**按括号配对**剥掉所有 `{{…}}`（含嵌套）。

    ★ 为什么不能用循环版正则：实测 DIO 的 voiceactor 是
      `{{S|Latin Spanish Dub, [[…|SO, {{S|ep. 12}}]] only}}`
      —— S 模板里嵌了链接、链接里又嵌了 S 模板。
      逐层正则只能吃掉一半，剩下 `]] only}}`，清理后变成
      `Sergio Becerril ]] only` 这种残句（脏值自检就是抓到了它）。
      配对扫描一次到位。
    """
    out: list[str] = []
    i = 0
    depth = 0
    while i < len(s):
        if s.startswith("{{", i):
            depth += 1
            i += 2
            continue
        if s.startswith("}}", i):
            if depth:
                depth -= 1
            i += 2
            continue
        if depth == 0:
            out.append(s[i])
        i += 1
    return "".join(out)


def _expand_simple_templates(s: str) -> str:
    """把「有真值」的模板换成真值，其余模板整个丢掉 —— **一次括号配对扫描**。

    ★★ 为什么不能用逐条正则（我第一版就是这么写的，实测连栽两次）★★
      真实数据里模板是**嵌套**的：
        · `{{cvt|185<ref name="chamt" />{{CharSheet}}|cm|ftin}}`
          —— cvt 里嵌了 ref 和 CharSheet
        · `{{S|Latin Spanish Dub, [[…|SO, {{S|ep. 12}}]] only}}`
          —— S 里嵌了链接，链接里又嵌了 S
      `[^}]*` 这类写法遇到内层 `}` 就停，于是要么**取不到值**
      （DIO 的身高整个丢失）、要么**留半截残句**（`]] only}}`）。
      配对扫描 + 内层先展开，一次到位。

    Args:
        只处理"值在参数里"的模板；未知模板**整段丢弃**（宁可少显示，不留残骸）。
    """
    out: list[str] = []
    i = 0
    while i < len(s):
        if not s.startswith("{{", i):
            out.append(s[i])
            i += 1
            continue
        # 找配对的结束位置
        depth = 0
        j = i
        while j < len(s):
            if s.startswith("{{", j):
                depth += 1
                j += 2
                continue
            if s.startswith("}}", j):
                depth -= 1
                j += 2
                if depth == 0:
                    break
                continue
            j += 1
        closed = s[j - 2:j] == "}}"
        body = s[i + 2:(j - 2 if closed else j)]
        body = _expand_simple_templates(body)          # ★ 内层先展开
        parts = [p.strip() for p in body.split("|")]
        name = (parts[0].strip().lower() if parts else "")
        val: str | None = None
        if name == "cvt" and len(parts) > 1:
            m = re.search(r"[\d.]+", parts[1])
            val = m.group(0) if m else None
        elif name == "gender" and len(parts) > 1:
            val = parts[1] or None
        elif name in ("w", "wp") and len(parts) > 1:
            # `{{W|Page|Display}}` → 有显示名用显示名，否则用页面名
            val = (parts[2] or parts[1]) if len(parts) > 2 else parts[1]
        elif name in ("ruby", "nihongo", "nihongo2", "nihongo3") and len(parts) > 1:
            val = parts[1] or None
        if val:
            out.append(val)
        i = j
    return "".join(out)


def tidy_prose(s: str | None) -> str | None:
    """正文类字段（简介/背景/性格）的最后一道清洗。

    ★ 为什么还要单独做一道：这几段来自 `wash()`，而 wash 是**按括号配对**处理
      模板的；源站偶有**标记本身写错**的页面。实测 `Diego Brando` 的首段是：
        `commonly referred to as {{Ruby|Dio|ディオ}}'''}}, is a primary antagonist`
      —— `'''` 后面多了一个 `}}`，配对就断了，于是输出里留下半个 `{{Ruby`。
      5712 个字段里只有这 1 处，但**半截模板会直接显示在答案正文上**，
      属于"看着有值其实是脏的"那一类，必须收掉。
    """
    if not s:
        return None
    t = s
    # ★ 媒体残片先清 —— 实测 `Josuke` 的 Personality 首段开头是
    #   `|260x260px|alt=]]`（源站 File 链接的一截），它会成为"第一段"的开头。
    t = re.sub(r"\[\[[^\]]*\]\]", "", t)
    t = re.sub(r"\|[^\n|]*px[^\n]*", "", t)
    t = re.sub(r"[^\S\n]*\|[^\S\n]*alt\s*=[^\n]*", "", t)
    t = t.replace("[[", "").replace("]]", "")
    t = _strip_templates(t)
    # ★ 源站标记写错时会**留下半截模板**（见函数说明里的 Diego Brando）。
    #   它会把**错误的词**留在答案里（显示成 "referred to as Ruby"，真值 Dio）
    #   —— 宁可少一句，不留错词：连同它后面到下一个逗号/句号的内容一起去掉。
    #   ★ 顺序：必须在 `replace("{{","")` **之前**，否则 `{{` 已经没了、规则匹配不到
    #     （我第一版就是写在后面，结果 Ruby 还是留在答案里）。
    t = re.sub(r"\{\{\s*[A-Za-z]+\b[^,.;]*", "", t)
    t = t.replace("{{", "").replace("}}", "")
    t = t.replace("'''", "").replace("''", "")
    t = re.sub(r"[^\S\n]+([,.;])", r"\1", t)
    t = re.sub(r"[^\S\n]{2,}", " ", t)
    return t.strip(" \n|") or None


def field(ib: str, key: str) -> str | None:
    """取 Infobox 的单个字段值（**空白类不含换行**，见 clean() 的说明）。"""
    m = re.search(r"^[^\S\n]*\|[^\S\n]*%s[^\S\n]*=[^\S\n]*(.+)$"
                  % re.escape(key), ib, re.M)
    return m.group(1).strip() if m else None


def first_para(section_text: str | None, limit: int = 700) -> str | None:
    """取该节的**第一段**（首段才是概述，后面是细节）。"""
    if not section_text:
        return None
    for para in section_text.split("\n\n"):
        p = para.strip()
        if len(p) >= 60:               # 太短的多是标题残留/列表碎片
            return p[:limit].rstrip()
    return None


# ---------------------------------------------------------------
def main() -> int:
    if not ARTICLES.exists():
        print(f"✗ 缺少 {ARTICLES}（先跑 fetch_articles.py）")
        return 1

    art = json.loads(ARTICLES.read_text(encoding="utf-8"))
    pages = [p for p in art.get("pages", []) if p.get("kind") == "character"]
    stands = json.loads(STANDS.read_text(encoding="utf-8")) if STANDS.exists() else []
    stand_by_name = {(s.get("name_en") or "").lower(): s.get("stand_id")
                     for s in stands if s.get("name_en")}

    # 角色中文名（130 个，来自既有别名系统）
    char_zh: dict[str, list[str]] = {}
    try:
        sys.path.insert(0, str(ROOT / "services"))
        import aliases                                  # noqa: E402
        char_zh = dict(aliases.load_char_zh_map() or {})
    except Exception as ex:                             # noqa: BLE001
        print(f"  ⚠ 角色中文名加载失败（{type(ex).__name__}）：只有英/日名")

    # 语料块：给"简介/背景/性格"配 chunk_id（★ 证据可追溯）
    #   ★★ 两级查找（我第一版只做了第一级，实测 4 个角色拿不到块号）★★
    #   语料块是按 `min_len=200` 合并出来的 —— 一个**短的 Background 节**
    #   会被并进上一块，那一块的 `section` 就成了别的名字。
    #   所以：先按 (角色, 节名) 精确找；找不到就**按内容回查**
    #   （拿这段原文的前 60 个字符去块里搜）—— 这样指到的一定是
    #   "真正含有这段文字的那一块"，而不是猜一个。
    chunks_of: dict[str, list[tuple]] = {}
    for c in (_read_json(CHUNKS) or []):
        if (c.get("extra") or {}).get("kind") != "character":
            continue
        cid = c.get("character_id")
        if cid:
            chunks_of.setdefault(cid, []).append(
                (c.get("chunk_id"), c.get("section") or "(lead)",
                 c.get("content") or ""))

    def chunk_id_of(cid: str, section: str, text: str | None) -> int | None:
        for ck, sec, _ in chunks_of.get(cid, []):
            if sec == section:
                return ck
        if not text:
            return None
        probe = re.sub(r"\s+", " ", text).strip()[:60]
        if len(probe) < 20:
            return None
        for ck, _, content in chunks_of.get(cid, []):
            if probe in re.sub(r"\s+", " ", content):
                return ck
        return None

    out: list[dict] = []
    stat = collections.Counter()
    for p in pages:
        title = p["title"]
        wt = p["wikitext"]
        cid = _char_id(title)
        ib = infobox(wt)
        raw = {k: field(ib, k) for k in SHOW_FIELDS}
        f = {k: clean(v) for k, v in raw.items()}

        # 正文分段
        #   ★ split_sections 会把**节标题前缀进内容**（wash.py 第 173–175 行，
        #     那是为了检索时标题不脱离正文）→ 取第一段前必须把这一行剥掉，
        #     否则简介里会冒出一个孤零零的 "Background"。
        text = wash(wt)
        secs = {}
        for s in split_sections(text, min_len=1, max_len=10 ** 9):
            key = s["section"] or "(lead)"
            body = s["content"]
            if s["section"] and body.startswith(s["section"] + "\n"):
                body = body[len(s["section"]) + 1:]
            secs[key] = body
        bio = tidy_prose(first_para(secs.get("(lead)")))
        bg = tidy_prose(first_para(secs.get("Background")))
        pers = tidy_prose(first_para(secs.get("Personality")))

        part, basis = part_of(f.get("mangadebut"))
        stand_names = links(raw.get("stand"))
        stand_ids = [stand_by_name[n.lower()] for n in stand_names
                     if n.lower() in stand_by_name]

        rec = {
            "character_id": cid,
            "title": title,
            "name_en": title,
            "name_ja": f.get("ja_kanji"),
            "name_romaji": f.get("ja_romaji"),
            "name_zh": (char_zh.get(cid) or [None])[0],
            "name_zh_variants": [x for x in (char_zh.get(cid) or [])
                                 if x != (char_zh.get(cid) or [None])[0]],
            "part": part,
            "part_basis": basis,
            "stand_names": stand_names,
            "stand_ids": stand_ids,
            "bio": bio,
            "background": bg,
            "personality": pers,
            "bio_chunk_id": chunk_id_of(cid, "(lead)", bio),
            "background_chunk_id": chunk_id_of(cid, "Background", bg),
            "personality_chunk_id": chunk_id_of(cid, "Personality", pers),
            # ★ 多值字段拆成列表（源站用 <br /> 并列，不是同一值的分隔）
            **{MULTI_FIELDS[k] if k in MULTI_FIELDS else k:
               (split_multi(f.get(k)) if k in MULTI_FIELDS else f.get(k))
               for k in SHOW_FIELDS},
            # ★ 保留原始 Infobox —— 界面上没展示的字段也**不丢**，
            #   方便后续补展示而不必重抓（articles.json 是 .gitignore 的）
            "infobox_raw": ib[:4000] or None,
            "n_sections": len(secs),
            "source_url": p.get("url"),
            "wt_len": p.get("wt_len"),
            # 已知数据问题：13 个页面抓不到（名字像**两个角色名粘连**，
            #   如 `Andre BoomboomL.A. Boomboom` / `Dixie FlatlineN`）
            "suspect_title": bool(_CJK.search(title)) or
                             bool(re.search(r"[a-z][A-Z]", title or "")),
        }
        out.append(rec)
        stat["total"] += 1
        for k in ("name_ja", "bio", "background", "personality", "part"):
            if rec.get(k) is not None:
                stat[k] += 1
        if rec["stand_ids"]:
            stat["stand"] += 1

    OUT.write_text(json.dumps({
        "n_characters": len(out),
        "_note": ("结构化角色数据（M66）。简介/背景/性格都是源站原文（英文），"
                  "**不做机器翻译**；每条都带 chunk_id 可回查原文。"),
        "characters": out,
    }, ensure_ascii=False), encoding="utf-8")

    n = len(out)
    print(f"\n{'='*62}")
    print(f"角色 {n} 个   （源站角色页 {len(pages)}）")
    print(f"  日文名 {stat['name_ja']} · 有替身 {stat['stand']} · 可判部 {stat['part']}")
    print(f"  ★ 简介(lead) {stat['bio']} · 背景节 {stat['background']} · "
          f"性格节 {stat['personality']}")
    print(f"  中文名 {sum(1 for r in out if r['name_zh'])}")
    print(f"  标题可疑（疑两角色名粘连）{sum(1 for r in out if r['suspect_title'])}")

    # ---- ★ 脏值自检（M65 的坑 5 与「残留 wiki 标记」都归到这里）----
    PARSED = [k for k in SHOW_FIELDS] + \
             list(MULTI_FIELDS.values()) + \
             ["bio", "background", "personality"]
    bad: list[tuple[str, str, str]] = []
    for r in out:
        for k in PARSED:
            v = r.get(k)
            vals = v if isinstance(v, list) else ([v] if v else [])
            for s in vals:
                if isinstance(s, str) and re.search(
                        r"\{\{|\}\}|\[\[|\]\]|<ref|<br|https?://|=\s*\w+\s*\|", s):
                    bad.append((r["title"], k, s[:60]))
                    break
    print(f"\n  解析后字段残留 wiki 标记：{len(bad)} / {n * len(PARSED)}")
    for t, k, s in bad[:8]:
        print(f"     {t} [{k}] {s}")
    print(f"→ {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

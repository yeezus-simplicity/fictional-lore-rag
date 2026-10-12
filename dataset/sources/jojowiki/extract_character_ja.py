# -*- coding: utf-8 -*-
"""从已抓的角色页 wikitext 提取**日文名**（M45，解决 M38 遗留）。

== 背景 ==
`characters.name_ja` 一直是 **0% 覆盖**（178 条全 NULL）——
M38 遗留两条之一。但数据**其实已经在手上**：
B 方案抓的 `articles.json` 里有 168 个角色页，每个的
`{{Character Infobox}}` 都带 `|ja_kanji = 空条 承太郎`。

== 做法 ==
纯离线提取，**不需要重新抓取**：
  articles.json（角色页 wikitext）
    → 正则取 `|ja_kanji=` 字段
    → 清理 HTML 标签 / 嵌套模板 / 注音
    → 输出 {角色英文名: 日文名}

★ 为什么 key 用**英文名**：`characters.name_en` 是从
  `stands.owner_name` 反推的，与 jojowiki 的页面标题一致，
  用它对齐最直接（不引入新的 id 规则）。

用法：
    python dataset/sources/jojowiki/extract_character_ja.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PROC = ROOT / "dataset" / "processed"
ART = ROOT / "dataset" / "sources" / "jojowiki" / "articles.json"
OUT = PROC / "character_ja.json"

# 日文名占位/无效值
_BAD = {"", "?", "????", "unknown", "n/a", "-", "—"}


def _clean(v: str) -> str:
    """清掉 wiki 标记，只留日文名。

    ★ 实测踩过的形态：
      · `<ref>東立版</ref>`         → 引用要整块去
      · `{{Nihongo|空条 承太郎|…}}` → 嵌套模板要去
      · `空条 承太郎<br/>`          → 换行标签要去
      · `空条 承太郎（くうじょう）`   → 注音括号保留原文更易读，不删
    """
    v = re.sub(r"<ref[^>]*>.*?</ref>", "", v, flags=re.S | re.I)
    v = re.sub(r"<ref[^>]*/>", "", v)
    v = re.sub(r"<br\s*/?>", " ", v, flags=re.I)
    v = re.sub(r"<[^>]+>", "", v)
    # 嵌套模板：先尝试取 Nihongo 的第一个非空参数（那才是名字）
    m = re.search(r"\{\{\s*[Nn]ihongo\s*\|([^|}]+)", v)
    if m:
        v = m.group(1)
    else:
        v = re.sub(r"\{\{[^{}]*\}\}", "", v)
    v = re.sub(r"'{2,}", "", v)
    v = re.sub(r"\s+", " ", v)
    return v.strip(" |　")


def main() -> int:
    if not ART.exists():
        print(f"✗ 缺少 {ART}（先跑 fetch_articles.py）")
        return 1
    data = json.loads(ART.read_text(encoding="utf-8"))
    chars = [p for p in data.get("pages", []) if p.get("kind") == "character"]
    print(f"角色页 {len(chars)} 个")

    out: dict[str, str] = {}
    n_bad = 0
    for p in chars:
        wt = p.get("wikitext") or ""
        m = re.search(r"\|\s*ja_kanji\s*=([^\n|]{1,80})", wt)
        if not m:
            continue
        v = _clean(m.group(1))
        if not v or v.lower() in _BAD:
            n_bad += 1
            continue
        # 至少要含一个日文假名或汉字，否则多半是残留标记
        if not re.search(r"[\u3040-\u30ff\u4e00-\u9fff]", v):
            n_bad += 1
            continue
        out[p["title"]] = v

    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"提取到日文名 {len(out)} 条（跳过无效 {n_bad}）")
    for k, v in list(out.items())[:5]:
        print(f"    {k:28s} → {v}")
    print(f"→ {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

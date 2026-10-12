# -*- coding: utf-8 -*-
"""从萌娘百科补替身中文名 —— 补 B 方案新增的 30 个替身（M41）。

== 起因（用户反馈）==
  「还是缺失很多替身啊 比如繁荣之墓 再度失言 都是第7 9的替身」

  ★ 实测这两个替身**都在库里**：
      繁荣之墓 = Tomb of the Boom 1 2 3（第7部）
      再度失言 = Big Mouth Strikes Again（第9部）
    缺的不是数据，是**中文名映射**：
      stand_name_zh.json 只有 156 条，而 B 方案新补的 30 个替身
      （第8/9部为主）**完全没有中文名**
    → 用户用中文搜，别名表里查不到 → 体感就是「这个替身根本没收录」。

== 为什么另起一个脚本 ==
  - 原 fetch_zh_huiji.py 依赖 `jojo.huijiwiki.com`，实测已 **403**（反爬升级）
  - 萌娘百科主站 `zh.moegirl.org.cn` 的 API 返回
    `action-notallowed: Unauthorized API call`
  - 实测 **`moegirl.uk`（萌娘百科镜像）API 可用**，且：
      · `list=search` 用**英文名**能直接搜到中文条目名
        （November Rain → 十一月雨 / Tomb of the Boom → 繁荣之墓）
      · `prop=revisions` 能取到条目 wikitext
      · 条目的 `{{替身信息}}` 模板里有 `title` 与 `别名` 字段

== 收录原则（★ 宁可少收，不可错收）==
  1. 条目正文必须出现**目标英文名**（规范化后）→ 防止搜到角色页/能力分类页
     （实测搜 "November Rain" 第 2 条是「天气操控」，那是能力分类，不是替身）
  2. 别名要清掉模板/ref/删除线等噪声
  3. **单字名不收**（历史教训：单字名会误匹配，见 build_zh_map.py 的
     `_SINGLE_CHAR_OFFICIAL`）
  4. 拿不到就留空，**不推测、不机翻**

用法：
    python dataset/sources/zh_names/fetch_moegirl_stands.py            # 只补缺中文名的
    python dataset/sources/zh_names/fetch_moegirl_stands.py --all      # 全部重抓
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PROC = ROOT / "dataset" / "processed"
HERE = Path(__file__).resolve().parent
CACHE = HERE / "_cache_moegirl_stands.json"
OUT = HERE / "zh_moegirl_pairs.json"

API = "https://moegirl.uk/api.php"
LEVEL = "C"          # 单来源（萌娘百科），与既有 C 级一致
SOURCE = "moegirl.uk"


def _opener():
    px = os.environ.get("https_proxy") or os.environ.get("http_proxy")
    op = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": px, "https": px}))
    op.addheaders = [("User-Agent", "Mozilla/5.0 (research; educational; rag-kb)")]
    return op


OP = _opener()


def api(params: dict, tries: int = 3) -> dict | None:
    """调萌娘 API，带重试（实测 SSL 偶发 UNEXPECTED_EOF）。"""
    u = API + "?" + urllib.parse.urlencode(params)
    for i in range(tries):
        try:
            return json.loads(OP.open(u, timeout=40).read())
        except Exception:
            if i == tries - 1:
                return None
            time.sleep(2.0 + i)
    return None


def _clean_wiki(s: str) -> str:
    """清掉 wiki 标记，只留纯文本。"""
    s = re.sub(r"<ref[^>]*>.*?</ref>", "", s, flags=re.S | re.I)
    s = re.sub(r"<ref[^>]*/>", "", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\{\{[^{}]*\}\}", "", s)          # 单层模板
    s = re.sub(r"\[\[[^\]|]*\|([^\]]*)\]\]", r"\1", s)
    s = re.sub(r"\[\[([^\]]*)\]\]", r"\1", s)
    s = re.sub(r"'{2,}", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _han_n(s: str) -> int:
    return len(re.findall(r"[\u4e00-\u9fff]", s or ""))


def _zh_only(s: str) -> bool:
    """是否含中文、且不是单字（单字名会误匹配，一律不收）。"""
    return _han_n(s) >= 2


def _usable_zh(s: str) -> bool:
    """★ 可收为**别名**的译名：汉字数 ≥3。

    为什么卡 3 字：实测新增的 2 字译名里有「**天气**」，
    而「今天天气怎么样」是日常问句 → 被误判成查询 Weather Report
    （test_m21 的「无关问句仍拒答」因此回归失败）。
    同类还有「奶油」「手枪」「鲨鱼」「丰收」「折纸」等日常词。
    ★ 与既有原则一致：宁可少收 —— 用户仍可用全名查询。
    """
    return _han_n(s) >= 3


def _norm_en(s: str) -> str:
    """英文名规范化 —— ★ 必须与 build_zh_map.py 的 norm_en 完全一致。

    历史教训（build_zh_map.py 注释里记着）：两侧规范化规则一旦不同，
    key 就对不上，154 个里只匹配上 30 个。
    """
    s = unicodedata.normalize("NFKC", s).lower()
    return re.sub(r"[^a-z0-9]", "", s)


def search_title(en: str) -> list[str]:
    d = api({"action": "query", "list": "search", "srsearch": en,
             "srlimit": 5, "format": "json", "formatversion": "2"})
    if not d:
        return []
    return [x["title"] for x in d.get("query", {}).get("search", [])]


def page_content(title: str) -> str | None:
    d = api({"action": "query", "prop": "revisions", "rvprop": "content",
             "rvslots": "main", "format": "json", "formatversion": "2",
             "titles": title, "redirects": "1"})
    if not d:
        return None
    pages = d.get("query", {}).get("pages", [])
    if not pages or pages[0].get("missing"):
        return None
    try:
        return pages[0]["revisions"][0]["slots"]["main"]["content"]
    except Exception:
        return None


def parse_entry(wt: str, en: str) -> dict | None:
    """从条目 wikitext 提取主译名与别名。

    ★ 关键校验：正文（或 title 字段）里必须出现目标英文名，
      否则说明搜到的是角色页/能力页 → 丢弃。
    """
    norm = re.sub(r"[^a-z0-9]", "", en.lower())
    # ★ 库内名可能带形态后缀（"Tomb of the Boom 1 2 3"），
    #   而萌娘正文用基名（"Tomb of the Boom"）→ 去数字后缀再校验一次。
    #   实测不加这条，「繁荣之墓」会被误判为"搜错条目"而丢弃。
    base_en = re.sub(r"\s*\d+(\s+\d+)*\s*$", "", en).strip()
    norm_base = re.sub(r"[^a-z0-9]", "", base_en.lower())
    # title 字段优先
    m = re.search(r"\|\s*title\s*=([^\n|]{0,60})", wt)
    title_field = _clean_wiki(m.group(1)) if m else ""
    # ★ title 字段也可能是子字段，不是替身名。
    #   实测：「Modern Crusaders」条目的 |title= 取到「原版MV」。
    if re.search(r"(MV|视频|原版|插图|图片|封面|专辑|歌曲|漫画|动画)", title_field):
        title_field = ""
    # 英文名校验：正文前 1500 字里找
    head = re.sub(r"[^a-zA-Z0-9]", "", wt[:1500]).lower()
    title_norm = re.sub(r"[^a-z0-9]", "", title_field.lower())
    ok = (norm in head or norm_base in head or norm == title_norm
          or (title_norm and title_norm in head))
    if not ok:
        return None

    # ★★ 领域校验（精准版）：必须是萌娘的**替身条目** ★★
    #   判据：页面用了 {{替身信息}} 模板 —— 这是萌娘替身条目的固定信息框。
    #
    #   ★ 踩过两次，校验逐步收紧：
    #     ① 只看英文名 → 搜 "Lyin' Eyes" 命中
    #        「迈克尔·杰克逊《Bad》完整版1987」（Eagles 歌名出现在视频页里）
    #     ② 改成「正文含 JOJO」→ 仍漏：搜 "Enigma" 命中
    #        「Modern Crusaders」（Enigma 的同名歌曲页），
    #        该页是 JOJO 相关条目（JOJO 片尾曲），故含 "JOJO"，
    #        而它的 `|title=` 取到「**原版MV**」→ 被当成中文译名收进来。
    #     → 最终判据：页面必须用 {{替身信息}} 模板。这比任何关键词都准。
    if "替身信息" not in wt:
        return None

    # 别名
    m2 = re.search(r"\|\s*别名\s*=([^\n]*)", wt)
    aliases: list[str] = []
    if m2:
        # ★ 必须先整体清洗、再切分：
        #   反过来的话 `<ref>东立版译名</ref>` 会被顿号切碎，
        #   标签不成对 → 清不掉 → 别名里混进 "11月之雨东立版译名<"。
        raw_clean = _clean_wiki(m2.group(1))
        for part in re.split(r"[、,，/]", raw_clean):
            v = part.strip(" 　“”\"'《》")
            if not v or not _zh_only(v):
                continue
            if len(v) > 14:          # 过长多半是说明文字
                continue
            if re.search(r"(译名|个人翻译|暂无|待补)", v):
                continue
            if v not in aliases:
                aliases.append(v)

    # 主译名：条目名若是中文就用它；否则取第一个中文别名
    primary = title_field.strip() if _zh_only(title_field) else ""
    if not primary:
        primary = aliases[0] if aliases else ""
    if not _zh_only(primary):
        return None

    rest = [a for a in aliases if a != primary]
    return {"name_zh": primary, "name_zh_variants": rest,
            "title_field": title_field, "en": en}


def main() -> int:
    stands = json.loads((PROC / "stands.json").read_text(encoding="utf-8"))
    zh_existing = json.loads(
        (PROC / "stand_name_zh.json").read_text(encoding="utf-8"))

    do_all = "--all" in sys.argv
    targets = []
    for s in stands:
        sid = s["stand_id"]
        en = (s.get("name_en") or "").strip()
        if not en:
            continue
        # 形态成员（Tusk ACT1…）不单独抓，母体够用
        if re.match(r"^(echoes|tusk)_(act\d+)$", sid):
            continue
        if not do_all and sid in zh_existing:
            continue
        targets.append({"stand_id": sid, "name_en": en,
                        "part": s.get("part")})

    print(f"待补替身：{len(targets)} 个（{'全量' if do_all else '仅缺中文名的'}）")
    cache: dict[str, dict] = {}
    if CACHE.exists():
        cache = json.loads(CACHE.read_text(encoding="utf-8"))

    # ★ key 用 **规范化英文名**（与 build_zh_map.py 的 norm_en 一致），
    #   不是 stand_id —— 否则两侧 key 对不上，一个都匹配不到。
    out: dict[str, list[str]] = {}
    hit = miss = 0
    for i, t in enumerate(targets, 1):
        sid, en = t["stand_id"], t["name_en"]
        if sid in cache:
            rec = cache[sid]
        else:
            rec = None
            titles = search_title(en)
            # ★ 逐个候选试，取第一个通过校验的
            for title in titles:
                wt = page_content(title)
                if not wt:
                    continue
                # 用 en 校验；条目名本身是英文时也接受
                parsed = parse_entry(wt, en)
                if parsed:
                    parsed["wiki_title"] = title
                    rec = parsed
                    break
                time.sleep(0.4)
            cache[sid] = rec
            CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1),
                             encoding="utf-8")
            time.sleep(0.8)

        if rec:
            # ★ 出口把关：只保留 ≥3 汉字的译名（2 字及以下可能是日常高频词）
            _names = [n for n in
                      [rec["name_zh"]] + rec.get("name_zh_variants", [])
                      if _usable_zh(n)]
            if _names:
                out[_norm_en(en)] = _names
                hit += 1
                _vs = _names[1:]
                print(f"  [{i}/{len(targets)}] ✓ {en:32s} → {_names[0]}"
                      f"{('  异译: ' + '、'.join(_vs)) if _vs else ''}")
            else:
                miss += 1
                print(f"  [{i}/{len(targets)}] – {en:32s} （译名均短于 3 字，不收）")
        else:
            miss += 1
            print(f"  [{i}/{len(targets)}] – {en:32s} （萌娘无条目或未通过校验）")

    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"\n命中 {hit} / 未命中 {miss}  → {OUT.relative_to(ROOT)}")
    print("★ 未命中不等于替身不存在，只是萌娘没有对应条目/未通过英文名校验；"
          "不推测、不机翻。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

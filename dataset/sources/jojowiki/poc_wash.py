"""POC：验证 jojowiki wikitext 能否洗成可读文本。

★ 这是 B 方案（扩语料）的可行性关口：
  wikitext 里塞满 {{模板}} / [[File:]] / <ref> / 表格，
  如果洗出来是残缺句子，整条路就是错的 → 必须先验证再动手。
"""
from __future__ import annotations

import os
import re
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

PX = os.environ.get("https_proxy") or os.environ.get("http_proxy")
OP = urllib.request.build_opener(
    urllib.request.ProxyHandler({"http": PX, "https": PX}))
OP.addheaders = [("User-Agent", "Mozilla/5.0 (research; educational; rag-kb)")]


def fetch_wikitext(title: str) -> str:
    u = ("https://jojowiki.com/api.php?action=parse&prop=wikitext"
         "&redirects=1&format=json&formatversion=2&page="
         + urllib.parse.quote(title))
    d = json.loads(OP.open(u, timeout=60).read())
    return d["parse"]["wikitext"]


# ------------------------------------------------------------------
# 清洗：括号计数法（正则处理不了嵌套模板）
# ------------------------------------------------------------------
# ★★ 关键：模板不能一律删 ★★
#   实测踩坑：jojowiki 用 {{Nihongo|'''Star Platinum'''|スタープラチナ…}}
#   承载**主语**。整块删掉 → 正文变成 "is the Stand of Jotaro Kujo…"，
#   句子缺主语 → BM25 再也匹配不到 "Star Platinum" 这个词。
#   → 这类"内容型模板"要取第一参数，其余（样式/引用/图片）才删除。
_KEEP_FIRST_ARG = {"nihongo", "nihongo2", "ruby"}


def _process_templates(s: str) -> str:
    """逐个处理顶层模板：内容型的取第一参数，其余整块删除。"""
    out: list[str] = []
    buf: list[str] = []
    i, n, depth = 0, len(s), 0
    while i < n:
        if s.startswith("{{", i):
            depth += 1
            if depth == 1:
                buf = []
            else:
                buf.append("{{")
            i += 2
            continue
        if s.startswith("}}", i) and depth > 0:
            depth -= 1
            if depth == 0:
                body = "".join(buf)
                name = body.split("|")[0].strip().lower()
                if name in _KEEP_FIRST_ARG:
                    args = body.split("|")
                    if len(args) > 1 and args[1].strip():
                        out.append(args[1].strip())
            else:
                buf.append("}}")
            i += 2
            continue
        if depth == 0:
            out.append(s[i])
        else:
            buf.append(s[i])
        i += 1
    return "".join(out)


def _strip_braced(s: str, op: str, cl: str) -> str:
    """按深度计数删除配对结构（用于 {| … |} 表格）。"""
    out = []
    i, n, depth = 0, len(s), 0
    while i < n:
        if s.startswith(op, i):
            depth += 1
            i += len(op)
            continue
        if s.startswith(cl, i) and depth > 0:
            depth -= 1
            i += len(cl)
            continue
        if depth == 0:
            out.append(s[i])
        i += 1
    return "".join(out)


def wash(wt: str) -> str:
    s = wt
    # 1. HTML 注释
    s = re.sub(r"<!--.*?-->", "", s, flags=re.S)
    # 2. ref（引用标记）
    s = re.sub(r"<ref[^>]*/>", "", s)
    s = re.sub(r"<ref[^>]*>.*?</ref>", "", s, flags=re.S)
    # 3. gallery / tabber 等容器标签（含内容，通常是图）
    for tag in ("gallery", "tabber", "tabview", "imagemap", "timeline"):
        s = re.sub(rf"<{tag}[^>]*>.*?</{tag}>", "", s, flags=re.S | re.I)
    # 4. 剩余 HTML 标签只去标记不去内容
    s = re.sub(r"<[^>]+>", "", s)
    # 5. 模板：内容型取第一参数（★ 保住主语），其余删除
    s = _process_templates(s)
    # 6. 表格 {| ... |}
    s = _strip_braced(s, "{|", "|}")
    # 7. 文件/图片链接（整条删）
    s = re.sub(r"\[\[(File|Image|Media)\s*:[^\]]*\]\]", "", s, flags=re.I)
    # 8. 内部链接：[[A|B]]→B ; [[A]]→A
    s = re.sub(r"\[\[[^\]|]*\|([^\]]*)\]\]", r"\1", s)
    s = re.sub(r"\[\[([^\]]*)\]\]", r"\1", s)
    # 9. 外部链接：[http://x y] → y
    s = re.sub(r"\[https?://\S+\s+([^\]]*)\]", r"\1", s)
    s = re.sub(r"\[https?://\S+\]", "", s)
    # 10. 强调标记
    s = re.sub(r"'{2,5}", "", s)
    # 11. ★ 先处理列表符号，再处理标题 ★
    #    实测踩坑：反过来的话，标题生成的 "## " 会被列表规则
    #    （`^\s*[*#;:]+\s*`）当成 # 号列表 → "==Appearance==" 变成 "- Appearance"。
    s = re.sub(r"^\s*[*;:]+[ \t]*", "- ", s, flags=re.M)
    # 12. 标题：保留为节标记（切块要用）
    #    ★ 实测格式是 `==Appearance==`（等号与标题间**无空格**）
    s = re.sub(r"^\s*={2,6}\s*(.+?)\s*={2,6}\s*$", r"\n## \1\n",
               s, flags=re.M)
    # 13. 空段压缩
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


if __name__ == "__main__":
    for title in ["Star Platinum", "Jotaro Kujo", "DIO"]:
        wt = fetch_wikitext(title)
        tx = wash(wt)
        paras = [p for p in tx.split("\n") if len(p.strip()) > 60]
        print(f"\n{'='*66}")
        print(f"【{title}】")
        print(f"  原始 {len(wt):>7,} 字符 → 清洗后 {len(tx):>6,} 字符  "
              f"（保留 {len(tx)/len(wt)*100:.1f}%）")
        print(f"  >60 字符的段落数: {len(paras)}")
        print(f"  --- 正文前 700 字 ---")
        print("  " + tx[:700].replace("\n", "\n  "))
        time.sleep(0.5)

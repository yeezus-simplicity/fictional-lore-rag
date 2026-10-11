r"""jojowiki wikitext → 纯文本（B 方案：扩语料）

== 为什么需要这个模块 ==
现有 2407 块语料是从 `Stand_Stats` 主表抽的**结构化字段**，
元数据齐全（stand_name/section 100% 覆盖）→ 向量检索发挥不出作用
（实测：recall@10 纯 BM25 0.8207 反高于混合 0.7826）。

要验证「向量的价值 = f(语料非结构化程度)」，必须引入**非结构化长文本**。
jojowiki 的角色页/替身页正是这类：Jotaro 页 79,845 字符、DIO 页 125,941 字符。

== 两个实测踩坑（都影响检索质量）==
1. **模板不能一律删**：jojowiki 用 `{{Nihongo|'''Star Platinum'''|…}}`
   承载**主语**。整块删掉 → 正文变成 "is the Stand of Jotaro Kujo…"，
   缺主语的句子 BM25 再也匹配不到替身名。
   → 内容型模板（Nihongo/ruby）取第一参数，其余才删。
2. **列表规则与标题规则有先后**：若先转标题（生成 `## X`），
   后处理列表符（`^\s*[*#;:]+`）会把 `##` 当 # 号列表吃掉，
   于是 `==Appearance==` 变成 `- Appearance`。必须先列表、后标题。

== 用法 ==
    from wash import wash
    text = wash(wikitext)
"""
from __future__ import annotations

import re

# ★ 内容型模板：整块删会丢正文，改为取第一参数
_KEEP_FIRST_ARG = {"nihongo", "nihongo2", "ruby", "ruby2", "lang"}


def _process_templates(s: str) -> str:
    """逐个处理顶层模板：内容型取第一参数，其余整块删除。

    ★ 用深度计数而非正则 —— 模板可嵌套（{{A|{{B}}}}），
      正则匹配不了配对括号。
    """
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
    out: list[str] = []
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
    """wikitext → 纯文本。保留 `## 标题` 作为节标记（切块要用）。"""
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
    # 5. 模板：内容型取第一参数（保住主语），其余删除
    s = _process_templates(s)
    # 6. 表格 {| … |}
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
    # 11. ★ 必须先列表、后标题（否则 `## X` 被 # 号列表规则吃掉）
    s = re.sub(r"^\s*[*;:]+[ \t]*", "- ", s, flags=re.M)
    # 12. 标题 → `## X`（★ jojowiki 格式是 `==Appearance==`，等号间无空格）
    s = re.sub(r"^\s*={2,6}\s*(.+?)\s*={2,6}\s*$", r"\n## \1\n",
               s, flags=re.M)
    # 13. 空段压缩
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


# ------------------------------------------------------------------
# 切块：按 `## 标题` 分节，再做长度聚合
# ------------------------------------------------------------------
def split_sections(text: str, min_len: int = 200,
                   max_len: int = 900) -> list[dict]:
    """把 wash 后的文本按节切块。

    ★ 与现有语料的一致性：现有块中位 229 字符、p95 671。
      这里沿用相近区间，避免两批语料粒度差异过大导致检索行为不可比。
    ★ 保留节标题作为块前缀 —— 否则「Appearance」这类小节的内容
      脱离标题后语义模糊（问「外观」时匹配不到）。
    """
    # 先按 ## 切
    parts = re.split(r"\n## ", "\n" + text)
    chunks: list[dict] = []
    for p in parts:
        lines = p.split("\n", 1)
        if len(lines) == 2 and not p.startswith("\n"):
            title, body = lines[0].strip(), lines[1].strip()
            title = re.sub(r"={2,6}\s*$", "", title).strip()
        else:
            title, body = "", p.strip()
        if not body:
            continue
        # 段落聚合到 max_len
        paras = [x.strip() for x in body.split("\n\n") if x.strip()]
        cur = ""
        for pa in paras:
            if cur and len(cur) + len(pa) + 2 > max_len:
                chunks.append({"section": title, "content": cur.strip()})
                cur = pa
            else:
                cur = f"{cur}\n\n{pa}".strip()
        if cur:
            chunks.append({"section": title, "content": cur.strip()})

    # ★ 过短的块（< min_len）合并进前一块 —— 避免产生碎片噪声
    merged: list[dict] = []
    for c in chunks:
        if (merged and len(c["content"]) < min_len
                and len(merged[-1]["content"]) + len(c["content"]) < max_len):
            merged[-1]["content"] += "\n\n" + c["content"]
        else:
            merged.append(c)
    # 前缀节标题（除非是 lead 段）
    for c in merged:
        if c["section"]:
            c["content"] = f"{c['section']}\n{c['content']}"
    return merged

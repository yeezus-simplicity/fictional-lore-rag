"""修复 M1 遗留的「多值字段粘连」—— 不重抓，用已抓的 wikitext 语料就地恢复（M67）。

== 问题（根因）==
M1 的详情页解析走 HTML，用 `sib.text_content()` 取 Infobox 值，而
**lxml 的 `text_content()` 会丢掉 `<br>` 且不补任何分隔符**：

```
<div>Benjamin Boomboom (1)<br/>Andre Boomboom (2)<br/>L.A. Boomboom (3)</div>
   →  "Benjamin Boomboom (1)Andre Boomboom (2)L.A. Boomboom (3)"
```

于是下游 `_char_id` 把**多个人粘成一个角色**：

| 后果 | 实例 |
|---|---|
| **10 个角色查不到**（id 长到走哈希兜底）| `Andre BoomboomL.A. Boomboom`、`Caravan SeraiChakaFour Unnamed miceKhan…`、`Whitesnake's Unknown Victim Thunder McQueen` |
| 角色中文名对跟着粘 | `zh_char_pairs.json` 里一个 id 挂**两个名字** |
| 13 个"角色页"抓不到 | 请求的名字就是这种粘连串 → 404（M65 记过）|
| 替身类型被粘 | `AutomaticMaterializedPsychological AssaultArtificial Non-Humanoid…` |

**根因已在 `dataset/pipeline/scrape.py` 修掉**（`_text_with_breaks`：`<br>` → `, `）。
本脚本负责修复**已经落盘的数据**。

== 为什么不用重抓 ==
维基文本语料（`dataset/sources/jojowiki/articles.json`）里**分隔符是完好的**：
`|user = [[Caravan Serai]] {{S|Originally}}<br />[[Chaka]] {{S|Possessed}}…`

== ★ 只修 owner_name，**stand_type 不动**（这条是刻意的）★ ==
类型串的每一段来自 `{{ST|缩写}}`，**模板带缩写展开表**（`Auto` → `Automatic`），
而模板正文**不在**我们抓下来的数据里 → 只能从结果反推。实测反推**不可靠**：

```
· 某个替身只出现一次 `Bound`，反推出 "Materialized" —— 明显不对
· `PH` 的候选值里 "Phenomenon" 与 "Physical Humanoid" **都能自洽**（无法区分）
```

按"宁可少修，不引入新错"，类型串**保持原样**并在报告里说明。
★ 它**没有用户可见损害**：界面的中文类型是 `_clean_stand_type` 按**关键词**匹配的
（"Close-Range" 在粘连串里照样命中 → 显示"近距离型"），所以只是底层值不干净。

== ★ owner_name 的校验门（宁可漏修，不引入新错）★ ==
把 wikitext 渲染出的各段拼成 `, ` 分隔的串后：
  · 指纹**逐字相同** → 正常修复
  · 旧值是新候选的**子串** → 源站比我们抓到的多一段（实测 tomb_of_the_boom）
    → 也修，但**单独计数上报**
  · 其余 → **保留旧值并列出**

用法：
    python dataset/pipeline/repair_owner_split.py            # 预览，不写
    python dataset/pipeline/repair_owner_split.py --write     # 写回
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROC = ROOT / "dataset" / "processed"
SRC = ROOT / "dataset" / "sources" / "jojowiki"
STANDS = PROC / "stands.json"
ARTICLES = SRC / "articles.json"
ZH_PAIRS = ROOT / "dataset" / "sources" / "zh_names" / "zh_char_pairs.json"

SEP = ", "
_REF = re.compile(r"<ref[^>]*/>|<ref[^>]*>.*?</ref>", re.I | re.S)


def field(wt: str, key: str) -> str | None:
    """取 wikitext Infobox 的单行字段（空白类**不含换行** —— M65 坑 5）。"""
    m = re.search(r"^[^\S\n]*\|[^\S\n]*%s[^\S\n]*=[^\S\n]*(.+)$" % re.escape(key),
                  wt, re.M)
    return m.group(1).strip() if m else None


def render_part(part: str) -> str:
    """把 wikitext 的一个值段渲染成**页面上看到的样子**（与旧值同口径）。

    `[[A|B]]` → B ；`[[A]]` → A ；`{{S|X}}` → `(X)` ；其余模板去掉。
    """
    t = _REF.sub("", part)
    t = re.sub(r"\{\{\s*S\s*\|\s*([^}]*)\}\}", r"(\1)", t, flags=re.I)
    t = re.sub(r"\{\{[^{}]*\}\}", "", t)
    t = re.sub(r"\[\[([^\]|]*)\|([^\]]*)\]\]", r"\2", t)
    t = re.sub(r"\[\[([^\]]*)\]\]", r"\1", t)
    t = t.replace("'''", "").replace("''", "")
    t = re.sub(r"<[^>]+>", "", t)
    return re.sub(r"\s+", " ", t).strip()


def split_parts(raw: str | None) -> list[str]:
    """按 `<br>` 拆段（源站表示"多个值"的方式）。"""
    if not raw:
        return []
    return [p for p in (x.strip() for x in re.split(r"<br\s*/?>", raw, flags=re.I))
            if p]


def sig(s: str) -> str:
    """比较用的"指纹"：去掉空白**与分隔符逗号**。

    ★ 为什么要把逗号也去掉（我第一版没去，15 个全被判"校验失败"）：
      新值里是我们插进去的 `, `，旧值没有 —— 两者当然不逐字相同。
      要去掉的是**我们引入的东西**，留下的才是真差异。
      ★ 名字里本来就有的逗号两边都有，会自然抵消。
    """
    return re.sub(r"\s+", "", s).replace(",", "")


def glued_owner(s: dict, page: dict | None) -> bool:
    """是否属于"粘连"。

    ★★ 判据是**两选一**，不能只看正则 ★★
      我第一版只用 `[a-z)][A-Z]`（相邻的大小写边界），漏掉了
      `…(Original user) Sports Maxx…` 这种「**右括号 + 空格 + 大写**」的写法
      —— 实测 3 个替身（limp_bizkit / marilyn_manson / planet_waves）因此没被修。
      正确做法：**以源站为准** —— wikitext 的 `user` 有多个 `<br>` 段，
      而落盘值里没有分隔符 ⇒ 就是粘连。
    """
    raw = (s.get("owner_name") or "").strip()
    if not raw or SEP in raw:
        return False
    # ★ 已经带分隔符的（源站/下游可能用了 ` / `）就不算粘连 ——
    #   实测 milagroman 的值本来就用 ` / ` 分好，不该动它。
    if re.search(r"\s/\s|\s·\s|\s\|\s", raw):
        return False
    if page and len(split_parts(field(page["wikitext"], "user"))) > 1:
        return True
    return bool(re.search(r"[a-z)][A-Z]", raw))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="写回 stands.json")
    args = ap.parse_args()

    stands = json.loads(STANDS.read_text(encoding="utf-8"))
    pages = {p["title"]: p for p in
             json.loads(ARTICLES.read_text(encoding="utf-8"))["pages"]
             if p.get("kind") == "stand"}
    print(f"替身 {len(stands)} 个 · 语料替身页 {len(pages)} 个")

    stat = collections.Counter()
    fix: list[tuple[str, str, str]] = []
    remap_src: list[tuple[str, str]] = []      # (旧 owner_name, 新 owner_name)
    skip: list[tuple[str, str]] = []
    glued_types: list[tuple[str, str]] = []

    for s in stands:
        st = (s.get("stand_type") or "").strip()
        if st and SEP not in st and re.search(r"[a-z][A-Z]", st):
            glued_types.append((s["stand_id"], st))
        page = pages.get(s.get("name_en") or "")
        if not glued_owner(s, page):
            continue
        raw = (s.get("owner_name") or "").strip()
        stat["owner_glued"] += 1
        if not page:
            skip.append((s["stand_id"], raw))
            stat["owner_no_page"] += 1
            continue
        parts = [render_part(p) for p in
                 split_parts(field(page["wikitext"], "user"))]
        parts = [p for p in parts if p]
        cand = SEP.join(parts)
        if parts and sig(cand) == sig(raw):
            fix.append((s["stand_id"], raw, cand))
            remap_src.append((raw, cand))
            stat["owner_fixed"] += 1
        elif parts and sig(raw) in sig(cand):
            # ★ 源站比我们抓到的**多一段**（实测 tomb_of_the_boom_1_2_3：
            #   wikitext 有「Boomboom Family (Collectively)」而旧值没有）
            #   → 旧值整体是新候选的**子串** ⇒ 仍是同一串的粘连版，可以修，
            #     但**单独计数上报**，不混进"逐字指纹相同"那一类。
            fix.append((s["stand_id"], raw, cand))
            remap_src.append((raw, cand))
            stat["owner_fixed"] += 1
            stat["owner_superset"] += 1
        else:
            skip.append((s["stand_id"], raw))
            stat["owner_verify_fail"] += 1

    print("\n" + "=" * 66)
    print("owner_name（★ 只修这一类）")
    print("=" * 66)
    print(f"  粘连 {stat['owner_glued']} 个 → 可修复 {stat['owner_fixed']}"
          f"（其中源站多一段 {stat['owner_superset']}）· "
          f"校验失败 {stat['owner_verify_fail']} · 无语料页 {stat['owner_no_page']}")
    for sid, old, new in fix:
        print(f"    ✓ {sid}")
        print(f"        旧: {old}")
        print(f"        新: {new}")
    for sid, old in skip:
        print(f"    ⚠ 保留旧值 {sid}: {old[:90]}")

    print("\n" + "=" * 66)
    print("stand_type（★ 刻意不动 —— 见文件头说明）")
    print("=" * 66)
    print(f"  粘连 {len(glued_types)} 个，**保持原样**")
    print("  原因：`{{ST|缩写}}` 的展开表不在已抓数据里，反推会产生错值")
    print("        （实测 `Bound` 被反推成 'Materialized'；`PH` 有多个自洽候选）")
    print("  影响：**无用户可见损害** —— 界面的中文类型是按关键词匹配的")
    for sid, st in glued_types[:3]:
        print(f"    · {sid}: {st[:70]}")

    if not args.write:
        print("\n（预览模式，未写入。加 --write 生效）")
        return 0

    fixmap = {sid: new for sid, _, new in fix}
    for s in stands:
        if s["stand_id"] in fixmap:
            s["owner_name"] = fixmap[s["stand_id"]]
    STANDS.write_text(json.dumps(stands, ensure_ascii=False, indent=1),
                      encoding="utf-8")
    print(f"\n✓ 已写回 {STANDS.relative_to(ROOT)}（owner {len(fixmap)} 条）")

    # ---- 角色中文名迁移 ----
    #   `zh_char_pairs.json` 的键是**入库时的 character_id**；粘连 id 上挂的名字
    #   要搬到拆分后的 character_id 上，否则中文名跟着丢。
    #   ★★ 用**人工确认表**，不做自动定位 ★★
    #     实测自动按位置对齐会错配：`whitesnake_s_unknown_victim_thunder_mcqueen`
    #     的旧值是 `['桑达·麦奎恩','白蛇']` —— 「白蛇」是**替身名**、不属角色，
    #     按位置对齐就把「白蛇」挂到了 `thunder_mcqueen` 上。
    #     → 与 M41/M65 同一取舍：**宁可少给一个中文名，不给错的中文名**。
    if ZH_PAIRS.exists():
        zh = json.loads(ZH_PAIRS.read_text(encoding="utf-8"))
        man_p = ZH_PAIRS.parent / "zh_char_remap_manual.json"
        n_move = 0
        if man_p.exists():
            man = json.loads(man_p.read_text(encoding="utf-8"))
            for new_id, rec in (man.get("entries") or {}).items():
                name = rec.get("zh")
                if not name:
                    continue
                zh.setdefault(new_id, [])
                if name not in zh[new_id]:
                    zh[new_id].append(name)
                    n_move += 1
            ZH_PAIRS.write_text(json.dumps(zh, ensure_ascii=False, indent=1),
                                encoding="utf-8")
            print(f"✓ 角色中文名迁移：按人工确认表写入 {n_move} 个"
                  f"（表里另有 {len(man.get('_skipped') or {})} 条**刻意不给**，"
                  f"理由见 {man_p.name}）")
        print("  ★ 未做自动对齐 —— 旧键**保留**（它们只在旧报告/旧链接里出现，"
              "删了反而对不上）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

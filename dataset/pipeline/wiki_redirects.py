"""jojowiki 的**重定向 → 规范页名**映射（M72）。

== 为什么需要 ==
源站有 12 个页面是重定向（指向另一个正文页）：

    DIO                        → Dio Brando
    Anjuro "Angelo" Katagiri   → Anjuro Katagiri
    Shigekiyo "Shigechi" Yangu → Shigekiyo Yangu
    Former Tama                → Tama
    Elder A. Phex Brother      → A. Phex Brothers
    Younger A. Phex Brother    → A. Phex Brothers
    Holy Joestar               → Holy Kujo
    Star Platinum: The World   → Star Platinum（段落锚点）
    Tusk ACT1/ACT2/ACT3/ACT4   → Tusk

而 `fetch_wikitext()` 用 `redirects=1`，所以**抓重定向名会得到正文页的副本**
（内容一模一样）。两处因此出问题：

  ① 抓取层：`Category:Part N Characters` 同时列了重定向和它的目标
     → 同一个角色被抓成两条（`characters` 表里 `dio` 和 `dio_brando` 并存）。

  ② 入库层：替身页的 `owner_name` 用的是**重定向名**
     （The World 的持有者在源站写作 "DIO"）
     → `owner_map` 用 `_char_id("DIO")` 造出一个 `dio` 行，
       而分类名单那边是 `dio_brando` —— 又是两行。

★ 判定方法（唯一可靠的一种）：
    `action=query&prop=revisions&rvprop=content` 拿**未经重定向解析**的原文，
    以 `#REDIRECT` 开头即为重定向，`[[...]]` 里就是目标页。
    ★ 不要用 `action=parse` 判：它对 `redirects` 参数的行为不一致 ——
      实测同一个 `Holy Joestar`，带 `redirects=0` 返回 title=Holy Kujo，
      不带又返回 title=Holy Joestar。同一接口两次调用给出不同答案，
      拿它当判据会得出自相矛盾的结果。
    ★ 也不要查 `pageprops.wikibase_item`：本 wiki 实测**全为 None**
      （没有接 Wikidata），区分不了任何页面。

★ 为什么是手工表而不是运行时自动解析：
  自动判需要在每次抓取前多打一遍 API（12+ 个标题），
  而这个集合**极少变动**（源站结构稳定）。
  手工表 + 测试锁死，比"每次现算"更可控，
  也与本项目 M65/M70「人工确认表 + 注明依据」的一贯做法一致。
  → 若将来发现新的重定向，往表里加一行并补测试即可。
"""
from __future__ import annotations

# 重定向页名 → 规范页名（正文页）
REDIRECT_TO_CANONICAL: dict[str, str] = {
    # ---- 角色侧（来自 Category:Part N Characters）----
    "DIO": "Dio Brando",
    'Anjuro "Angelo" Katagiri': "Anjuro Katagiri",
    'Shigekiyo "Shigechi" Yangu': "Shigekiyo Yangu",
    "Former Tama": "Tama",
    "Elder A. Phex Brother": "A. Phex Brothers",
    "Younger A. Phex Brother": "A. Phex Brothers",
    "Holy Joestar": "Holy Kujo",
    # ---- 替身侧（来自 stands.json 的形态组，M1 遗留问题）----
    #   这 5 个指向的是**段落锚点**或父替身，不是独立正文页。
    #   替身本身仍按 M1 的形态组模型登记（那是另一个问题），
    #   但不该抓 5 份一模一样的页面。
    "Star Platinum: The World": "Star Platinum",
    "Tusk ACT1": "Tusk",
    "Tusk ACT2": "Tusk",
    "Tusk ACT3": "Tusk",
    "Tusk ACT4": "Tusk",
}

# 反向索引：规范页名 → 所有指向它的重定向名
CANONICAL_TO_REDIRECTS: dict[str, list[str]] = {}
for _r, _c in REDIRECT_TO_CANONICAL.items():
    CANONICAL_TO_REDIRECTS.setdefault(_c, []).append(_r)


def canonical_name(title: str) -> str:
    """把重定向名规范化成正文页名；不是重定向则原样返回。"""
    t = (title or "").strip()
    return REDIRECT_TO_CANONICAL.get(t, t)


def is_redirect(title: str) -> bool:
    return (title or "").strip() in REDIRECT_TO_CANONICAL


def redirect_targets() -> set[str]:
    """全部重定向页名（供抓取名单排除用）。"""
    return set(REDIRECT_TO_CANONICAL)

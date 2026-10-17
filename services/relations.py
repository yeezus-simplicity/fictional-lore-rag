"""亲属关系问答（M77）：「普奇的弟弟是谁」。

★★ 为什么单独做 ★★
  用户实测：「问普奇的弟弟是谁，它只给出普奇的档案」。
  数据其实是**有的** —— 源站 Infobox 的 `family` 字段本来就把关系写在括号里：
      Enrico Pucci → ['Domenico Pucci (Twin Brother)', 'Perla Pucci (Younger Sister)']
  只是 characters.py 里没有任何一处读它（`families` 只在档案卡里当字符串列表展示），
  于是问句被当成普通的「普奇 是谁」→ 返回档案。

★ 数据口径（与项目其它模块一致）：
  ① **只读源站标注**，不推理。源站写 `Brother` 就只说「兄弟」，**不猜长幼**。
  ② 问「弟弟」而源站只写 `Brother` / `Twin Brother` → 仍返回，
     但明确标注「源站未标注长幼」，不假装确定。
  ③ 亲属名可能是**源站没单独建条目的角色**（如 Domenico Pucci 在本库里没有
     独立角色页）→ 照实给名字，并说明「本库无该角色条目」，不编造档案。
"""
from __future__ import annotations

import re
from typing import Optional

# 关系词（中文）→ 源站 role 里应出现的关键词（按优先级）
ROLE_CN = {
    "弟弟": ("弟弟", ["younger brother", "little brother", "twin brother", "brother"]),
    "哥哥": ("哥哥", ["older brother", "big brother", "elder brother", "twin brother", "brother"]),
    "兄弟": ("兄弟", ["brother"]),
    "姐姐": ("姐姐", ["older sister", "big sister", "elder sister", "twin sister", "sister"]),
    "妹妹": ("妹妹", ["younger sister", "little sister", "twin sister", "sister"]),
    "姐妹": ("姐妹", ["sister"]),
    "双胞胎": ("双胞胎", ["twin"]),
    "父亲": ("父亲", ["father"]),
    "爸爸": ("父亲", ["father"]),
    "母亲": ("母亲", ["mother"]),
    "妈妈": ("母亲", ["mother"]),
    "儿子": ("儿子", ["son"]),
    "女儿": ("女儿", ["daughter"]),
    "妻子": ("妻子", ["wife"]),
    "老婆": ("妻子", ["wife"]),
    "丈夫": ("丈夫", ["husband"]),
    "老公": ("丈夫", ["husband"]),
    "祖父": ("祖父", ["grandfather"]),
    "爷爷": ("祖父", ["grandfather"]),
    "祖母": ("祖母", ["grandmother"]),
    "奶奶": ("祖母", ["grandmother"]),
    "孙子": ("孙子", ["grandson"]),
    "孙女": ("孙女", ["granddaughter"]),
    "叔叔": ("叔伯", ["uncle"]),
    "舅舅": ("叔伯", ["uncle"]),
    "伯父": ("叔伯", ["uncle"]),
    "阿姨": ("姑姨", ["aunt"]),
    "姑姑": ("姑姨", ["aunt"]),
    "婶婶": ("姑姨", ["aunt"]),
    "岳父": ("岳父", ["father-in-law"]),
    "岳母": ("岳母", ["mother-in-law"]),
    "亲属": ("亲属", []),
    "家人": ("亲属", []),
    "亲戚": ("亲属", []),
}

# 问句形状：关系词 + 疑问收尾（避免把「普奇的哥哥在漫画第几话出场」误判成亲属问句）
REL_ASK = re.compile(
    r"(" + "|".join(sorted(ROLE_CN, key=len, reverse=True)) + r")"
    r"\s*(?:是|叫|有)?\s*(谁|什么名字|叫什么|有哪些|都是谁|都有谁)")

_FAMILY_RE = re.compile(r"^(.*?)\s*\(([^)]+)\)\s*$")


def parse_families(families: Optional[list[str]]) -> list[tuple[str, Optional[str]]]:
    """['Domenico Pucci (Twin Brother)'] → [('Domenico Pucci', 'Twin Brother')]。

    没有括号的（源站只给名字，如 DIO 的 `Giorno Giovanna`）→ role=None。
    """
    out: list[tuple[str, Optional[str]]] = []
    for f in families or []:
        s = (f or "").strip()
        if not s:
            continue
        # 源站偶尔一行写多个（用 <br> 或换行残留）
        for part in re.split(r"\s*(?:<br\s*/?>|\n)\s*", s):
            part = part.strip()
            if not part:
                continue
            m = _FAMILY_RE.match(part)
            if m:
                out.append((m.group(1).strip(), m.group(2).strip()))
            else:
                out.append((part, None))
    return out


def detect(q: str) -> Optional[str]:
    """问句里问的是哪种关系 → 中文关系词（已归并，如 爸爸→父亲）。"""
    m = REL_ASK.search(q or "")
    if not m:
        return None
    return ROLE_CN[m.group(1)][0]


def relatives_of(card: dict, role_cn: str) -> dict:
    """按关系筛选亲属 → 答案字典（`character_relatives`）。

    ★ 三层结果都要能解释清楚：
      ① 源站明确标了该关系 → 直接给
      ② 源站只标了更宽的关系（问弟弟，只有 Brother / Twin Brother）
         → 也给，但标注「未标注长幼」
      ③ 源站没有该关系 → 给**已有亲属全表**并说明，不退化成档案
    """
    relatives = parse_families(card.get("families"))
    role_label, keys = ROLE_CN[role_cn]

    # ★★ 关系要拆成「基础关系 + 长幼」两维 ★★
    #   踩过的坑：问「普奇的姐姐」，源站写的是 `Younger Sister` —— 旧的实现
    #   用 `sister` 这个宽关键词命中了，还补一句「源站未标注长幼」。
    #   **源站明明标了 Younger**，是姐姐不是妹妹 —— 既答错又给了错的理由。
    #   → 长幼与基础关系分开判：年龄不符的进 `contrast`（说明「是妹妹，不是姐姐」），
    #     只写 `Brother`/`Twin Brother` 的才进 `loose`（确实没标长幼）。
    want_base, want_age = _split_role(role_cn)
    exact: list[dict] = []
    loose: list[dict] = []
    contrast: list[dict] = []

    for name, role in relatives:
        if not role:
            continue
        base_en, age = _split_en(role)
        if not base_en or not any(k in role.lower() for k in keys):
            continue
        item = {"name": name, "role": role}
        if want_base and base_en != want_base:
            continue
        # ★ 「双胞胎」是**并列**，不含长幼信息 —— 不能当成「长幼不符」
        #   （问弟弟时 Twin Brother 应算作「未标长幼」，而不是「这是哥哥」）
        eff_age = None if age == "twin" else age
        if want_age == "twin":
            if "twin" not in role.lower():
                continue
            eff_age = None
        if want_age and eff_age and eff_age != want_age:
            # 问哥哥却给了弟弟 → 明确说「不是这个称谓」，不混进结果
            contrast.append(item)
            continue
        if want_age and not eff_age:
            loose.append(item)          # 确实没标长幼（Brother / Twin Brother）
        else:
            exact.append(item)

    base = {
        "type": "character_relatives",
        "character_id": card.get("character_id"),
        "character": card.get("name_zh") or card.get("name_en"),
        "relation": role_label,
        "items": exact + loose,
        "contrast": contrast,
        "strict": bool(exact) and not loose,
        "matched": bool(exact or loose),
    }

    if exact or loose:
        notes = []
        if loose:
            got = "、".join(f"{i['name']}（{i['role']}）" for i in loose)
            notes.append(f"源站对 {got} 只标了「{'/'.join(sorted({i['role'] for i in loose}))}」，"
                         "**未标注长幼** —— 这里按你的问法一并列出，"
                         "不替你判定谁是兄谁是弟。")
        if contrast:
            got = "、".join(f"{i['name']}（{i['role']}）" for i in contrast)
            notes.append(f"另有 {got} —— 源站标注的长幼与你问的相反。")
        base["note"] = "".join(notes) or (
            f"亲属关系来自源站 Infobox 的 family 字段（{len(relatives)} 条）。")
        return base

    # ③ 没有该关系 → 不退化成档案：说明「源站没标」，并列出**已有的**亲属
    known = [{"name": n, "role": r} for n, r in relatives]
    base["items"] = []
    base["contrast"] = contrast
    base["matched"] = False
    parts = [f"源站没有标注「{role_label}」。"]
    if contrast:
        got = "、".join(f"{i['name']}（{i['role']}）" for i in contrast)
        parts.append(f"该角色条目里与之最接近的是 {got}。")
    if known:
        desc = "、".join(f"{i['name']}（{i['role']}）" if i["role"] else i["name"]
                         for i in known)
        parts.append(f"该角色条目共标了 {len(known)} 个亲属：{desc}。")
    base["note"] = "".join(parts)
    return base


# 中文称谓 → (基础关系, 长幼)
_ROLE_SPEC = {
    "弟弟": ("brother", "younger"),
    "哥哥": ("brother", "older"),
    "兄弟": ("brother", None),
    "姐姐": ("sister", "older"),
    "妹妹": ("sister", "younger"),
    "姐妹": ("sister", None),
    "双胞胎": (None, "twin"),
    "父亲": ("father", None),
    "母亲": ("mother", None),
    "儿子": ("son", None),
    "女儿": ("daughter", None),
    "妻子": ("wife", None),
    "丈夫": ("husband", None),
    "祖父": ("grandfather", None),
    "祖母": ("grandmother", None),
    "孙子": ("grandson", None),
    "孙女": ("granddaughter", None),
    "叔伯": ("uncle", None),
    "姑姨": ("aunt", None),
    "岳父": ("father-in-law", None),
    "岳母": ("mother-in-law", None),
    "亲属": (None, None),
}

_BASE_EN = ("brother", "sister", "father", "mother", "son", "daughter",
            "wife", "husband", "grandfather", "grandmother", "grandson",
            "granddaughter", "uncle", "aunt", "cousin")


def _split_role(role_cn: str) -> tuple[Optional[str], Optional[str]]:
    return _ROLE_SPEC.get(role_cn, (None, None))


def _split_en(role: str) -> tuple[Optional[str], Optional[str]]:
    """'Twin Brother' → ('brother', 'twin')；'Younger Sister' → ('sister','younger')。"""
    rl = (role or "").lower()
    base = next((b for b in _BASE_EN if b in rl), None)
    # 'twin' 只对兄弟/姐妹有意义；'younger/older' 用于判长幼
    if re.search(r"\b(younger|little)\b", rl):
        age = "younger"
    elif re.search(r"\b(older|big|elder)\b", rl):
        age = "older"
    elif "twin" in rl:
        age = "twin"
    else:
        age = None
    return base, age

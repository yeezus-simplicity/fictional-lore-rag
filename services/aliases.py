"""
替身别名解析（M14）。

==★ 为什么要这个 ==

实测问题：用户问「黄金体验的能力」，返回的是**按篇章分组的统计表**
（Stardust Crusaders 33 个替身 / Diamond is Unbreakable 29 个…），
完全答非所问。

而问「Gold Experience 的能力是什么」→ 正常返回它的原文描述。

**根因**：`stands.json` 里其实**有中文名**，但藏在 `name_ja` 的括号里：
    Gold Experience → name_ja = 'ゴールド・エクスペリエンス(黄金体験)'
→ 而 `name2id` / `known_entities` / `_guess_stand()`
  **三处全都只收录 `name_en`（英文名）** → 中文名匹配不到。

★ 为什么不能直接用 `name_ja` 括号里的原文：
1. 只有 32/154 个替身有中文别名（154-32=122 个用户问中文只会得到乱码）
2. 那32 个里写的是**日文汉字**「黄金体験」，用户输入**简体**「黄金体验」
   （験 vs 验）→ 字符串直接比也匹配不上

==★ 本模块的策略 ==

1. **能提取中文别名就提取**（括号内含汉字的部分）
2. **繁简通配**：`験→验`、`體→体`、`經→经` 等常用差异
   —— 这是最小必要集，不是完整 OpenCC
3. **同时保留原文里的日文名**（如 `ゴールド・エクスペリエンス`）
   —— 有些用户会直接粘日文原文
4. **★ 不猜测没被收录的中文名**：
   122 个没有官方中文名的替身，与其猜错，不如让用户用英文名。
   但可以给出**明确的提示**（见 api 层unknown 替身的处理）

==★ 一个更重要的判断 ==

即使补全了别名，**「匹配不到 → 返回篇章统计表」这个兜底本身也是错的**
（见 services/executor.py: detect_intent）。它会让任何识别失败的问题
都得到一个「看起来合理但完全无关」的答案。
→ 那部分修复在 executor.py，本模块只管别名解析。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
PROC = ROOT / "dataset" / "processed"


# ============================================================
# 繁简差异（最小必要集）
# ============================================================
# ★★★ 这里必须用**码点**（\uXXXX）写，不能直接写字面量。
#
#   实测踩坑：直接写 "体験"/"體" 进源码时，
#   编辑器/写入工具会做**字体规范化**（NFKC），
#   把它们悄悄变成简体 "体验"/"体"
#   → 键和值变成同一个字 → 映射恒为空 → 繁简通配"完全无效"但**不报错**。
#
#   症状：simplify_zh("黄金体験") 返回 "黄金體験"（原样不动）
#   码点写法不受规范化影响，可靠。

#★ 日文/繁体 → 简体。取最常被误用的几个：
#   験→验（黄金体験/黄金体验）
#   體→体龍→龙壓→压氣→气車→车门→门體→体
#   經→经驗→验髮→髮 裝→装 馬→马 鳥→鸟 魚→鱼 館→馆 鬥→斗
_TRAD2_SIMP: dict[str, str] = {
    "\u9a13": "\u9a8c",   # 験 → 验
    "\u9ad4": "\u4f53",   # 體 → 体
    "\u7d93": "\u7ecf",   # 經 → 经
    "\u9a57": "\u9a8c",   # 驗 → 验
    "\u58d3": "\u538b",   # 壓 → 压
    "\u6c17": "\u6c14",   # 気 → 气
    "\u9928": "\u9986",   # 館 → 馆
    "\u88c5": "\u88c5",   # 裝 → 装（简体同码点，仅列出说明）
    "\u9aee": "\u53d1",   # 髮 → 发
    "\u99ac": "\u9a6c",   # 馬 → 马
    "\u9ce5": "\u9e1f",   # 鳥 → 鸟
    "\u9b5a": "\u9c7c",   # 魚 → 鱼
    "\u8eca": "\u8f66",   # 車 → 车
    "\u9580": "\u95e8",   # 門 → 门
    "\u9b25": "\u6597",   # 鬥 → 斗
    "\u4f53": "\u4f53",   # 体 → 体（占位，避免反向表覆盖）
}

# 反向表：简体 → 日文/繁体原字（用于把用户输入对齐到数据里的形式）
_SIMP2_TRAD: dict[str, str] = {}
for _t, _s in _TRAD2_SIMP.items():
    if _s != _t:
        _SIMP2_TRAD.setdefault(_s, _t)


def simplify_zh(text: str) -> str:
    """把日文/繁体写法转成简体，便于用户输入匹配。

    ★ 逐字符映射，不做词组转换 —— 足够覆盖「黄金体験→黄金体验」。
    """
    #★★★ 查**_TRAD2_SIMP**（繁体→简体），不是 _SIMP2_TRAD（反向表）。
    #   实测踩坑：建了正向表却查了反向表 → 繁体字符永远查不到
    #   → 通配恒等失效。**因为别名表里两种写法都登记了，
    #   匹配用例照样全过** —— 故障点没被测试覆盖。
    return "".join(_TRAD2_SIMP.get(ch, ch) for ch in text)


def expand_variants(text: str) -> set[str]:
    """给一个名字生成所有可接受写法（原形 + 简繁变体）。"""
    out = {text}
    s = simplify_zh(text)
    if s != text:
        out.add(s)
    return out


# ============================================================
# 从 name_ja 提取中文别名
# ============================================================

# 括号（半角+全角）
_PAREN = re.compile(r"[（(]([^（()）]+)[）)]")
# 汉字（含日文汉字）
_CJK = re.compile(r"[一-鿿]")


def extract_zh_aliases(name_ja: Optional[str]) -> list[str]:
    """从 `name_ja` 里提取中文别名。

    例：
        'ゴールド・エクスペリエンス(黄金体験)' → ['黄金体験', '黄金体验']
        'デスサーティーン(死神13)'              → ['死神13']
        'アヌビス神'                            → []（没有括号内容）

    ★ 同时返回「原形」与「简体形」两种写法。
    """
    if not name_ja:
        return []
    out: list[str] = []
    for m in _PAREN.finditer(name_ja):
        inner = m.group(1).strip()
        # 必须含汉字才值得收（纯假名的括号不是中文名）
        if not inner or not _CJK.search(inner):
            continue
        out.extend(expand_variants(inner))
    # 去重且保序
    seen: set[str] = set()
    return [x for x in out if not (x in seen or seen.add(x))]


def extract_ja_name(name_ja: Optional[str]) -> Optional[str]:
    """提取 `name_ja` 的括号前部分（纯日文名）。

    有些用户会直接粘日文原文问，认出来能提升体验。
    """
    if not name_ja:
        return None
    head = _PAREN.split(name_ja)[0].strip()
    # 太长或与英文名相同的就不必收
    if not head or len(head) > 24:
        return None
    return head


# ============================================================
# 构建别名表
# ============================================================

# ★ M15：中文名词典（由 dataset/sources/zh_names/build_zh_map.py 生成）
ZH_MAP_PATH = PROC / "stand_name_zh.json"


def load_zh_map() -> dict[str, dict]:
    """载入中文名词典。文件缺失时返回空 dict（降级但不崩）。"""
    if not ZH_MAP_PATH.exists():
        return {}
    try:
        return json.loads(ZH_MAP_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def build_alias_table() -> tuple[dict[str, str], dict[str, list[str]]]:
    """构建 (别名 → stand_id) 与 (stand_id → 别名列表)。

    ★ 与 `name2id`（仅英文名）**并行使用**，不是替换。
       英文名仍要能查到 —— 中文名只是额外的入口。
    """
    p = PROC / "stands.json"
    if not p.exists():
        return {}, {}
    stands = json.loads(p.read_text(encoding="utf-8"))
    zh_map = load_zh_map()

    alias2id: dict[str, str] = {}
    id2alias: dict[str, list[str]] = {}

    for s in stands:
        sid = s.get("stand_id")
        if not sid:
            continue
        # ★ 先放英文名（主入口），再放日文名与中文别名
        names: list[str] = []
        for key in ("name_en", "name_raw"):
            v = s.get(key)
            if v and v not in names:
                names.append(v)
        ja_name = extract_ja_name(s.get("name_ja"))
        if ja_name and ja_name not in names:
            names.append(ja_name)
        names.extend(extract_zh_aliases(s.get("name_ja")))

        # ★★ M15：优先用中文名词典（M15 构建，覆盖 130 个替身）
        #   词典里的 name_zh 是简体且经过质量校验；
        #   括号里挖出来的可能是日文汉字（「黄金体験」）或单字（「力」）。
        zh_rec = zh_map.get(sid) or {}
        for zh in (zh_rec.get("name_zh_variants") or []):
            if zh and zh not in names:
                names.append(zh)
        if zh_rec.get("name_zh") and zh_rec["name_zh"] not in names:
            names.append(zh_rec["name_zh"])
        # ★ 被标记为不可匹配的（如含繁体字的「審判」）不进别名
        if zh_rec.get("_excluded_from_matching"):
            names = [n for n in names
                     if not (zh_rec.get("name_zh") == n
                             and zh_rec.get("_excluded_from_matching"))]

        id2alias[sid] = names
        for n in names:
            # ★★ 排除**单字中文别名**
            #   实测踩坑：Strength 的中文名是单字「力」，
            #   而「黄金体验的**能力**」里也有「力」
            #   → 单字别名会抢在「黄金体験」之前命中 → 返回 Strength 的数据。
            #   ★ 单字中文名信息量太低，误匹配率必然很高，一律不收。
            if len(n) <= 1 and _CJK.search(n):
                continue
            # 长名优先匹配（调用方按长度降序遍历，这里只负责登记）
            alias2id.setdefault(n, sid)
            # 简繁变体也登记（用户可能输入任一形式）
            for v in expand_variants(n):
                alias2id.setdefault(v, sid)

    return alias2id, id2alias


def match_stand(question: str, alias2id: dict[str, str]) -> Optional[str]:
    """在问句里找替身 stand_id（按别名匹配，长名优先）。

    ★ 与 `executor.extract_stand_name` 的区别：
       后者只认 `name2id`（英文名），这里认全量别名。
    """
    if not alias2id:
        return None
    low = question.lower()
    # 长名优先：否则「皇帝」可能抢在「皇帝之杖」前面命中
    for name in sorted(alias2id, key=len, reverse=True):
        if name and name.lower() in low:
            return alias2id[name]
    # 再试一次简繁归一（用户在中文名里混用了简繁）
    q2 = simplify_zh(question)
    if q2 != question:
        for name in sorted(alias2id, key=len, reverse=True):
            if name and simplify_zh(name).lower() in q2.lower():
                return alias2id[name]
    return None


# ============================================================
# 模糊候选（★ 只作提示，不作答案）
# ============================================================
# ★★ 设计依据（M15 实测结论，见 evaluation/eval_fuzzy_alias.py）：
#     阈值 2 → 正例命中 100%，但负例误匹配 11.8%（不可用）
#     阈值 3 → 负例误匹配 0%，但正例命中仅 44%
#     阈值 4 → 负例 0%，正例仅 16%
#
#   ★ 结论：任何阈值都达不到「既好用又安全」。
#     → 所以**不用它决定答案**，只在精确匹配失败时给候选提示：
#       「你是不是想问『黄金体验』？」
#     → 误匹配的危害从「答错」降为「多问一句」。
#
#   ★ M14 的教训是「单字名抢匹配」；这里进一步说明
#     **放宽匹配的风险靠调参解决不了，只能靠限制用途**。

# ★ 只对长度 ≥3 的中文名做候选（2 字太短，评测显示误匹配率 11.8%）
_FUZZY_MIN_LEN = 3


def _cjk_fragments(question: str, min_len: int = 2) -> list[str]:
    """从问句里切出连续中文片段（长度递减）。"""
    runs = re.findall(r"[一-鿿]+", question)
    frags: list[str] = []
    for run in runs:
        for size in range(len(run), min_len - 1, -1):
            for i in range(len(run) - size + 1):
                frags.append(run[i:i + size])
    # 长片段优先（更具体）
    return sorted(set(frags), key=len, reverse=True)


def suggest_stands(question: str,
                   id2alias: dict[str, list[str]],
                   limit: int = 3) -> list[dict]:
    """给出模糊候选（仅提示，**不决定答案**）。

    ★ 与 `match_stand` 的区别：
       match_stand 精确匹配 → 返回唯一结果，可以直接用
       suggest_stands 模糊 → 返回多个候选，**必须让用户确认**

    Returns:
        [{"stand_id", "name", "matched_by", "reason"}, ...]
        按匹配到的别名长度降序（越长越可信）
    """
    hits: dict[str, dict] = {}
    for sid, names in id2alias.items():
        for name in names:
            if len(name) < _FUZZY_MIN_LEN:
                continue
            if not re.search(r"[一-鿿]", name):
                continue
            # 情况 A：问句包含完整别名（如「黄金体验的能力」含「黄金体验」）
            if name in question:
                hits[sid] = {"stand_id": sid, "name": name,
                             "matched_by": "input_contains_name"}
                break
            # 情况 B：别名包含问句片段（如「黄金」⊂「黄金体验」）
            if len(question) >= _FUZZY_MIN_LEN:
                for frag in _cjk_fragments(question):
                    if len(frag) >= _FUZZY_MIN_LEN and frag in name:
                        hits.setdefault(sid, {
                            "stand_id": sid, "name": name,
                            "matched_by": f"fragment:{frag}"})
                        break
            if sid in hits:
                break
    # ★ 排序规则（实测踩坑后加的）：
    #   「黄金体验」既是独立替身，也是「黄金体验·镇魂歌」的一部分，
    #   两者长度不同 → 按长度降序会选中「黄金体验·镇魂歌」（更长），
    #   但用户输入「黄金体验的破坏力」时想问的是**前者**。
    #   → 优先「问句包含它」而非「它包含问句片段」，长度短者优先。
    out = sorted(
        hits.values(),
        key=lambda h: (h["matched_by"] != "input_contains_name",
                       len(h["name"])))[:limit]
    for h in out:
        h["reason"] = (f"未精确匹配到替身名，但问句里有「{h['name']}」"
                       f"（{h['matched_by']}）")
    return out


# ============================================================
# 自检
# ============================================================
if __name__ == "__main__":
    print("=" * 68)
    print("别名表自检")
    print("=" * 68)

    a2i, i2a = build_alias_table()
    stands = json.loads((PROC / "stands.json").read_text(encoding="utf-8"))
    print(f"\n替身数{len(stands)}  别名数 {len(a2i)}")

    #1) 英文名必须都在（不能因为加别名而丢掉主入口）
    missing_en = [s["name_en"] for s in stands
                  if s.get("name_en") and s["name_en"] not in a2i]
    print(f"  {'✓' if not missing_en else '✗'} 全部英文名都在别名表里"
          f"{'' if not missing_en else f' —— 缺 {len(missing_en)} 个'}")
    if missing_en:
        print(f"      {missing_en[:5]}")

    # 2) 覆盖率
    n_zh = sum(1 for v in i2a.values()
               if any(_CJK.search(x) for x in v))
    print(f"  ℹ 带中文名的替身 {n_zh} / {len(stands)}"
          f"（其余 {len(stands) - n_zh} 个用户需用英文名）")

    # 3) 简繁通配
    #★★ 必须用码点构造字符串。
    #   用字面量写「黄金体験」时，写入工具会做字体规范化，
    #   把「体」悄悄变成 U+4F53（简体）→ 测试变成「简体→简体」，
    #   恒等且无意义（实测踩过：写了 3 遍才发现是这个坑）。
    trad = "\u9ec4\u91d1\u4f53\u9a13"   # 黄金体験
    simp = "\u9ec4\u91d1\u4f53\u9a8c"   # 黄金体验
    got = simplify_zh(trad)
    ok3 = got == simp
    print(f"  {'✓' if ok3 else '✗'} 简繁通配：黄金体験 → {got}")
    if not ok3:
        print(f"      ★ 码点断言失败：得到 {got!r}，期望 {simp!r}")
        print(f"      实际码点：{[hex(ord(c)) for c in trad]}")
        print(f"      期望码点：{[hex(ord(c)) for c in simp]}")

    # 4) 实际匹配测试
    cases = [
        ("黄金体验的能力", "gold_experience"),
        ("Gold Experience 的能力是什么", "gold_experience"),
        ("黄金体验的破坏力是几级", "gold_experience"),
        ("ゴールド・エクスペリエンス 的能力", "gold_experience"),
        ("女教皇的能力", "high_priestess"),
        ("Star Platinum 的破坏力", "star_platinum"),
    ]
    print(f"\n  {'用例':34s} {'期望':18s} 实测")
    bad = 0
    for q, exp in cases:
        got = match_stand(q, a2i)
        ok = got == exp
        if not ok:
            bad += 1
        print(f"  {'OK ' if ok else 'XX '}{q:32s} {exp:18s} {got}")
    print(f"\n  失败 {bad}/{len(cases)}")

    # ---- 模糊候选（只作提示，不决定答案）----
    print("\n【模糊候选】只提示，不决定答案")
    fuzzy = [
        ("黄金体验的破坏力", "gold_experience"),
        ("黄金的能力", "gold_experience"),
        ("白金之星的形态", "star_platinum"),
    ]
    fbad = 0
    for q, exp in fuzzy:
        sug = suggest_stands(q, i2a)
        got = sug[0]["stand_id"] if sug else None
        ok = got == exp
        if not ok:
            fbad += 1
        via = sug[0]["matched_by"] if sug else "-"
        print(f"  {'OK ' if ok else 'XX '}{q:20s} → {got}  ({via})")
    print(f"  → 失败 {fbad}/{len(fuzzy)}")

    print("\n  ★ 负例（无关问句不应产生候选）:")
    nfalse = 0
    for q in ["破坏力是多少", "形态与外观", "今天天气怎么样"]:
        sug = suggest_stands(q, i2a)
        if sug:
            nfalse += 1
            print(f"    {q:16s} → 误报 {len(sug)} 个："
                  f"{[x['name'] for x in sug][:3]}")
        else:
            print(f"    {q:16s} → 无候选 ✓")
    print(f"  → 误报 {nfalse}/3")

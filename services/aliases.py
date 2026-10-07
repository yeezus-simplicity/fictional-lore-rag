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

def build_alias_table() -> tuple[dict[str, str], dict[str, list[str]]]:
    """构建 (别名 → stand_id) 与 (stand_id → 别名列表)。

    ★ 与 `name2id`（仅英文名）**并行使用**，不是替换。
       英文名仍要能查到 —— 中文名只是额外的入口。
    """
    p = PROC / "stands.json"
    if not p.exists():
        return {}, {}
    stands = json.loads(p.read_text(encoding="utf-8"))

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

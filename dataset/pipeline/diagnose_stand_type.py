"""诊断：把stands.stand_type 的粘连串按封闭词表切分（只读，不写库）。

用法：python dataset/pipeline/diagnose_stand_type.py
输出：可完全分解 / 有残余 两类计数 + 残余样例。
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROC = ROOT / "dataset" / "processed"

# 候选词表：起始版来自 api/main.py 的 _STAND_TYPE_CN，
# 这里补上语料实测出现、但词表里漏掉的词。
VOCAB = [
    "Range Irrelevant", "Close-Range", "Long-Range", "Long Distance",
    "Natural Non-Humanoid", "Natural Humanoid",
    "Artificial Non-Humanoid", "Artificial Humanoid",
    "Psychological Assault", "Physical Assault", "Materialized",
    "IntegratedPhenomenon (Other)",
    "Reconnaissance", "Integrated", "Materialized", "Multilayered",
    "Phenomenon", "Automatic", "Colony", "Bound", "Wearable",
    "Tool", "Spirit", "Psychic", "Sentient", "Multi-Unit",
    "Multi-User", "Non-Existent", "Shared", "Crystallization",
    "Animal", "Ability", "Ethereal", "Dimensional", "Resurrection",
    # ↓ 由本脚本跑出的「残余」反推补入（Foresight / Evolved）
    "Foresight", "Evolved",
]

NOTE_RE = re.compile(r"\([^)]*\)")


def split(raw: str) -> tuple[list[str], str]:
    """贪心最长匹配。返回 (命中词列表, 未识别残余)。"""
    t = NOTE_RE.sub(" ", str(raw))
    hits: list[str] = []
    i = 0
    while i < len(t):
        for w in sorted(VOCAB, key=len, reverse=True):
            if t.lower().startswith(w.lower(), i):
                hits.append(w)
                i += len(w)
                break
        else:
            i += 1
    used = "".join(hits).lower()
    rest = t.lower()
    for h in sorted(hits, key=len, reverse=True):
        rest = rest.replace(h.lower(), "", 1)
    return hits, rest.strip()


def main() -> int:
    data = json.load(open(PROC / "stands.json", encoding="utf-8"))
    items = data["stands"] if isinstance(data, dict) and "stands" in data else data
    ok, bad = 0, []
    tokens: Counter[str] = Counter()
    for s in items:
        raw = s.get("stand_type")
        if not raw:
            continue
        hits, rest = split(raw)
        tokens.update(hits)
        if rest:
            bad.append((s["name_en"], raw, hits, rest))
        else:
            ok += 1
    print(f"可完全分解 {ok} / 有残余 {len(bad)}")
    print("\n== 命中词频 ==")
    for w, n in tokens.most_common():
        print(f"  {n:4d}  {w}")
    print("\n== 有残余的样例 ==")
    for name, raw, hits, rest in bad[:40]:
        print(f"  {name}\n    raw   = {raw}\n    命中  = {hits}\n    残余  = {rest!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

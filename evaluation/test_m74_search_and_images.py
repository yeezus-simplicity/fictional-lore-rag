"""M74：搜索体验 + 角色/地点图片（用户实测反馈驱动）

== 用户原话 ==
  「搜索人物时不会返回人物的图片」
  「我搜索承太郎 迪亚哥 齐贝林都搜索不到」
  「搜英文好像必须分清大小写还是什么 根本没有模糊搜索 我搜 dio 也搜索不到」
  「地点要是有图片也加上吧」
  「要是有重名的角色 如齐贝林 乔斯达 东方仗助 就都返回就行了」

== 五条诉求 → 五个根因 ==
【1】头像缺失
    替身图片 M17 就有，角色/地点一直没有 —— 而它们的 infobox 里**本来就有图**
    （`{{Character Infobox | image = ...}}` / `{{Location Infobox | image}}`）。
    → `fetch_entity_images.py` 预抓 490 张（角色 359 / 地点 131），
      走 `/media/{kind}/{id}` 端点。

【2】「承太郎」搜不到
    `match()` 的方向是「**名字 ⊂ 问句**」（用户打全名才命中）。
    用户打简称时方向反了 —— **问句片段 ⊂ 名字**。
    → 加子串索引 + `fuzzy_hits()`（贪心取最长不重叠片段）。

【3】「齐贝林」搜不到
    M73 把**歧义简称**（姓氏）整条丢掉了 —— 理由是"收了必然答错"。
    但用户要的不是"答对一个"，而是"**都列出来**"。
    → 新增 `build_char_alias_index()`（多值、含歧义），
      答案层在命中多个时返回 `character_candidates`。

【4】「dio」搜不到（大小写/模糊）
    英文只做**全名**匹配：`(?<!...)dio brando(?!...)` 在 "dio" 里当然找不到。
    → 英文加**前缀/整词**匹配，且始终小写比较（本来就 lower，问题在只比全名）。

【5】守门不能坏
    放宽匹配最怕的是「今天天气怎么样」变成"有答案"。
    → 子串索引只含**角色名**的片段（「天气预报」是替身名，不在角色表）；
      Router 的 `has_domain` 只加 `character_hit`，不加"随便命中"。

跑法：python evaluation/test_m74_search_and_images.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROC = ROOT / "dataset" / "processed"
IMG = ROOT / "images"
sys.path.insert(0, str(ROOT / "services"))

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          + (f"   {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    print("=" * 74)
    print("M74：搜索体验（模糊/多候选）+ 角色与地点图片")
    print("=" * 74)

    import characters as C

    # ---- ① 模糊搜索：简称/姓氏/英文前缀 ----
    print("\n[1] ★ 模糊搜索（用户：承太郎/齐贝林/dio 都搜不到）")
    cases = [
        ("承太郎", "jotaro_kujo"),          # 简称（名字的一半）
        ("迪亚哥", "diego_brando"),
        ("布加拉提", "bruno_bucciarati"),
        ("乔尼", "johnny_joestar"),
        ("dio", "dio_brando"),              # 英文前缀 + 小写
        ("DIO", "dio_brando"),              # 大小写不敏感
        ("Jotaro", "jotaro_kujo"),          # 英文名的一半
        ("东方仗助", "josuke_higashikata"),  # 全名仍要命中
    ]
    for q, want in cases:
        hits = [c for c, _ in C.match(q)]
        check(f"「{q}」命中 {want}", want in hits, f"实得 {hits[:6]}")

    # ---- ② 多候选：重名/同姓要**全部返回** ----
    print("\n[2] ★ 重名/同姓 → 全部返回（用户：就都返回就行了）")
    ans, _, _ = C.answer("齐贝林是谁")
    check("「齐贝林」返回 character_candidates",
          ans.get("type") == "character_candidates", f"实得 {ans.get('type')}")
    check("「齐贝林」≥5 个候选（5 位齐贝林）",
          ans.get("count", 0) >= 5, f"实得 {ans.get('count')}")
    ans2, _, _ = C.answer("东方是谁")
    check("「东方」返回 ≥10 个候选（东方家）",
          ans2.get("type") == "character_candidates"
          and ans2.get("count", 0) >= 10, f"实得 {ans2.get('count')}")
    # 唯一命中时不能变成候选列表
    ans3, _, _ = C.answer("承太郎是谁")
    check("「承太郎是谁」仍是单人档案（不误触发候选列表）",
          ans3.get("type") == "character_fact", f"实得 {ans3.get('type')}")
    # 「第 X 部有哪些角色」不能被候选列表抢走
    ans4, _, _ = C.answer("第4部有哪些角色")
    check("「第4部有哪些角色」仍是 character_list",
          ans4.get("type") == "character_list", f"实得 {ans4.get('type')}")

    # ---- ③ 守门：放宽匹配不能把无关问句变成有答案 ----
    print("\n[3] ★ 守门（放宽匹配的最大风险）")
    for q in ("今天天气怎么样", "华盛顿在哪里", "甘道夫的替身是什么"):
        check(f"「{q}」不命中任何角色", C.match(q) == [], f"实得 {C.match(q)[:3]}")
    from characters import fuzzy_hits
    check("fuzzy_hits 对无关问句返回空", fuzzy_hits("今天天气怎么样") == [])
    check("★「天气」不会因为「天气预报」是替身名而误命中角色",
          fuzzy_hits("今天天气怎么样") == [])

    # ---- ④ 图片索引 ----
    print("\n[4] ★ 角色/地点图片（用户：人物要有图，地点也加上）")
    idx_p = PROC / "media_index.json"
    check("media_index.json 存在", idx_p.exists())
    if idx_p.exists():
        idx = json.loads(idx_p.read_text(encoding="utf-8"))
        n_ch = len(idx.get("character") or {})
        n_lo = len(idx.get("location") or {})
        check(f"角色头像 ≥350 个（实得 {n_ch}）", n_ch >= 350)
        check(f"地点图 ≥120 个（实得 {n_lo}）", n_lo >= 120)
        # 文件必须真的在（索引与磁盘一致）
        # ★ 图片是**抓取产物**（已 gitignore）：全新部署 / CI 上 images/ 为空，
        #   此时不该判失败 —— 前端 `<img onerror>` 会隐藏坏图，属优雅降级。
        #   索引本身仍须正确（它入库，是"哪张图对应哪个实体"的映射）。
        if not IMG.exists():
            print("  ⚠ images/ 未抓取（全新部署正常）→ 跳过文件存在性检查")
        else:
            missing = []
            for kind, m in idx.items():
                for eid, fn in m.items():
                    p = IMG / eid / Path(fn).name
                    if not p.exists() or p.stat().st_size < 512:
                        missing.append(f"{kind}/{eid}")
            check(f"索引里的图片文件都存在（缺失 {len(missing)}）",
                  not missing, f"缺失样例 {missing[:5]}")
        # 关键实体必须有图
        for kind, eid in (("character", "jotaro_kujo"),
                          ("character", "diego_brando"),
                          ("location", "morioh")):
            check(f"{kind}/{eid} 有头像", eid in (idx.get(kind) or {}))
        # 占位图不能进索引（源站 NoPicAvailable.png）
        bad = [f for m in idx.values() for f in m.values()
               if "NoPic" in f or "Placeholder" in f]
        check("源站占位图已过滤（NoPicAvailable 等）", not bad, f"实得 {bad[:3]}")

    # ---- ⑤ 答案里带 image 字段 ----
    print("\n[5] 答案携带 image 字段（前端据此渲染）")
    a1, _, _ = C.answer("承太郎是谁")
    check("character_fact 带 image", bool(a1.get("image")), f"实得 {a1.get('image')}")
    a2, _, _ = C.answer("齐贝林是谁")
    n_img = sum(1 for x in (a2.get("items") or []) if x.get("image"))
    check("character_candidates 每一项都带 image",
          n_img == len(a2.get("items") or []), f"{n_img}/{len(a2.get('items') or [])}")
    import locations as L
    a3, _, _ = L.answer("杜王町在哪里")
    check("location_fact 带 image", bool(a3.get("image")), f"实得 {a3.get('image')}")

    print("\n" + "=" * 74)
    if FAILS:
        print(f"✗ {len(FAILS)} 项失败：")
        for f in FAILS:
            print(f"   · {f}")
        return 1
    print("✓ M74 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""M53 回归测试：「无限」被显示成「无数据」。

用户反馈：
  「明明天堂制造的速度是无限 为什么六维图和六维明细里写的是无呢」

★ 实测（不是推测）：
    stand_stats 里 Made in Heaven 的 spd 是
        value=NULL, raw='∞', cat='INFINITE'
    —— 它有值（无限），只是 ∞ 放不进 1..5 的等级刻度，所以数值列是 NULL。

== 根因：整条链路把「value 是 NULL」直接等同于「无数据」 ==
  1. `services/media.py: radar_payload`
        `if v is None: missing.append(k)` —— 只看 NULL，**没看 cat**。
     → INFINITE 被归进 missing → 雷达图标成「无数据」，
       图下还注明「未推测为 0」。
  2. `services/executor.py` 的 value_label
        `LEVEL_CN.get(v["value"]) if v["value"] is not None else None`
     → 答案卡「数值」显示「—」，问「速度是几级」像是"没数据"。
  3. `api/main.py` 的 `_lv`（zh_card.stats_cn）
        同样按 NULL → None → 前端「六维明细/六维」一律渲染成「无数据」。
  4. `api/main.py /stands/{id}` 的 stats.label → 「值」列「—」，
     而同一行的「原始」是 ∞、「类别」是无限 —— 自相矛盾。

== 关键依据：只有 INFINITE 的 raw 是「值符号」 ==
  value 为 NULL 时各类别的实测分布（共 276 处，写判定前查过）：
      NOT_APPLICABLE     198  raw=None   该源未提供
      EMPTY_SLOT          53  raw='∅'    空位（真缺失）
      UNKNOWN             16  raw='?'    未知（存在争议）
      **INFINITE           6  raw='∞'    无限 —— 有值！**
      CONDITIONAL_NO_BASE  3  raw='※…'   条件值无基础等级
  —— 只有 INFINITE 要单独走 `specials`，其余仍然算「无数据」。

== 本测试守什么 ==
  1. radar_payload 把 INFINITE 归入 specials、**不进 missing**
  2. 真缺失（NOT_APPLICABLE / EMPTY_SLOT / UNKNOWN）仍然算 missing
  3. 雷达图：无限维度标 ∞（不是「无数据」）、有**空心**标记、
     不画数据点、说明里写「无限」而不是「未推测为 0」
  4. 混合情况（1 个无限 + 1 个缺失）两条说明都在，且不重叠、不溢出
  5. 答案卡/明细用的 level_label：INFINITE → ∞
  6. 数据侧：全库 6 个 INFINITE 维度，且 raw 都是 ∞（对接 pipeline 的 V10 校验）
  7. 静态：两个 API 调用点都传了 specials，且 row 里带了 *_raw / *_cat
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROC = ROOT / "dataset" / "processed"
sys.path.insert(0, str(ROOT / "services"))

DIM_KEYS = ("pwr", "spd", "rng", "sta", "prc", "dev")


def _row(**kw) -> dict:
    """按 stand_stats 的形状造一行。"""
    r: dict = {k: kw.get(k) for k in DIM_KEYS}
    for k in DIM_KEYS:
        r[f"{k}_raw"] = kw.get(f"{k}_raw")
        r[f"{k}_cat"] = kw.get(f"{k}_cat")
    return r


def main() -> int:
    fails: list[str] = []
    n = 0

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal n
        n += 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}"
              f"{('   ' + detail) if detail and not ok else ''}")
        if not ok:
            fails.append(name)

    from media import radar_payload          # noqa: E402
    from radar_chart import DIMS, radar_svg, _CY, _R, _LABEL_R  # noqa: E402

    print("=" * 74)
    print("M53：「无限」不能被当作「无数据」")
    print("=" * 74)

    # ---- ① radar_payload 的分类 ----
    print("\n[1] 分类：无限 vs 真缺失")
    mh = _row(pwr=4, spd=None, spd_raw="∞", spd_cat="INFINITE",
              rng=3, sta=5, prc=3, dev=5,
              **{f"{k}_cat": "RANKED" for k in ("pwr", "rng", "sta", "prc", "dev")})
    rp = radar_payload(mh)
    check("Made in Heaven：spd 进 specials 且值为 ∞",
          rp["specials"].get("spd") == "∞", f"实际 {rp['specials']}")
    check("Made in Heaven：spd 不进 missing",
          "spd" not in rp["missing"], f"missing={rp['missing']}")
    check("Made in Heaven：其余五维正常在 stats 里",
          all(rp["stats"][k] is not None for k in ("pwr", "rng", "sta", "prc", "dev")))

    tusk = _row()
    for k in DIM_KEYS:
        tusk[f"{k}_cat"] = "NOT_APPLICABLE"
    rp_t = radar_payload(tusk)
    check("Tusk（该源未提供）仍算 missing，且没有 specials",
          len(rp_t["missing"]) == 6 and not rp_t["specials"],
          f"missing={len(rp_t['missing'])} specials={rp_t['specials']}")

    for cat, raw in [("EMPTY_SLOT", "∅"), ("UNKNOWN", "?")]:
        # ★ 其余五维要给**值**，否则它们也会进 missing（第一版漏了，误报失败）
        kw = {k: 3 for k in DIM_KEYS[1:]}
        kw.update({f"{k}_cat": "RANKED" for k in DIM_KEYS[1:]})
        kw.update(pwr=None, pwr_raw=raw, pwr_cat=cat)
        rr = radar_payload(_row(**kw))
        check(f"{cat}（真缺失）算 missing、不算 specials",
              rr["missing"] == ["pwr"] and not rr["specials"],
              f"missing={rr['missing']} specials={rr['specials']}")

    # ---- ② 雷达图渲染 ----
    print("\n[2] 雷达图：∞ 的标签与标记")
    svg = radar_svg(rp["stats"], missing=rp["missing"],
                    specials=rp["specials"], title="天堂制造")
    check("★ 全图不出现「无数据」", "无数据" not in svg)
    check("速度标签第二行是 ∞（不是「无数据」）",
          bool(re.search(r"速度<tspan[^>]*>∞</tspan>", svg)))
    check("外圈有**空心**标记（fill:none + 主色描边）",
          bool(re.search(r'<circle[^>]*fill="none"[^>]*stroke="#7F77DD"', svg)))
    check("无限维度**不画实心数据点**（实心点只有 5 个）",
          len(re.findall(r'<circle[^>]*r="3" fill="#7F77DD"', svg)) == 5,
          f"实际 {len(re.findall(r'<circle[^>]*r=.3. fill=.#7F77DD.', svg))} 个")
    check("说明里写明「无限」",
          "无限" in svg and "超出 E~A 等级" in svg)
    check("说明里**不**写「未推测为 0」（那不是缺值）",
          "未推测为 0" not in svg)

    # ---- ③ 混合：1 无限 + 1 缺失 ----
    print("\n[3] 混合情况（1 个无限 + 1 个缺失）")
    mix = svg
    mx = _row(pwr=4, spd=None, spd_raw="∞", spd_cat="INFINITE",
              rng=None, rng_raw="∅", rng_cat="EMPTY_SLOT",
              sta=5, prc=3, dev=5,
              **{f"{k}_cat": "RANKED" for k in ("pwr", "sta", "prc", "dev")})
    rpm = radar_payload(mx)
    check("混合：spd 进 specials、rng 进 missing",
          rpm["specials"] == {"spd": "∞"} and rpm["missing"] == ["rng"],
          f"specials={rpm['specials']} missing={rpm['missing']}")
    svgm = radar_svg(rpm["stats"], missing=rpm["missing"],
                     specials=rpm["specials"], title="混合")
    check("混合：缺值说明仍在（射程 无数据）",
          "射程 无数据" in svgm and "未推测为 0" in svgm)
    check("混合：无限说明也在（速度 = ∞）", "速度 = ∞" in svgm)
    # 说明文字的几何不能压到底部标签（M51 的约束）
    h = int(re.search(r'viewBox="0 0 \d+ (\d+)"', svgm).group(1))
    labels, notes = [], []
    for mm in re.finditer(r'<text x="[\d.]+" y="([\d.]+)"[^>]*font-size="(\d+)"'
                          r'[^>]*>(.*?)</text>', svgm):
        y, fs, inner = float(mm.group(1)), int(mm.group(2)), mm.group(3)
        (labels if "<tspan" in inner else notes).append((y, fs))
    check("混合：说明仍在底部标签下方（≥30px，M51 约束）",
          min(y for y, _ in notes) - (_CY + _R * _LABEL_R) >= 30,
          f"{min(y for y,_ in notes) - (_CY + _R * _LABEL_R):.1f}px")
    check("混合：说明在画布高度内",
          h >= max(y for y, _ in notes) + 4, f"画布 {h}")

    # ---- ④ level_label（答案卡 / 六维明细的取值） ----
    print("\n[4] level_label：INFINITE → ∞")
    from executor import level_label       # noqa: E402
    check("INFINITE（value=None）→ '∞'", level_label(None, "INFINITE") == "∞")
    check("真缺失（value=None, cat=UNKNOWN）→ None",
          level_label(None, "UNKNOWN") is None)
    check("正常等级 5 → 'A'", level_label(5, "RANKED") == "A")
    check("默认参数（不传 cat）行为不变", level_label(3) == "C")

    # ---- ⑤ 数据侧 ----
    print("\n[5] 数据侧")
    stats = {r["stand_id"]: r for r in json.loads(
        (PROC / "stand_stats.json").read_text(encoding="utf-8"))}
    inf = [(sid, k) for sid, r in stats.items() for k in DIM_KEYS
           if r.get(f"{k}_cat") == "INFINITE"]
    check(f"全库 INFINITE 维度 = 6（实测 {len(inf)}）", len(inf) == 6,
          str(inf))
    check("每个 INFINITE 的 value 都是 NULL（所以才会被误判）",
          all(stats[sid].get(k) is None for sid, k in inf))
    check("每个 INFINITE 都保留了 raw='∞'（pipeline V10 校验）",
          all(str(stats[sid].get(f"{k}_raw")) == "∞" for sid, k in inf))
    check("天堂制造在这个集合里", ("made_in_heaven", "spd") in inf)

    # ---- ⑥ 静态：调用点有没有漏传 ----
    print("\n[6] 调用点")
    api = (ROOT / "api" / "main.py").read_text(encoding="utf-8")
    check("查询链路传了 specials",
          api.count("specials=rp[\"specials\"]") == 2,
          f"实际 {api.count('specials=rp[\"specials\"]')} 处")
    check("两个 radar_payload 调用点都带了 *_raw / *_cat",
          api.count('f"{k}_raw"') == 2 and api.count('f"{k}_cat"') == 2)
    check("zh_card 的 _lv 会优先用 specials（∞ 不被当成 None）",
          '_sp[_key]' in api and '_key in _sp' in api)
    check("/stands 的 stats.label 对 INFINITE 返回 ∞",
          '"∞" if r[23 + i] == "INFINITE"' in api)

    print()
    if fails:
        print(f"✗ {len(fails)}/{n} 项失败：" + "、".join(fails))
        return 1
    print(f"✓ 全部通过（{n} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

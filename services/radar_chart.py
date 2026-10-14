"""替身六维能力雷达图（M17）。

==★ 核心原则：缺失维度必须留空，不能填0 ★★

数据里 `stand_stats.missing_count > 0` 表示该替身有维度缺值
（jojowiki 原文写的是 "?" / "Unknown" / "varies" 之类）。
★ 填0 是**误导**：雷达图上0 会被画成"这项能力为零"，
  而事实是"这项没有数据"。两者在视觉上无法区分，
  但含义完全相反 —— 这比不画图更糟。
→ 本模块的做法：
  1. 缺值维度**不画数据点**，六边形在该方向上内缩到边界（表示无数据）
  2. 在图下方列出缺哪几项，文字明确标注「无数据」
  3. 数据点的连线遇到缺值维度时**断开**，不跨越

==★ 为什么用 SVG 而不是 matplotlib ==★
  - 服务端零新增依赖（matplotlib 会带来 numpy/fontconfig 一堆问题）
  - SVG 可直接内嵌 HTML、缩放不失真、能带 CSS 主题色
  - GUI 里直接 `<img src="data:image/svg+xml,...">` 或内联即可

用法：
    from radar_chart import radar_svg
    svg = radar_svg({"pwr":5,"spd":5,"rng":3,"sta":4,"prc":5,"dev":3},
                    missing=["sta"])
"""
from __future__ import annotations

import math
from typing import Optional, Sequence

# 六维顺序：与 DB 的 stand_stats 列顺序一致（pwr/spd/rng/sta/prc/dev）
DIMS: list[tuple[str, str]] = [
    ("pwr", "破坏力"),
    ("spd", "速度"),
    ("rng", "射程"),
    ("sta", "持续力"),
    ("prc", "精密性"),
    ("dev", "成长性"),
]

# 值域 0..5（DB 存的是 smallint，0=无 1=E 2=D 3=C 4=B 5=A）
VMAX = 5

#等级名（与 api/main.py 的 level_cn 一致）
LEVEL_CN = {0: "无", 1: "E", 2: "D", 3: "C", 4: "B", 5: "A"}

_SIZE = 260          # 画布边长
_CX = _CY = 130      # 中心
_R = 92              # 外圈半径

# ★ M51：标签几何必须**单点定义**，供「缺失说明」定位使用。
#   之前两处各写各的（标签用 1.24、说明用 _SIZE+4），谁也没算对方多高 → 压行。
_LABEL_R = 1.24      # 标签中心半径倍数
_LABEL_LINE2_DY = 11  # 标签第二行（等级 / 「无数据」）相对标签中心的偏移
_NOTE_FS = 10         # 缺失说明字号
# ★ M51：说明文字的 y **由实测反推**，不能写死（原来是 _SIZE+4，直接压在标签上）。
#   浏览器实测（Chromium，用 SVG getBBox 量真实文字范围）：
#     · 底部标签「持续力 A」的 bbox 下缘 = 标签中心 y + 17.5
#       （两行文字，且含字体 descent —— 光按字号算是 13，会少 4.5px）
#     · 说明文字（font-size 10）的 bbox 上缘 = y - 10.5
#   要留 6px 空隙 → 说明 y = 标签中心 y + 17.5 + 10.5 + 6 = 中心 y + 34
_NOTE_DY_FROM_BOTTOM = 34


def _pt(i: int, ratio: float) -> tuple[float, float]:
    """第 i 个维度、半径比例 ratio 处的坐标。

    起始角 -90°（正上），顺时针 —— 这样破坏力在顶部，
    与 jojowiki 官方六边图的排布一致。
    """
    ang = -math.pi / 2 + i * 2 * math.pi / len(DIMS)
    return (_CX + _R * ratio * math.cos(ang),
            _CY + _R * ratio * math.sin(ang))


def _esc(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def radar_svg(stats: dict[str, Optional[int]],
               missing: Optional[Sequence[str]] = None,
               title: str = "",
               accent: str = "#7F77DD",
               specials: Optional[dict[str, str]] = None) -> str:
    """生成六维雷达图 SVG。

    参数
      stats    : {dim_key: value_or_None}，value ∈ 0..5
      missing  : 明确声明缺失的维度（会与 stats 里的 None 合并）
      title    : 图上方标题（通常是替身名）
      accent   : 主色
      specials : {dim_key: 原始字面量} —— **有值、但超出 1..5 刻度**的维度
                 （目前只有 INFINITE，raw = '∞'）。
                 ★★ M53：它们**不是「无数据」**。以前和 missing 混在一起，
                   于是「天堂制造的速度是无限」在图上写成了「无数据」，
                   下面还注明「未推测为 0」—— 把"无限"说成"没有数据"。
                 画法：外圈画一个**空心**标记（区别于实心数据点，
                 表示"超出刻度"），**不连线**（不猜它在刻度上的位置），
                 标签第二行直接写原始值（∞），并在图下单独说明。

    ★ 缺失维度处理见文件头说明：不填 0，连线断开，图下列出。
    """
    missing_set = set(missing or ())
    special_set = set(specials or {})
    vals: list[Optional[float]] = []
    for k, _ in DIMS:
        v = stats.get(k)
        if v is None or (isinstance(v, str) and not str(v).strip()):
            vals.append(None)
        else:
            try:
                iv = int(v)
            except (TypeError, ValueError):
                vals.append(None)
                continue
            vals.append(None if iv <= 0 and k in missing_set else float(iv))
    # 明确声明缺失的强制置 None
    for i, (k, _) in enumerate(DIMS):
        if k in missing_set:
            vals[i] = None
    # ★ M53：无限维度也不参与多边形 —— 它在刻度上没有位置可放
    for i, (k, _) in enumerate(DIMS):
        if k in special_set:
            vals[i] = None

    # ---- ★ M51：先算好「缺失说明」的位置与行数，再决定画布高度 ----
    #   底部标签是 i=3（正下方），它的第二行基线 = _CY + _R*_LABEL_R + dy。
    #   说明必须写在**它下面**，否则就会像之前那样压在「持续力 A」上。
    #   ★ M53：说明分两类（缺数据 / 无限），逐条按需拆行。
    raw_notes: list[str] = []
    if missing_set:
        raw_notes.append(
            "、".join(cn for k, cn in DIMS if k in missing_set)
            + " 无数据（未推测为 0）")
    if special_set:
        raw_notes.append(
            "、".join(f"{cn} = {specials.get(k) or '∞'}"
                      for k, cn in DIMS if k in special_set)
            + "（无限，超出 E~A 等级，未画入图中）")
    note_lines: list[str] = []
    for ln in raw_notes:
        # 中文按 1em/字估算宽度；画布宽 _SIZE，留出边距
        if len(ln) * _NOTE_FS > _SIZE - 16 and "（" in ln:
            head, tail = ln.split("（", 1)
            note_lines += [head, "（" + tail]
        else:
            note_lines.append(ln)
    _label_cy = _CY + _R * _LABEL_R          # 底部标签（i=3，正下方）的中心 y
    _note_y = _label_cy + _NOTE_DY_FROM_BOTTOM
    _height = (_note_y + (len(note_lines) - 1) * (_NOTE_FS + 2) + 6
               if note_lines else _SIZE + 8)

    p: list[str] = []
    p.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {_SIZE} '
        f'{_height:.0f}" width="100%" '
        f'style="max-width:300px;display:block;margin:0 auto" '
        f'role="img" aria-label="{_esc(title or "六维能力图")}">')

    # ---- 网格：5 层六边形 ----
    for r in range(1, VMAX + 1):
        ratio = r / VMAX
        pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in
                       (_pt(i, ratio) for i in range(len(DIMS))))
        p.append(f'<polygon points="{pts}" fill="none" '
                 f'stroke="var(--color-border-tertiary,rgba(128,128,128,.25))" '
                 f'stroke-width="0.5"/>')
    # 中心到各顶点的辐条
    for i in range(len(DIMS)):
        x, y = _pt(i, 1.0)
        p.append(f'<line x1="{_CX}" y1="{_CY}" x2="{x:.1f}" y2="{y:.1f}" '
                 f'stroke="var(--color-border-tertiary,rgba(128,128,128,.18))" '
                 f'stroke-width="0.5"/>')

    # ---- 数据多边形 ----
    # ★ 遇到 None 的维度就断开路径，分段画，避免连线跨越无数据区域
    segs: list[list[str]] = []
    cur: list[str] = []
    for i, v in enumerate(vals):
        if v is None:
            if len(cur) > 1:
                segs.append(cur)
            cur = []
            continue
        ratio = min(v / VMAX, 1.0)
        x, y = _pt(i, ratio)
        cur.append(f"{x:.1f},{y:.1f}")
    if len(cur) > 1:
        segs.append(cur)
    if len(cur) == 1:      # 只有一个点，画不出来，忽略
        segs = segs[:-1]

    for seg in segs:
        # 闭环：首尾相连（该段内部所有维度都有值）
        closed = seg + [seg[0]]
        p.append(f'<polygon points="{" ".join(closed)}" '
                 f'fill="{accent}" fill-opacity="0.18" stroke="{accent}" '
                 f'stroke-width="1.5" stroke-linejoin="round"/>')

    # ---- 数据点 ----
    for i, v in enumerate(vals):
        if v is None:
            continue
        x, y = _pt(i, min(v / VMAX, 1.0))
        p.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{accent}"/>')

    # ---- ★ M53：无限维度 —— 外圈画**空心**标记 ----
    #   实心点 = 有等级；空心 = 超出刻度（∞），两者一眼可分辨。
    #   位置放在外圈上（不是刻度上的某一点），并不连线。
    for i, (k, _) in enumerate(DIMS):
        if k not in special_set:
            continue
        mx, my = _pt(i, 1.0)
        p.append(f'<circle cx="{mx:.1f}" cy="{my:.1f}" r="3.6" fill="none" '
                 f'stroke="{accent}" stroke-width="1.6"/>')

    # ---- 维度标签（缺失的标「无数据」；无限的标原始值 ∞）----
    for i, (k, cn) in enumerate(DIMS):
        # 标签放外圈外一点
        lx, ly = _pt(i, _LABEL_R)
        v = vals[i]
        if k in special_set:
            p.append(f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="middle" '
                     f'dominant-baseline="central" font-size="11" '
                     f'fill="var(--color-text-secondary,#aaa)">{_esc(cn)}'
                     f'<tspan x="{lx:.1f}" dy="{_LABEL_LINE2_DY}" font-size="10" '
                     f'fill="{accent}">{_esc(specials.get(k) or "∞")}</tspan>'
                     f'</text>')
        elif v is None:
            p.append(f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="middle" '
                     f'dominant-baseline="central" font-size="11" '
                     f'fill="var(--color-text-tertiary,#999)">{_esc(cn)}'
                     f'<tspan x="{lx:.1f}" dy="{_LABEL_LINE2_DY}" font-size="9">'
                     f'无数据</tspan></text>')
        else:
            p.append(f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="middle" '
                     f'dominant-baseline="central" font-size="11" '
                     f'fill="var(--color-text-secondary,#aaa)">{_esc(cn)}'
                     f'<tspan x="{lx:.1f}" dy="{_LABEL_LINE2_DY}" font-size="10" '
                     f'fill="{accent}">{LEVEL_CN.get(int(v), "?")}</tspan>'
                     f'</text>')

    # ---- 标题 ----
    if title:
        p.insert(1, f'<title>{_esc(title)}</title>')

    # ---- 缺失维度说明（明确写出，避免被误读成 0 分）----
    #   ★ M51：y 按**底部标签的实际末行**推出来（见上方几何计算）。
    #     原来是写死的 _SIZE+4=264，而底部标签「持续力 A」的第二行
    #     基线在 255、下缘约 258 —— 两者直接叠在一起。
    for li, line in enumerate(note_lines):
        yy = _note_y + li * (_NOTE_FS + 2)
        p.append(
            f'<text x="{_CX}" y="{yy:.0f}" text-anchor="middle" '
            f'font-size="{_NOTE_FS}" '
            f'fill="var(--color-text-tertiary,#999)">'
            f'{_esc(line)}</text>')

    p.append("</svg>")
    return "".join(p)
"""M51 回归测试：两处「文字换行/压行」的排版缺陷。

用户反馈（截图三张）：
  ① 答案卡片里「类别」的值**单独换了一行、还缩进约 8 个空格**，
     而上面「替身/维度/数值/原始值」四行的值都正常对齐。
  ② 雷达图下方「速度 无数据（未推测为 0）」与底部标签「持续力 A」**压在一起**。

== 根因（都实测确认，不是推测）==

【①】`white-space` 是**可继承**属性。
  `api/static/index.html` 里 `.answer { white-space: pre-wrap }`
  —— 本意是保留纯文本答案里的 `\\n\\n` 空行；但结构化答案是
  **多行模板字符串**拼出来的 HTML 片段，于是被一起继承了。
  而 fact 模板里恰好只有「类别」那一行的 `<dd>` 写成：
      <dt>类别</dt><dd>
        <span ...>无限</span></dd>
  —— 它以「换行 + 8 个空格」开头（其余 4 行 `<dd>` 紧跟标签、无前导空白），
  在 pre-wrap 下这段空白被**原样渲染**成换行 + 缩进。所以只有它中招。

【②】`services/radar_chart.py` 里说明文字的 y 是**写死的** `_SIZE + 4`，
  谁也没算底部标签有多高：
      底部标签中心 y = _CY + _R*1.24 = 244.1，第二行基线 255.1，
      浏览器实测 bbox 下缘 **261.6**（比按字号估的多 4.5px，含字体 descent）
      而说明文字 y=264、bbox 上缘 **260.6** → 与标签重叠。
  → 改成由「标签中心 y + 实测偏移」推出，并让画布高度跟着说明走。

== 本测试守什么 ==
静态可查的 4 条 + 雷达图几何 3 条（**不需要起服务，秒级**）：
  1. `.answer` 之外的**结构化答案容器**必须显式关掉 pre-wrap
  2. `.kv` 自身也不要继承 pre-wrap（第二道防线）
  3. 整个 HTML 里不该再出现「`<dd>` 后紧跟换行」的写法
  4. 雷达图：说明文字必须在底部标签**下方**足够远处
  5. 雷达图：说明文字必须在画布高度之内
  6. 雷达图：说明文字单行宽度不超出画布宽度
  7. 雷达图：缺失维度仍逐项标注「无数据」，且说明里写明「未推测为 0」

★ 为什么用静态测试而不是 Playwright：
  这两类的成因是**静态的**（CSS 继承 + 写死的坐标），静态断言足够守住；
  浏览器实测另有一次性验证（见 commit 说明），不适合放进日常回归。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "api" / "static" / "index.html"

sys.path.insert(0, str(ROOT / "services"))


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

    print("=" * 74)
    print("M51：结构化答案的 white-space / 雷达图说明文字的位置")
    print("=" * 74)

    src = HTML.read_text(encoding="utf-8")
    css = re.sub(r"<script[^>]*>.*?</script>", "", src, flags=re.S)

    # ---- ① 结构化答案容器关掉 pre-wrap ----
    print("\n[1] 结构化答案容器")
    m = re.search(r"\.answer\s*\{([^}]*)\}", css)
    base_ws = (m.group(1) if m else "")
    check(".answer 基线仍是 pre-wrap（纯文本答案要保留换行）",
          "pre-wrap" in base_ws, f"实际 {base_ws.strip()[:60]}")
    check("存在 .answer.ans-html{white-space:normal}",
          bool(re.search(r"\.answer\.ans-html\s*\{[^}]*white-space\s*:\s*normal",
                         css)))
    check(".kv 自身声明 white-space:normal（第二道防线）",
          bool(re.search(r"\.kv\s*\{[^}]*white-space\s*:\s*normal", css)))

    # ---- ② 结构化分支必须挂上 ans-html ----
    print("\n[2] 结构化答案的分发")
    check("有 isHtml 标记",
          "let isHtml" in src)
    check("数组分支（折叠英文原文）置 isHtml=true",
          bool(re.search(r"details class=\"en-fold\"[\s\S]{0,600}?isHtml\s*=\s*true",
                         src)))
    check("object 分支（renderStructured）置 isHtml=true",
          bool(re.search(r"body = renderStructured\(a\);\s*\n\s*isHtml\s*=\s*true",
                         src)))
    check("容器模板会在 isHtml 时加 ans-html 类",
          bool(re.search(r"isHtml\s*\?\s*'\s*ans-html'", src)))

    # ---- ③ 不再有「<dd> 后紧跟换行」的写法 ----
    print("\n[3] 模板里的前导空白")
    dd_nl = re.findall(r"<dd>\s*\n", src)
    check("全文件没有「<dd> 后跟换行」的写法", not dd_nl,
          f"找到 {len(dd_nl)} 处")
    # 顺带：答案模板里不该出现「标签后紧跟纯空白行」的 dt/dd
    check("fact 模板的「类别」<dd> 与 <span> 之间无空白",
          bool(re.search(r"<dt>类别</dt><dd><span", src)))

    # ---- ④ 雷达图几何 ----
    print("\n[4] 雷达图：说明文字的位置")
    from radar_chart import DIMS, radar_svg, _CY, _R, _LABEL_R  # noqa: E402

    def analyse(stats, missing, tag):
        svg = radar_svg(stats, missing=missing, title="t")
        h = int(re.search(r'viewBox="0 0 \d+ (\d+)"', svg).group(1))
        w = int(re.search(r'viewBox="0 0 (\d+) \d+"', svg).group(1))
        labels, notes = [], []
        for mm in re.finditer(r'<text x="[\d.]+" y="([\d.]+)"[^>]*font-size="(\d+)"'
                              r'[^>]*>(.*?)</text>', svg):
            y, fs, inner = float(mm.group(1)), int(mm.group(2)), mm.group(3)
            plain = re.sub(r"<[^>]+>", "", inner)
            (labels if "<tspan" in inner else notes).append((y, fs, plain))
        return {"tag": tag, "h": h, "w": w, "svg": svg,
                "labels": labels, "notes": notes}

    cases = [
        analyse({"pwr": 4, "spd": None, "rng": 3, "sta": 5, "prc": 3, "dev": 5},
                ["spd"], "单维缺失"),
        analyse({"pwr": 4, "spd": None, "rng": None, "sta": 5, "prc": None,
                 "dev": 5}, ["spd", "rng", "prc"], "三维缺失"),
        analyse({k: None for k, _ in DIMS}, [k for k, _ in DIMS], "全缺失"),
    ]

    label_cy = _CY + _R * _LABEL_R
    for c in cases:
        tag = c["tag"]
        check(f"{tag}：有 6 个维度标签且都带第二行",
              len(c["labels"]) == 6, f"实际 {len(c['labels'])}")
        if not c["notes"]:
            check(f"{tag}：有缺失说明文字", False)
            continue
        note_top_of_svg = min(y for y, _, _ in c["notes"])
        # ★ 实测（Chromium getBBox）：底部标签 bbox 下缘 = 标签中心 y + 17.5；
        #   说明文字 bbox 上缘 = y - 10.5。要留 ≥6px 空隙 → 说明 y ≥ 中心 y + 34。
        #   这里按**保守阈值 30** 断言（留出字体差异余量）。
        gap = note_top_of_svg - label_cy
        check(f"{tag}：说明文字在底部标签下方（≥30px）",
              gap >= 30, f"仅 {gap:.1f}px")
        check(f"{tag}：说明文字在画布高度内",
              c["h"] >= max(y for y, _, _ in c["notes"]) + 4,
              f"画布高 {c['h']}，说明 y {max(y for y,_,_ in c['notes'])}")
        for y, fs, txt in c["notes"]:
            est_w = len(txt) * fs          # 中文按 1em/字估算
            check(f"{tag}：说明「{txt[:14]}…」不超宽",
                  est_w <= c["w"] - 8, f"估算 {est_w}px > 画布 {c['w']}px")

    # ---- ⑤ 缺失维度仍逐项标注 ----
    print("\n[5] 缺失维度的标注")
    c = cases[0]
    check("缺失维度标签下有「无数据」副行",
          "无数据</tspan>" in c["svg"])
    check("说明文字含「未推测为 0」",
          any("未推测为 0" in t for _, _, t in c["notes"]))
    check("说明文字列出了缺失的那个维度名",
          any("速度" in t for _, _, t in c["notes"]))

    print()
    if fails:
        print(f"✗ {len(fails)}/{n} 项失败：" + "、".join(fails))
        return 1
    print(f"✓ 全部通过（{n} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

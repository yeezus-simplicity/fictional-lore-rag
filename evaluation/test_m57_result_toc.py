"""M57 回归测试：结果页内的**板块导航**（用户真正要的那个"导航栏"）。

用户原话：
  「我不是让你把这个改回去吗 我要的导航栏是搜索结果出来后
    能够导航到各个部分 如答案 替身资料 检索过程 图片」

== 我之前理解错在哪 ==
  M54 我把「左侧导航栏」做成了**全局**导航（问答 / 数据浏览 / 冲突记录 / 系统状态），
  而用户要的是**结果页内的板块索引**：搜出结果后，能跳到各个板块。
  → M57：全局导航回到**顶栏**（M54 之前的样子）；左侧留给结果页索引。

== 现在的结构 ==
  顶栏 <header>：品牌 / 状态灯 / 规模 / 构建号 / 四个视图按钮
  结果区 .result：
      .res-toc   左侧索引（sticky）——「本页内容」+ 1.答案 2.替身资料
                 3.检索过程 4.图片 5.相似替身
      .res-main  右侧正文（#rout 路由徽标 + #ans 里的五个板块）
  板块 = 带 `data-sec`（锚点 key）与 `data-secname`（显示名）的卡片。
  顺序按用户列的顺序：答案 → 替身资料 → **检索过程** → 图片 → 相似替身
  （「检索过程」原本是页顶一个静态折叠块，现在成了正式板块，证据也归它。）

== 本测试守什么（都是"不报错但会失效"的点）==
  1. 索引项**按实际渲染出的板块**生成（读 [data-sec]）；< 2 块时整条隐藏
  2. ★ 点击跳转按 data-sec 匹配，**不是 getElementById**
     —— 实测踩坑：板块用的是 data-sec 属性、没有 id，
        写 getElementById 永远取到 null ⇒ 点了**完全没反应**、也不报错。
  3. 滚动高亮（scroll-spy）存在且用 rAF 节流（滚动事件很密）
  4. 板块会异步增加（相似替身 / 图片抓完）→ MutationObserver 自动补建
  5. 锚点跳转让开 sticky 顶栏（scroll-margin-top）
  6. 窄屏索引变横排 chip（不挤压正文）
  7. 五个板块的名称与锚点 key 都对得上
  8. 渲染顺序：先 renderAnswer（生成 #proc）再 renderProcess（填内容）
     —— 顺序反了会往不存在的节点写，静默失效
  9. #procCard 的访问都判空（它在 #ans 里，可能刚被清空）

★ 浏览器行为（点击是否真的滚动、滚动是否真的换高亮）用 Playwright 实测过：
    点「图片」→ 该板块 top=72px（正好在顶栏下方）、高亮切到「4. 图片」；
    滚回顶部 → 高亮回到「1. 答案」；排名类问题只有 1 块时索引隐藏。
  这类断言不适合放进日常回归（要起浏览器），故只在此记录。
"""
from __future__ import annotations

import http.client
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "api" / "static" / "index.html"
PORT = 8801

SECTIONS = [("sec-answer", "答案"), ("sec-stand", "替身资料"),
            ("sec-proc", "检索过程"), ("sec-images", "图片"),
            ("sec-similar", "相似替身")]


def wait_http(port: int, timeout: float = 240) -> bool:
    """等到**能真正应答**为止（只探端口会「假就绪」：uvicorn 先 bind 再跑启动事件）。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            c.request("GET", "/health")
            if c.getresponse().status == 200:
                return True
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
        time.sleep(2)
    return False


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

    src = HTML.read_text(encoding="utf-8")
    css = re.sub(r"<script[^>]*>.*?</script>", "", src, flags=re.S)

    print("=" * 74)
    print("M57：结果页内的板块导航（左侧索引）")
    print("=" * 74)

    # ---- ① 全局导航已回到顶栏 ----
    print("\n[1] 全局导航回到顶栏")
    check("有 <header>", "<header>" in src)
    check("M54 的全局侧栏已移除", '<aside class="side">' not in src)
    check("顶栏里有 4 个视图按钮 + 状态灯 + 构建号",
          all(f'data-v="{v}"' in src for v in "qbcs")
          and 'id="dot"' in src and 'id="webBuild"' in src)

    # ---- ② 结果区两列结构 ----
    print("\n[2] 结果区：左侧索引 + 右侧正文")
    check("DOM 有 .result / #resToc / #resMain",
          'class="result"' in src and 'id="resToc"' in src
          and 'id="resMain"' in src)
    check("CSS 有 .res-toc / .res-main", ".res-toc {" in css and ".res-main {" in css)
    check("索引 sticky 常驻、并让开顶栏",
          bool(re.search(r"\.res-toc\s*\{[^}]*position:\s*sticky", css))
          and bool(re.search(r"\.res-toc\s*\{[^}]*top:\s*\d+px", css)))
    check("锚点跳转让开 sticky 顶栏",
          bool(re.search(r"\[data-sec\]\s*\{[^}]*scroll-margin-top", css)))
    check("窄屏索引改横排 chip",
          bool(re.search(r"max-width:\s*860px[\s\S]{0,240}\.res-toc", css))
          and bool(re.search(r"max-width:\s*860px[\s\S]{0,320}flex-direction:\s*row",
                             css)))

    # ---- ③ 索引的生成 / 跳转 / 高亮 ----
    print("\n[3] 索引逻辑（不报错但会失效的点）")
    check("有 buildResToc()", "function buildResToc(" in src)
    check("★ 索引项按实际渲染出的板块生成（读 [data-sec]）",
          "querySelectorAll('[data-sec]')" in src)
    check("★ 少于 2 个板块时整条隐藏（避免点不过去的死链）",
          "secs.length < 2" in src and "toc.hidden = true" in src)
    check("★ 点击按 data-sec 匹配（**不是** getElementById）",
          "secs.find(s => s.dataset.sec === b.dataset.target)" in src
          and "getElementById(b.dataset.target)" not in src,
          "用了 getElementById 会静默点不动")
    check("有滚动高亮 updateTocActive()",
          "function updateTocActive(" in src)
    check("滚动监听用 rAF 节流",
          "addEventListener('scroll'" in src and "requestAnimationFrame" in src)
    check("板块异步增加时自动补建索引",
          "MutationObserver" in src and "observe(_resMain" in src)
    # ★ M76：索引项改成**胶囊导航**（前端重设计），不再显示「1. 2. …」序号
    #   —— 胶囊式标签带序号反而挤。断言改为「渲染板块名（secname 回退 sec）」。
    check("索引项渲染板块名（secname 回退 sec）",
          "s.dataset.secname || s.dataset.sec" in src,
          "没找到 `esc(s.dataset.secname || s.dataset.sec)`")

    # ---- ④ 五个板块的锚点与名称 ----
    print("\n[4] 五个板块")
    for key, nm in SECTIONS:
        check(f"「{nm}」= data-sec {key}",
              f'data-sec="{key}" data-secname="{nm}"' in src)
    # ★ 要查**渲染顺序**（renderAnswer 里的拼接次序），不是"在文件里出现的位置" ——
    #   板块分散在不同函数里定义，文件位置跟渲染顺序无关（我第一版就查错了）。
    ra = re.search(r"function renderAnswer\(d\)\{[\s\S]*?\n\}", src)
    body = ra.group(0) if ra else ""
    keys = ["sec-answer", "renderStandPanel(d)", "sec-proc", "renderMedia("]
    pos = [body.find(k) for k in keys]
    check("渲染顺序：答案 → 替身资料 → 检索过程 → 图片",
          bool(body) and all(p >= 0 for p in pos) and pos == sorted(pos),
          f"位置 {dict(zip(keys, pos))}")

    # ---- ⑤ 检索过程搬进结果区后的连带修改 ----
    print("\n[5] 「检索过程」搬进结果区")
    check("静态 markup 里不再有 procCard（改由 JS 生成）",
          'id="procCard"' in src and src.count('id="procCard"') == 1
          and 'style="display:none"' not in src)
    check("★ 先渲染答案再填检索过程（否则 #proc 还不存在）",
          bool(re.search(r"renderAnswer\(d\);\s*\n\s*renderProcess\(d\);", src)))
    check("#procCard 访问判空（它在 #ans 里，可能刚被清空）",
          "const _pc0 = $('#procCard');" in src
          and "if(_pc){ _pc.open = false;" in src)
    check("证据归「检索过程」板块",
          src.index('data-sec="sec-proc"') < src.index("${renderEvidence(d.evidence)}"))

    # ---- ⑥ 端到端：服务端返回的页面里这些都在 ----
    print(f"\n[6] 端到端（127.0.0.1:{PORT}）")
    import subprocess
    log = (ROOT / ".tmp_preview" / "_m57_server.log").open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "api" / "main.py"),
         "--port", str(PORT), "--no-vector"],
        cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL)
    try:
        if not wait_http(PORT):
            check("服务就绪", False, "超时")
            return 1
        c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=30)
        try:
            c.request("GET", "/")
            r = c.getresponse()
            body = r.read().decode("utf-8", "ignore")
        finally:
            c.close()
        check("/ 返回 200", r.status == 200, f"HTTP {r.status}")
        check("页面里有左右两列结构",
              'id="resToc"' in body and 'id="resMain"' in body)
        check("页面里有全部五个板块锚点",
              all(f'data-sec="{k}"' in body for k, _ in SECTIONS))
        check("顶栏版本号已注入（不是占位符）",
              bool(re.search(r'id="webBuild">\d{2}-\d{2} \d{2}:\d{2}<', body)))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except Exception:  # noqa: BLE001
            proc.kill()
        log.close()

    print()
    if fails:
        print(f"✗ {len(fails)}/{n} 项失败：" + "、".join(fails))
        return 1
    print(f"✓ 全部通过（{n} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

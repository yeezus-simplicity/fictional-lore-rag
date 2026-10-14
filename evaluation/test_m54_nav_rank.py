"""M54 回归测试：① 无限算最高 ② 答案置顶 ③ 左侧导航栏。

用户一次提了三件事：
  1. 「A」——「∞」在极值排名里算最高（我上一轮给了 A/B 两个选项，他选了 A）
  2. 「帮我把答案调到替身档案之上 让人一下子就能看到答案」
  3. 「然后帮我做个左侧导航栏」

== ① 无限算最高 ==
  实测（M53 发现的同源问题）：
    「速度最高的是谁」**排除了速度无限的天堂制造**，返回 A 级的 Ball Breaker。
  原因：`_extreme` 的 SQL 用 `{dim} IS NOT NULL` + `composite IS NOT NULL`
  过滤，而 ∞ 的数值列是 NULL（放不进 1..5）→ 整行被排除；
  即使不过滤，`ORDER BY {dim} DESC` 也没法把它排到 A 之前。
  做法：
    · top-N 与 max/min(降序) 两条 SQL 都放宽为
      `(composite IS NOT NULL OR {d}_cat='INFINITE')`
      `AND ({d} IS NOT NULL OR {d}_cat='INFINITE')`
    · `ORDER BY ({d}_cat='INFINITE') DESC NULLS LAST, {d} DESC NULLS LAST`
      ★ Postgres 的 DESC **默认 NULLS FIRST** —— 不显式写 NULLS LAST，
        ∞ 那一行的数值 NULL 反而会排到最前面。
    · 值用 level_label → '∞'；并加 caveat 说明"它不是一个等级数字"
    · **ASC（最低）分支保持排除** —— ∞ 不是「最低」
    · 前端给 ∞ 条目标 `c-inf` 类 + 名字后加「∞」，否则"∞ 排第一"像排序出错

== ② 答案置顶 ==
  原顺序 [档案+雷达] → [答案(含图片/证据)]，答案要滚动才看得到。
  新顺序 [答案] → [档案+雷达] → [图片+证据] → [相似替身]。
  图片与证据从答案卡里分出来单独成卡（它们各有标题，且会让答案卡过长）。

== ③ 左侧导航栏 ==
  原顶部 header（横条 nav）→ 左侧竖向栏：品牌 / 导航（带一行说明）/ 底部状态。
  ★ `#dot` / `#dotTxt` 的 id 必须保留 —— ping() 靠它们更新服务状态；
    另外 ping() 现在还往 `#sideStat` 写一行规模数字（替身数 / 语料块数）。
  ★ 窄屏（≤900px）侧栏转回顶部横条，行为与改造前等价。
"""
from __future__ import annotations

import http.client
import json
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "api" / "static" / "index.html"
EXE = ROOT / "services" / "executor.py"
PROC = ROOT / "dataset" / "processed"
PORT = 8798
PY = sys.executable


def wait_ready(port: int, timeout: float = 240) -> bool:
    """等到**能真正应答**为止。

    ★ 实测踩坑：uvicorn「先 bind 端口，再跑启动事件」——
      只探测 TCP 端口会「假就绪」，请求拿不到响应，
      失败信息还会误导（表现为"响应体不对"而不是"服务没起来"）。
      → 轮询 /health 拿到 200 才算就绪；并给服务加 --no-vector（与向量无关）。
    """
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


def query(q: str, port: int) -> dict:
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=150)
    try:
        c.request("POST", f"/query?q={quote(q)}&top_k=3",
                  headers={"Content-Type": "application/json"})
        return json.loads(c.getresponse().read())
    finally:
        c.close()


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
    exe = EXE.read_text(encoding="utf-8")

    print("=" * 74)
    print("M54：无限算最高 / 答案置顶 / 左侧导航栏")
    print("=" * 74)

    # ---- ① 左侧导航栏（静态） ----
    print("\n[1] 左侧导航栏")
    check("有 .app 两列容器", bool(re.search(r"\.app\s*\{[^}]*display:\s*flex", css)))
    check("有 .side 侧栏且 sticky 常驻",
          bool(re.search(r"\.side\s*\{[^}]*position:\s*sticky", css)))
    check("侧栏有固定宽度",
          bool(re.search(r"\.side\s*\{[^}]*flex:\s*0 0 \d+px", css)))
    check("旧的 <header> 已移除", "<header>" not in src)
    check("导航按钮在 .side nav 内（切换逻辑靠 'nav button' 选择器）",
          bool(re.search(r'<aside class="side">[\s\S]*?<nav>[\s\S]*?data-v="q"', src)))
    check("4 个视图按钮都在", all(f'data-v="{v}"' in src for v in "qbcs"))
    check("★ #dot / #dotTxt 仍保留（ping 靠它们更新状态）",
          'id="dot"' in src and 'id="dotTxt"' in src)
    check("侧栏底部有规模数字位 #sideStat", 'id="sideStat"' in src)
    check("ping() 会填 #sideStat",
          bool(re.search(r"\$\('#sideStat'\)", src))
          and bool(re.search(r"st\.textContent\s*=[\s\S]{0,120}n_stands", src)),
          "没找到写入语句")
    check("窄屏（≤900px）侧栏转横条",
          bool(re.search(r"@media \(max-width: 900px\)[\s\S]{0,200}"
                         r"\.side\s*\{[\s\S]{0,200}position:\s*static", css)))
    check("窄屏隐藏每项的说明文字", ".nb { display: none; }" in css)

    # ---- ② 板块顺序（静态） ----
    #   ★ M55 把卡片重组成「板块」后，这里同步更新为新的意图：
    #     答案在最前（M54 的要求），证据归答案，图片自成板块。
    print("\n[2] 板块顺序")
    m = re.search(r"function renderAnswer\(d\)\{[\s\S]*?\n\}", src)
    check("找到 renderAnswer", bool(m))
    if m:
        body = m.group(0)
        i_ans = body.index("<h3>答案")
        i_panel = body.index("renderStandPanel(d)")
        i_media = body.index("renderMedia(d)")
        i_ev = body.index("renderEvidence(")
        i_close = body.index("html += '</div>'")
        check("答案在最前（档案与图片都在它之后）",
              i_ans < i_panel < i_media,
              f"答案 {i_ans} / 档案 {i_panel} / 图片 {i_media}")
        check("证据仍在答案那一块（它是这条答案的出处）",
              i_ev < i_close, f"证据 {i_ev} vs 答案卡闭合 {i_close}")
        check("图片在答案卡之外（自成一个板块）", i_media > i_close)

    # ---- ③ 极值排名含 ∞（静态） ----
    print("\n[3] 极值排名的 ∞ 处理")
    ex = re.search(r"def _extreme\([\s\S]*?\n    def ", exe)
    body = ex.group(0) if ex else ""
    check("找到 _extreme", bool(body))
    check("SQL 把 ∞ 纳入排名（cat = INFINITE）",
          "_cat = 'INFINITE'" in body)
    check("★ 显式 NULLS LAST（Postgres 的 DESC 默认 NULLS FIRST）",
          body.count("NULLS LAST") >= 3)
    check("∞ 的值用 level_label → '∞'",
          "level_label(r[2], _cat)" in body and "level_label(top, top_cat)" in body)
    check("并列项按 cat 判（避免 None == None 误并）",
          'r[3] == "INFINITE"' in body)
    check("有 caveat 说明「它不是一个等级数字」",
          "不是一个等级数字" in body or "不是等级数字" in body)
    check("ASC（最低）分支仍排除 ∞",
          body.count("WHERE composite IS NOT NULL AND") >= 1)
    check("前端给 ∞ 条目标 c-inf 类",
          "c-inf" in src and bool(re.search(r"\.tag\.c-inf\s*\{", css)))
    check("前端含 ∞ 兼容两种数据结构（infinite 标记 / cat 字段）",
          "x.infinite || x.cat === 'INFINITE'" in src)

    # ---- ④ 端到端：排名（起服务） ----
    print("\n[4] 端到端排名（127.0.0.1:%d）" % PORT)
    log = (ROOT / ".tmp_preview" / "_m54_server.log").open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [PY, str(ROOT / "api" / "main.py"), "--port", str(PORT), "--no-vector"],
        cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL)
    try:
        if not wait_ready(PORT):
            check("服务就绪", False, "超时")
            return 1
        print(f"  [OK] 已就绪")

        d = query("速度最高的是谁", PORT)
        a = d.get("answer")
        if isinstance(a, list):
            a = a[0] if a else None
        a = a if isinstance(a, dict) else {}
        items = a.get("items") or a.get("winners") or []
        names = [x.get("name") for x in items]
        labels = [x.get("label") for x in items]
        check("「速度最高的是谁」有结果", bool(items), f"type={a.get('type')}")
        check("★ 榜首是天堂制造（速度 ∞）",
              bool(items) and "Heaven" in str(names[0]),
              f"实际榜首 {names[:3]}")
        check("★ 榜首的值显示 ∞（不是 None）",
              bool(labels) and labels[0] == "∞", f"实际 {labels[:3]}")
        check("榜首条目带 ∞ 标记（前端会加 c-inf）",
              bool(items) and (items[0].get("infinite")
                               or items[0].get("cat") == "INFINITE"))
        check("有 caveat 解释为什么 ∞ 排在最前",
              "无限" in str(a.get("caveat") or "") or
              "无限" in str(a.get("caveat_infinite") or ""),
              f"caveat={a.get('caveat')!r}")
        check("A 级条目仍在榜上（不是只有 ∞）",
              any(l == "A" for l in labels), f"榜单 {list(zip(names, labels))[:6]}")

        d2 = query("速度最低的是谁", PORT)
        a2 = d2.get("answer")
        if isinstance(a2, list):
            a2 = a2[0] if a2 else None
        a2 = a2 if isinstance(a2, dict) else {}
        w2 = [x.get("name") for x in (a2.get("items") or a2.get("winners") or [])]
        check("★ 「速度最低的是谁」不含天堂制造（∞ 不算最低）",
              not any("Heaven" in str(x) for x in w2), f"实际 {w2[:5]}")

        d3 = query("破坏力最高的是谁", PORT)
        a3 = d3.get("answer")
        if isinstance(a3, list):
            a3 = a3[0] if a3 else None
        a3 = a3 if isinstance(a3, dict) else {}
        i3 = a3.get("items") or a3.get("winners") or []
        check("正常维度（破坏力最高）未被破坏：榜首是 A 级",
              bool(i3) and (i3[0].get("label") == "A"
                            or i3[0].get("cat") == "RANKED"),
              f"实际 {[(x.get('name'), x.get('label')) for x in i3[:3]]}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except Exception:
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

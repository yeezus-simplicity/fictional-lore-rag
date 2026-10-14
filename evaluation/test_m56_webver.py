"""M56 回归测试：让「网页还是没区别」这个问题**能自己暴露出来**。

用户反复反馈：「我的网页还是没区别 到底怎么完全刷新」。
而我这边 `curl http://127.0.0.1:8765/` 明明是新版（`<h3>图片`、`替身信息`、
`sideStat` 全在），响应头也带 `cache-control: no-cache`。

== 双方都无法判断是哪种情况 ==
  ① 浏览器缓存了旧页面（**在加 no-cache 头之前**就缓存过的副本，
     浏览器可能一直不回来验证）
  ② 打开的不是这个服务（旧进程 / 别的端口 / 别的浏览器）
  这两类**从界面上看不出来**，所以每次只能互相猜。

== 做法：把「构建号」印在页面上，并自动比对 ==
  1. `api/main.py` 的 `root()` 把响应体里的 `__WEB_BUILD__` 替换成
     `index.html` 的修改时间（MM-DD HH:MM）
  2. `/health` 返回同一个值 → 页面拿它跟**自己带的**那个比对
  3. 页面侧栏底部印出「网页版本 …」，并在两种异常下直接写出该怎么做：
     · 构建号仍是占位符 → 服务是旧进程（旧代码不做替换）→ 重启 start.bat
     · 与 /health 不一致   → 浏览器缓存了旧页面 → Ctrl+Shift+R / 加 ?v=2
  4. `start.py` 打开浏览器的 URL 带 `?v=<月日时分秒>` ——
     浏览器缓存**按完整 URL** 分，页面一改参数就变，缓存必然失效。
     ★ 这比"教用户按 Ctrl+Shift+R"可靠：那是要人记住的操作，
       而这是双击 bat 就自动生效的。

== 本测试守什么 ==
  1. 注入机制：`/` 返回的 HTML 里**没有** `__WEB_BUILD__`，且带上了构建号
  2. `/health` 的 `web_build` 与页面上的构建号**一致**
  3. 页面有 `#webBuild` 与 `#verWarn`，且 `checkWebBuild` 会区分两种异常
  4. `start.py` 的 `web_url()` 带 `?v=`，且浏览器打开处与"端口被占用"
     提示处**都用它**（漏一处就等于漏一条路）
  5. HTML 的占位符必须与 main.py 替换的字符串**逐字一致**
     （写成 `__WEB_BUILD_ _` 之类会静默不替换 —— 这类拼写 bug 不报错）
"""
from __future__ import annotations

import http.client
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "api" / "static" / "index.html"
PORT = 8799
PY = sys.executable


def wait_ready(port: int, timeout: float = 240) -> bool:
    """等到**能真正应答**为止（不是只等端口）。

    ★ 实测踩坑：uvicorn 是「先 bind 端口，再跑 lifespan 启动事件」——
      `api/main.py` 启动时要加载向量模型（约 2-3 分钟），
      这期间端口**已经能连上**，但请求不会得到响应。
      只探测 TCP 端口会「假就绪」，后面的断言全都在跟一个还没起好的服务说话，
      失败信息还很误导（表现为「响应体不对」而不是「没起来」）。
      → 必须轮询 /health 拿到 200 才算就绪。
      ★ 另外本测试给服务加了 `--no-vector`：这些断言与向量无关，没必要等模型。
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


def get(path: str, port: int) -> tuple[int, dict, str]:
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    try:
        c.request("GET", path)
        r = c.getresponse()
        return r.status, dict(r.getheaders()), r.read().decode("utf-8", "ignore")
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
    mainpy = (ROOT / "api" / "main.py").read_text(encoding="utf-8")
    startpy = (ROOT / "start.py").read_text(encoding="utf-8")

    print("=" * 74)
    print("M56：网页构建号自检（解决「网页还是没区别」）")
    print("=" * 74)

    # ---- ① 占位符一致性（拼错就静默不替换） ----
    print("\n[1] 占位符一致性")
    n_html = src.count("__WEB_BUILD__")
    # ★★ 必须**只出现一次**（那个 <b> 里）★★
    #   服务端是"文本替换"，占位符出现在别处就会被一起改写。
    #   实测踩坑：它曾出现在 JS 字面量 `mine === '__WEB_BUILD__'` 里，
    #   被替换成 `mine === '10-14 18:17'` → 页面把正确的构建号判成
    #   "没被替换"，自己误报旧版本。
    check(f"★ HTML 里占位符**恰好出现一次**（实测 {n_html}）", n_html == 1)
    check("★ JS 里没有该占位符字面量（否则会被一起改写）",
          src.count("__WEB_BUILD__") == src.count('id="webBuild">__WEB_BUILD__<'))
    check("main.py 替换的是**同一个**字符串，且只换第一次",
          '"__WEB_BUILD__"' in mainpy and '_web_build(), 1)' in mainpy)
    check("判断「没被替换」用的是格式校验（不依赖会被改写的字面量）",
          "looksLikeStamp" in src and "test(mine)" in src)
    check("页面上有 #webBuild 与 #verWarn",
          'id="webBuild"' in src and 'id="verWarn"' in src)

    # ---- ② 页面自检逻辑 ----
    print("\n[2] 页面自检逻辑")
    check("有 checkWebBuild 函数", "function checkWebBuild(" in src)
    check("区分「服务是旧进程」（占位符没被替换）",
          "服务是旧进程" in src)
    check("区分「缓存了旧页面」（与 /health 不一致）",
          "缓存的旧页面" in src)
    check("启动时先做一次不依赖网络的自检", re.search(
        r"checkWebBuild\(null\);\s*\nping\(\);", src) is not None)
    check("ping 里用服务器返回值比对", "checkWebBuild(d.web_build)" in src)
    check("提示里给出可操作的解法（快捷键 / ?v=）",
          "Ctrl+Shift+R" in src and "?v=2" in src)

    # ---- ③ start.py 的缓存失效 URL ----
    print("\n[3] start.py：打开的 URL 必须带构建号")
    check("有 web_url() 且带 ?v=", "def web_url(" in startpy and "?v=" in startpy)
    check("自动开浏览器处用它", "url = web_url(port)" in startpy)
    check("「端口被占用」提示处也用它（漏一处就漏一条路）",
          "print(f\"    {web_url(port)}\")" in startpy
          or "{web_url(port)}" in startpy)

    # ---- ④ 端到端：注入是否真的生效 ----
    print(f"\n[4] 端到端注入（127.0.0.1:{PORT}）")
    log = (ROOT / ".tmp_preview" / "_m56_server.log").open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [PY, str(ROOT / "api" / "main.py"), "--port", str(PORT), "--no-vector"],
        cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL)
    try:
        if not wait_ready(PORT):
            check("服务就绪", False, "超时")
            return 1
        print("  [OK] 已就绪")

        st, hdrs, body = get("/", PORT)
        check("/ 返回 200", st == 200, f"HTTP {st}")
        check("★ 响应体里**没有**占位符（说明注入生效）",
              "__WEB_BUILD__" not in body)
        m = re.search(r'id="webBuild">([^<]+)<', body)
        build = (m.group(1).strip() if m else "")
        check("★ 页面上的构建号是时间格式（MM-DD HH:MM）",
              bool(re.match(r"\d{2}-\d{2} \d{2}:\d{2}$", build)),
              f"实际 {build!r}")
        check("仍然带 no-cache（M50 的约束不能丢）",
              "no-cache" in (hdrs.get("cache-control") or ""),
              str(hdrs.get("cache-control")))

        st2, _, hb = get("/health", PORT)
        h = json.loads(hb)
        check("★ /health 也返回 web_build", "web_build" in h, str(sorted(h)))
        check("★ 两者一致（页面的自检才能工作）",
              h.get("web_build") == build,
              f"页面 {build!r} vs health {h.get('web_build')!r}")

        # 用 ?v= 访问也必须正常（start.py 打开的就是这种 URL）
        st3, _, b3 = get("/?v=1014181701", PORT)
        check("带 ?v= 的 URL 也能正常打开", st3 == 200 and "<h3>答案" in b3)
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

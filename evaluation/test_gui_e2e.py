"""M18 GUI 回归测试：真浏览器验证前端交互（不只是查接口）。

★★ 为什么必须有这个 ★★
M18 之前一直以为前端没问题，直到用户反馈"界面看不到图片"。
真浏览器一跑才发现：**查询按钮 #qb 从未绑定 click 事件**
—— 填好问句点「查询」，页面连"检索中…"都不显示，
因为根本没发请求（只有示例 chip 用了 inline onclick）。

★ 所以：**接口全绿 ≠ 前端能��**。
   前端必须用真浏览器点一遍、截图看渲染结果。
"""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORT = 8794
PY = sys.executable
OUT = ROOT / ".tmp_preview"


def wait_ready(port: int, timeout: float = 90) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = socket.socket()
        s.settimeout(1.5)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except OSError:
            time.sleep(2)
        finally:
            s.close()
    return False


def main() -> int:
    OUT.mkdir(exist_ok=True)
    fails: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}"
              f"{'' if ok else '  ← ' + detail}")
        if not ok:
            fails.append(name)

    log = open(OUT / "_gui_test.log", "wb")
    proc = subprocess.Popen(
        [PY, str(ROOT / "api" / "main.py"), "--port", str(PORT),
         "--no-vector", "--chunk-merge", "512"],
        cwd=str(ROOT), stdout=log, stderr=log, stdin=subprocess.DEVNULL)
    try:
        if not wait_ready(PORT):
            print("服务超时")
            return 1
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            print("playwright 未安装，跳过（接口测试已覆盖）")
            return 0

        with sync_playwright() as pw:
            br = pw.chromium.launch()
            pg = br.new_page(viewport={"width": 1150, "height": 950})
            errs: list[str] = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.on("console", lambda m: errs.append(m.text)
                  if m.type == "error" else None)
            pg.goto(f"http://127.0.0.1:{PORT}/",
                    wait_until="domcontentloaded")

            # ==== 1) 查询按钮真的绑了事件吗（M18 的核心 bug）====
            print("[1] 查询按钮 / 回车 绑定（M18 修的核心 bug）")
            bound = pg.evaluate(
                "() => { const b=document.querySelector('#qb');"
                " return b ? (typeof b.onclick === 'function') : false; }")
            check("#qb.onclick 已绑定", bound is True, "按钮点了没反应")

            # 点按钮 → 必须出现答案卡
            pg.fill("#qi", "Star Platinum 的破坏力是几级？")
            pg.click("#qb")
            try:
                pg.wait_for_selector("#ans .card", timeout=20000)
                check("点按钮 → 答案卡出现", True)
            except Exception:
                check("点按钮 → 答案卡出现", False,
                      "点了没反应（事件没绑定？）")
                pg.screenshot(path=str(OUT / "btn_fail.png"),
                              full_page=True)
                br.close()
                return 1

            # ==== 2) 回车也能提交 ====
            print("\n[2] 回车提交")
            pg.fill("#qi", "Tusk 的射程是多少")
            pg.press("#qi", "Enter")
            try:
                pg.wait_for_function(
                    "() => document.querySelector('#ans .card')"
                    "?.textContent.includes('Tusk')",
                    timeout=20000)
                check("回车 → 答案更新为 Tusk", True)
            except Exception:
                check("回车 → 答案更新为 Tusk", False, "回车没提交")

            # ==== 3) 雷达图渲染 ====
            print("\n[3] 雷达图与图片区")
            has_svg = pg.evaluate(
                "() => !!document.querySelector('#ans .radar-wrap svg')")
            check("雷达图 SVG 已渲染", has_svg)

            # ==== 4) 图片：已缓存替身应直接出图 ====
            print("\n[4] 已缓存替身 → 直接出图（不等后台）")
            got_shot = False
            try:
                pg.wait_for_selector("#imgDone .img-grid img", timeout=25000)
                n = pg.eval_on_selector_all(
                    "#imgDone .img-grid img", "e => e.length")
                check(f"已缓存替身图片直接显示（{n} 张）", n > 0)
                got_shot = True
            except Exception:
                check("已缓存替身图片直接显示", False,
                      "等 25 秒仍未出现")
            # 实际加载成功（不是 broken image）
            if got_shot:
                # ★★ 必须先把图片滚进视口 ★★
                #   <img loading="lazy"> 在视口外不会加载，
                #   此时查 naturalWidth 恒为 0 → 会误报成"图片加载失败"
                #   （实测 /images/tusk/anime_3.jpg 直连是 200 + 合法 JPEG，
                #     纯粹是懒加载没进视口）。
                pg.eval_on_selector(
                    "#imgDone", "e => e.scrollIntoView({block:'center'})")
                pg.wait_for_timeout(3500)     # 等懒加载触发
                detail = pg.eval_on_selector_all(
                    "#imgDone .img-grid img",
                    "els => els.map(e => ({src:e.getAttribute('src'),"
                    " w:e.naturalWidth, complete:e.complete}))")
                bad = [d for d in detail if d["w"] == 0]
                ok = not bad
                check(f"所有 <img> 真实加载成功（{len(detail)} 张）", ok,
                      f"{len(bad)} 张未加载：" +
                      "; ".join(f"{d['src']}(w={d['w']},"
                                f"complete={d['complete']})"
                                for d in bad[:3]))

            # ==== 5) 无JS 错误 ====
            print("\n[5] 控制台无错误")
            check("无 pageerror/console.error", not errs,
                  "; ".join(errs[:3]))

            pg.screenshot(path=str(OUT / "m18_gui_regress.png"),
                          full_page=True)
            print(f"\n截图 {OUT/'m18_gui_regress.png'}")
            br.close()
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=20)
        except Exception:
            proc.kill()

    print("\n" + "=" * 62)
    if fails:
        print(f"✗ {len(fails)} 项失败：{fails}")
        return 1
    print("★ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
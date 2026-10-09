"""生成 README 用的截图（M26）。

★ 为什么不用测试产物里的图：那些是**测试证据**，
  命名混乱（m18/m19/m20…）、有的已过时、尺寸也不适合 README。
  这里专门截三张「门面图」：主问答 / 冲突消解 / 多轮追问。

★ 体积控制：输出到 docs/screenshots/（会入库），
  单张尽量 < 250 KB。
"""
from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORT = 8781
OUT = ROOT / "docs" / "screenshots"
PY = sys.executable


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
    OUT.mkdir(parents=True, exist_ok=True)
    log = open(ROOT / "images" / "_shots.log", "wb")
    proc = subprocess.Popen(
        [PY, "-u", str(ROOT / "api" / "main.py"), "--port", str(PORT),
         "--no-vector", "--chunk-merge", "512"],
        cwd=str(ROOT), stdout=log, stderr=log, stdin=subprocess.DEVNULL)
    try:
        if not wait_ready(PORT):
            print("服务超时")
            return 1
        print(f"服务就绪 :{PORT}")

        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            br = pw.chromium.launch()
            pg = br.new_page(viewport={"width": 1080, "height": 900},
                             device_scale_factor=1.5)
            pg.goto(f"http://127.0.0.1:{PORT}/", wait_until="domcontentloaded")
            pg.wait_for_timeout(700)

            # ---------- 1. 主问答（雷达图 + 图片）----------
            print("\n[1] 主问答")
            pg.fill("#qi", "白金之星的六维能力")
            pg.click("#qb")
            pg.wait_for_selector("#ans .card", timeout=40000)
            # ★ 前五版都在**手算裁切坐标**上翻车（视口/页面坐标混用、
            #   滚动导致 lazy 图卸载、布局变化让 scrollY 失效）。
            #   最终做法：先 scrollIntoView 把 lazy 图催出来，
            #   再用 element.bounding_box() —— 它直接给**页面坐标**，
            #   不需要我自己加 scrollY（那正是之前算错的根源）。
            pg.evaluate("""()=>{const a=document.querySelector('#ans');
                            if(a) a.scrollIntoView({block:'start'});}""")
            pg.wait_for_timeout(3000)
            el = pg.query_selector("#ans")
            bb = el.bounding_box()
            dbg = pg.evaluate("""()=>{
              const a=document.querySelector('#ans');
              return {h: a? Math.round(a.getBoundingClientRect().height):0,
                      nImg: document.querySelectorAll('#ans img').length,
                      nLoaded: [...document.querySelectorAll('#ans img')]
                               .filter(i=>i.naturalWidth>0).length};
            }""")
            print(f"      #ans 高 {dbg['h']}px，图片 {dbg['nLoaded']}/"
                  f"{dbg['nImg']} 已加载")
            # 只取上半部分（答案 + 雷达图）—— 整张 2000+px 长图不适合放 README
            h = min(bb["height"], 1150)
            pg.screenshot(path=str(OUT / "qa.png"),
                          clip={"x": bb["x"], "y": bb["y"],
                                "width": bb["width"], "height": h})
            print(f"      截取高度 {h:.0f}px，"
                  f"qa.png {(OUT/'qa.png').stat().st_size/1024:.0f} KB")

            # ---------- 2. 多轮追问 ----------
            print("\n[2] 多轮追问")
            pg.fill("#qi", "空条承太郎的替身是什么")
            pg.click("#qb")
            pg.wait_for_timeout(3500)
            pg.fill("#qi", "那它的速度呢")
            pg.click("#qb")
            pg.wait_for_timeout(4000)
            el = pg.query_selector("#rout")
            el.screenshot(path=str(OUT / "multiturn.png"))
            print(f"      multiturn.png "
                  f"{(OUT/'multiturn.png').stat().st_size/1024:.0f} KB")

            # ---------- 3. 冲突消解对照卡 ----------
            print("\n[3] 冲突消解")
            pg.click('button[data-v="c"]')
            pg.wait_for_selector(".cf-card", timeout=25000)
            pg.wait_for_timeout(900)
            # 找一张「已消解」的（右边那张展示了采纳理由与敏感性）
            cards = pg.query_selector_all(".cf-card")
            target = None
            for c in cards:
                cls = c.get_attribute("class") or ""
                if "is-pending" not in cls:
                    target = c
                    break
            (target or cards[0]).screenshot(path=str(OUT / "conflicts.png"))
            print(f"      conflicts.png "
                  f"{(OUT/'conflicts.png').stat().st_size/1024:.0f} KB")
            br.close()
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=20)
        except Exception:
            proc.kill()
        log.close()

    print("\n生成完毕：")
    for f in sorted(OUT.glob("*.png")):
        print(f"  {f.relative_to(ROOT)}  {f.stat().st_size/1024:.0f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
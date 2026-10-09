"""M32 浏览器验证：中文摘要卡 / 英文折叠 / 相似替身 / 多语言查询。

★ 接口全绿 ≠ 前端能用（M17/M22 栽过两次）。
  这里必须真浏览器点一遍并截图核对。
"""
from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORT = 8785
OUT = ROOT / ".tmp_preview"
PY = sys.executable
fails: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}"
          f"{'' if ok else '  ← ' + detail}")
    if not ok:
        fails.append(name)


def wait_ready(port: int, timeout: int = 90) -> bool:
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
    log = open(ROOT / "images" / "_m32ui.log", "wb")
    proc = subprocess.Popen(
        [PY, "-u", str(ROOT / "api" / "main.py"), "--port", str(PORT),
         "--no-vector", "--chunk-merge", "512"],
        cwd=str(ROOT), stdout=log, stderr=log, stdin=subprocess.DEVNULL)
    try:
        if not wait_ready(PORT):
            print("服务超时")
            return 1
        print(f"服务就绪 :{PORT}\n")

        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            br = pw.chromium.launch()
            pg = br.new_page(viewport={"width": 1150, "height": 900})
            errs: list[str] = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.goto(f"http://127.0.0.1:{PORT}/", wait_until="domcontentloaded")
            pg.wait_for_timeout(700)

            print("[1] 中文摘要卡（问一个替身，答案不该只有英文）")
            pg.fill("#qi", "白金之星的能力")
            pg.click("#qb")
            pg.wait_for_selector("#ans .card", timeout=40000)
            pg.wait_for_timeout(3000)
            n_card = pg.eval_on_selector_all(".zh-card", "els=>els.length")
            check("出现中文摘要卡", n_card > 0, f"{n_card}")
            if n_card:
                txt = pg.eval_on_selector_all(
                    ".zh-card", "els=>els.map(e=>e.textContent).join('')")
                print(f"      卡片内容: {txt[:110].strip()}")
                check("卡片含中文名「白金之星」", "白金之星" in txt, txt[:60])
                check("卡片含日文原名", "スタープラチナ" in txt, txt[:60])
                check("卡片含六维中文等级",
                      "破坏力" in txt and "速度" in txt, txt[:60])
                check("卡片含使用者", "Jotaro" in txt or "承太郎" in txt,
                      txt[:60])

            print("\n[2] 英文原文折叠（默认展开但可收起）")
            n_fold = pg.eval_on_selector_all(".en-fold", "els=>els.length")
            check("出现英文折叠块", n_fold > 0, f"{n_fold}")
            if n_fold:
                s = pg.eval_on_selector_all(
                    ".en-fold summary", "els=>els.map(e=>e.textContent).join('')")
                print(f"      折叠标题: {s.strip()[:70]}")
                check("标题说明了来源", "jojowiki" in s or "英文" in s, s[:50])

            print("\n[3] 相似替身（异步加载）")
            pg.wait_for_timeout(2500)
            n_sim = pg.eval_on_selector_all(
                "#simBox .sim-item", "els=>els.length")
            print(f"      相似替身条数: {n_sim}")
            check("出现相似替身卡片", n_sim > 0, f"{n_sim}")
            if n_sim:
                st = pg.eval_on_selector_all(
                    "#simBox", "els=>els.map(e=>e.textContent).join('')")
                print(f"      内容: {st[:130].strip()}")
                check("含「距离」与「共同维度」",
                      "距离" in st and "共同维度" in st, st[:70])
                check("说明了这是数值相似（非设定相似）",
                      "数值" in st, st[:80])
                # ★ 可点击 → 触发新查询
                pg.click("#simBox .sim-item")
                pg.wait_for_timeout(3500)
                t2 = pg.eval_on_selector_all(
                    "#ans", "els=>els.map(e=>e.textContent).join('')")
                check("点击相似替身能触发新查询",
                      len(t2) > 30, f"{len(t2)} 字符")
            pg.screenshot(path=str(OUT / "m32_zh_card.png"), full_page=True)

            print("\n[4] 多语言查询（日语 / 繁体）")
            for q, desc in (("スタープラチナの破壊力", "全日文"),
                            ("白金之星的破壞力", "繁体")):
                pg.fill("#qi", q)
                pg.click("#qb")
                pg.wait_for_timeout(3200)
                rt = pg.eval_on_selector_all(
                    "#rout", "els=>els.map(e=>e.textContent).join('')")
                a = pg.eval_on_selector_all(
                    "#ans", "els=>els.map(e=>e.textContent).join('')")
                ok = "abstain" not in rt and len(a) > 10
                print(f"      [{desc}] {q} → {a[:40].strip()}")
                check(f"{desc}查询不再被拒答", ok, rt[:60])

            check("无 JS 运行时错误", not errs, str(errs[:2]))
            br.close()
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=20)
        except Exception:
            proc.kill()
        log.close()

    print("\n" + "=" * 62)
    if fails:
        print(f"✗ {len(fails)} 项失败：{fails}")
        return 1
    print("★ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
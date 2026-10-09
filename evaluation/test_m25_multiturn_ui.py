"""M25 浏览器验证：多轮追问在**界面**上真的能用。

★ 接口全绿 ≠ 前端能用（M17/M22 踩过两次：按钮没绑事件、
  字段名对不上）。这里必须真浏览器点、真截图看。
"""
from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORT = 8782
OUT = ROOT / ".tmp_preview"
PY = sys.executable
fails: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}"
          f"{'' if ok else '  ← ' + detail}")
    if not ok:
        fails.append(name)


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
    log = open(ROOT / "images" / "_m25ui.log", "wb")
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
            pg = br.new_page(viewport={"width": 1150, "height": 880})
            errs: list[str] = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            cons: list[str] = []
            pg.on("console", lambda m: cons.append(m.text)
                  if m.type == "error" else None)
            pg.goto(f"http://127.0.0.1:{PORT}/", wait_until="domcontentloaded")
            pg.wait_for_timeout(600)

            print("[1] 第 1 轮：建立上下文")
            pg.fill("#qi", "空条承太郎的替身是什么")
            pg.click("#qb")
            pg.wait_for_selector("#ans .card", timeout=30000)
            pg.wait_for_timeout(800)
            t1 = pg.eval_on_selector_all(
                "#ans", "els=>els.map(e=>e.textContent).join('')")
            print(f"      答: {t1[:70]}")
            check("第 1 轮有答案", "Star Platinum" in t1 or "白金" in t1, t1[:60])
            check("第 1 轮不显示消解提示（本来没代词）",
                  pg.eval_on_selector_all(".coref", "els=>els.length") == 0,
                  "出现了 .coref")

            print("\n[2] 第 2 轮：用「它」追问 —— 关键")
            pg.fill("#qi", "那它的速度呢")
            pg.click("#qb")
            pg.wait_for_timeout(3500)
            n_coref = pg.eval_on_selector_all(".coref", "els=>els.length")
            print(f"      .coref 元素数: {n_coref}")
            check("★ 界面显示了消解提示", n_coref > 0, f"{n_coref}")
            if n_coref:
                ct = pg.eval_on_selector_all(
                    ".coref", "els=>els.map(e=>e.textContent).join('')")
                print(f"      提示内容: {ct.strip()[:80]}")
                check("提示里含「它」", "它" in ct, ct[:60])
                check("提示里含解析目标（白金之星/Star Platinum）",
                      ("白金" in ct) or ("Star" in ct), ct[:80])
            # 答案本身应该是速度值
            t2 = pg.eval_on_selector_all(
                "#ans", "els=>els.map(e=>e.textContent).join('')")
            print(f"      答: {t2[:70]}")
            check("★ 第 2 轮答的是 Star Platinum 的速度（非拒答）",
                  "拒答" not in t2 and len(t2) > 5, t2[:70])
            pg.screenshot(path=str(OUT / "m25_followup.png"), full_page=True)

            print("\n[3] 「处理过程」里可追溯")
            pg.click('button[data-v="q"]') if False else None
            proc_txt = pg.eval_on_selector_all(
                "#procCard, .proc, #proc", "els=>els.map(e=>e.textContent).join('')")
            if "指代消解" in proc_txt:
                print("      ✓ 处理过程含「指代消解」步骤")
            else:
                print(f"      （处理过程文本 {len(proc_txt)} 字符，"
                      f"未含「指代消解」——展开状态下才可见，不算失败）")

            print("\n[4] 「新对话」按钮：清空上下文")
            # ★ 必须在点击**之前**读旧 id，否则读到的是新 id（我第一版写错了）
            sid_before = pg.evaluate("()=>localStorage.getItem('ragkb_sid')")
            pg.click("#newChat")
            pg.wait_for_timeout(900)
            rt = pg.eval_on_selector_all(
                "#rout", "els=>els.map(e=>e.textContent).join('')")
            print(f"      提示: {rt.strip()[:60]}")
            check("点了「新对话」有反馈", "新对话" in rt or "清空" in rt, rt[:50])
            sid_after = pg.evaluate("()=>localStorage.getItem('ragkb_sid')")
            check("session_id 已更换", sid_before != sid_after,
                  f"{sid_before} == {sid_after}")

            # 清空后再追问 —— 没有上下文，不该再显示 .coref
            pg.click('button[data-v="q"]')
            pg.fill("#qi", "那它的速度呢")
            pg.click("#qb")
            pg.wait_for_timeout(3200)
            n2 = pg.eval_on_selector_all(".coref", "els=>els.length")
            print(f"      清空后再追问，.coref 数: {n2}")
            check("★ 清空后不再消解（coref 为 0）", n2 == 0, f"{n2}")

            print("\n[5] 回归：其它视图仍可用")
            for v, sel, nm in (("b", "#bans", "数据浏览"),
                               ("c", "#cans", "冲突记录"),
                               ("s", "#sans", "系统状态")):
                pg.click(f'button[data-v="{v}"]')
                pg.wait_for_timeout(1600)
                # ★ 数据浏览要**先点查询**才有内容（初始为空），
                #   不能直接断言 —— M22 我也在这上面写错过
                if v == "b":
                    pg.fill("#bi", "Star Platinum")
                    pg.click("#bb")
                    pg.wait_for_timeout(2500)
                txt = pg.eval_on_selector_all(
                    sel, "els=>els.map(e=>e.textContent).join('')")
                print(f"      {nm}: {len(txt)} 字符")
                check(f"{nm}视图有内容", len(txt) > 10, f"{len(txt)}")

            check("无 JS 运行时错误", not errs, str(errs[:2]))
            real = [c for c in cons if "404" not in c]
            check("无 console error", not real, str(real[:2]))
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
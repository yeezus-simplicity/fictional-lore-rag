"""M19 验证：缩略图是否完整显示（不裁切）。

★ 为什么必须截图看 ★★
这是纯视觉问题（object-fit / 卡片高度），
接口和 DOM 断言全都测不出来 —— 必须看渲染结果。

做法：起真服务 → 真浏览器 → 问一个已缓存的替身 → 滚到图片区 →
  ① 断言每张图object-fit=contain（不是 cover）
  ② 断言卡片高度与图片真实宽高比一致（证明没裁切也没压扁）
  ③ 截图肉眼核对
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORT = 8792
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

    log = open(OUT / "_m19.log", "wb")
    proc = subprocess.Popen(
        [PY, str(ROOT / "api" / "main.py"), "--port", str(PORT),
         "--no-vector", "--chunk-merge", "512"],
        cwd=str(ROOT), stdout=log, stderr=log, stdin=subprocess.DEVNULL)
    try:
        if not wait_ready(PORT):
            print("服务超时")
            return 1
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            br = pw.chromium.launch()
            pg = br.new_page(viewport={"width": 1150, "height": 950})
            errs: list[str] = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.goto(f"http://127.0.0.1:{PORT}/", wait_until="domcontentloaded")

            # 用已缓存的替身（tusk 有 11 张，比例多样）
            pg.fill("#qi", "Tusk 的能力")
            pg.click("#qb")
            pg.wait_for_selector("#imgDone .img-grid img", timeout=30000)
            pg.eval_on_selector("#imgDone",
                                "e => e.scrollIntoView({block:'start'})")
            pg.wait_for_timeout(4000)

            n = pg.eval_on_selector_all("#imgDone .img-grid img",
                                        "e => e.length")
            check(f"图片已显示（{n} 张）", n > 0)
            if n == 0:
                br.close()
                return 1

            # ① 必须是 contain（完整显示），不能是 cover（裁切）
            fits = pg.eval_on_selector_all(
                "#imgDone .img-grid img",
                "els => [...new Set(els.map(e => getComputedStyle(e)"
                ".objectFit))]")
            check(f"object-fit = contain（不裁切）", fits == ["contain"],
                  f"实际={fits}")

            # ② 卡片高度是否与真实宽高比吻合（允许 24px 容差：图注/边框）
            detail = pg.eval_on_selector_all(
                "#imgDone .img-grid img",
                """els => els.map(e => {
                      const w = e.naturalWidth, h = e.naturalHeight;
                      const boxW = e.clientWidth;
                      const want = w ? Math.round(boxW * h / w) : 0;
                      return {src:e.getAttribute('src'), nat:`${w}x${h}`,
                              boxW, cssH:e.clientHeight, want,
                              ratio: w ? +(w/h).toFixed(2) : 0};
                    })""")
            tall = [d for d in detail if d["ratio"] < 0.95]   # 竖图
            wide = [d for d in detail if d["ratio"] > 1.2]   # 横图
            # ★ 下限 96px / 上限 420px —— 越界的按夹取后的值判定
            LOW, HIGH = 96, 420
            bad_fit = [d for d in detail if d["want"]
                       and abs(d["cssH"] - max(LOW, min(HIGH, d["want"]))) > 24]
            check(f"卡片高度匹配图片比例（{len(tall)} 竖 / {len(wide)} 横）",
                  not bad_fit,
                  "不匹配：" + "; ".join(
                      f"{d['src']}(真实{d['nat']} 期望"
                      f"{max(LOW, min(HIGH, d['want']))} 实际{d['cssH']})"
                      for d in bad_fit[:3]))

            # 竖图应该明显比横图高（说明没被压成同高）
            if tall:
                h_tall = sum(d["cssH"] for d in tall) / len(tall)
                h_wide = (sum(d["cssH"] for d in wide) / len(wide)) if wide else 0
                check(f"竖图卡片更高（竖均 {h_tall:.0f}px vs "
                      f"横均 {h_wide:.0f}px）",
                      not wide or h_tall > h_wide,
                      "竖图没有被区别对待")
            # ★ 横图不该被撑出大片空白：高度不应远大于其按比例应得的
            if wide:
                waste = [d for d in wide
                         if d["want"] > 0 and d["cssH"] > d["want"] + 24]
                check("横图无多余留白（高度≈按比例）", not waste,
                      "; ".join(f"{d['src']}(期望{d['want']} "
                                f"实际{d['cssH']})" for d in waste[:3]))

            # ③ 完整性：图片实际渲染区域应等于 contain 后的可见尺寸
            check("无 JS 错误", not errs, "; ".join(errs[:2]))

            pg.screenshot(path=str(OUT / "m19_thumbs.png"), full_page=True)
            print(f"\n截图 {OUT/'m19_thumbs.png'}")
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
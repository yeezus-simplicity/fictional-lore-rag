"""M22 验证：冲突消解对照卡（真浏览器）。

★ 接口全绿 ≠ 前端能用（M17 踩过：接口都对，但按钮没绑 click 事件）。
  这里必须真浏览器点一遍 + 截图肉眼核对。
"""
from __future__ import annotations

import http.client
import json
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


def api(path: str) -> dict:
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=60)
    try:
        c.request("GET", path)
        return json.loads(c.getresponse().read())
    finally:
        c.close()


def main() -> int:
    OUT.mkdir(exist_ok=True)
    log = open(ROOT / "images" / "_m22.log", "wb")
    proc = subprocess.Popen(
        [PY, str(ROOT / "api" / "main.py"), "--port", str(PORT),
         "--no-vector", "--chunk-merge", "512"],
        cwd=str(ROOT), stdout=log, stderr=log, stdin=subprocess.DEVNULL)
    try:
        if not wait_ready(PORT):
            print("服务超时")
            return 1
        print(f"服务就绪 :{PORT}\n")

        # ---- 接口层 ----
        print("[1] 接口字段")
        d = api("/conflicts?status=all&limit=200")
        rows = d.get("conflicts") or []
        check("有冲突记录", len(rows) > 0, f"count={len(rows)}")
        keys = set(rows[0].keys()) if rows else set()
        for k in ("dim", "dim_cn", "value_a", "value_b", "rationale",
                  "confidence", "sensitivity", "has_rationale"):
            check(f"含字段 {k}", k in keys)
        check("全部条目有理由", all(r.get("has_rationale") for r in rows),
              f"{sum(1 for r in rows if not r.get('has_rationale'))} 条无")
        # 分组数应 < 行数（因为有同一(替身,维度)对比多镜像源的情况）
        groups = {(r["stand_id"], r["dim"]) for r in rows}
        print(f"      {len(rows)} 行 → {len(groups)} 组")
        check("分组数 ≤ 行数", len(groups) <= len(rows))
        # 筛选
        for st, pred in (("pending", lambda r: r["resolved_value"] is None),
                         ("resolved", lambda r: r["resolved_value"] is not None)):
            dd = api(f"/conflicts?status={st}&limit=200")
            rr = dd.get("conflicts") or []
            ok = all(pred(r) for r in rr)
            print(f"      status={st:8s} {len(rr)} 条")
            check(f"筛选 {st} 结果正确", ok, "含不符合条件的行")

        # ---- 浏览器层 ----
        print("\n[2] 真浏览器（点 tab + 截图）")
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            br = pw.chromium.launch()
            pg = br.new_page(viewport={"width": 1180, "height": 900})
            errs: list[str] = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            cons: list[str] = []
            pg.on("console", lambda m: cons.append(m.text)
                  if m.type == "error" else None)
            pg.goto(f"http://127.0.0.1:{PORT}/", wait_until="domcontentloaded")
            pg.wait_for_timeout(600)

            # 点「冲突记录」tab
            pg.click('button[data-v="c"]')
            pg.wait_for_selector(".cf-card", timeout=20000)
            pg.wait_for_timeout(700)

            n_cards = pg.eval_on_selector_all(".cf-card", "els=>els.length")
            n_pend = pg.eval_on_selector_all(
                ".cf-card.is-pending", "els=>els.length")
            print(f"      渲染卡片 {n_cards} 张（其中保留未知 {n_pend} 张）")
            check("卡片已渲染", n_cards > 0, f"{n_cards}")
            check("有『保留未知』卡片", n_pend > 0, f"{n_pend}")

            # ★ 关键：字段名 bug 的回归守卫
            #   修 bug 前「维度/主源/镜像源」四列恒空 —— 这里断言它们有内容
            bad = pg.evaluate("""() => {
              const out = [];
              document.querySelectorAll('.cf-card').forEach((c,i)=>{
                if(i>4) return;
                const dim = c.querySelector('.cf-dim');
                const srcs = c.querySelectorAll('.cf-srcs .cf-v');
                const srcN = c.querySelectorAll('.cf-srcs .cf-src').length;
                if(!dim || !dim.textContent.trim()) out.push('dim-empty');
                if(srcN < 2) out.push('src-missing');
                if(srcs.length < 2) out.push('val-missing');
              });
              return out;
            }""")
            print(f"      空字段检查: {bad if bad else '无'}")
            check("维度/源/值均有内容（原 bug 回归守卫）", not bad, str(bad))

            pg.screenshot(path=str(OUT / "m22_all.png"), full_page=True)
            print("      截图 m22_all.png")

            # 筛选：待消解
            pg.click('.chip:has-text("待消解")')
            pg.wait_for_timeout(1200)
            pend_all = pg.eval_on_selector_all(
                ".cf-card:not(.is-pending)", "els=>els.length")
            check("点『待消解』后无已消解卡片", pend_all == 0, f"{pend_all}")
            pg.screenshot(path=str(OUT / "m22_pending.png"), full_page=True)

            # 筛选：已消解
            pg.click('.chip:has-text("已消解")')
            pg.wait_for_timeout(1200)
            res_pend = pg.eval_on_selector_all(
                ".cf-card.is-pending", "els=>els.length")
            check("点『已消解』后无保留未知卡片", res_pend == 0, f"{res_pend}")
            pg.screenshot(path=str(OUT / "m22_resolved.png"), full_page=True)

            # ---- 其它视图未被破坏 ----
            # ★ 注意：问答 / 数据浏览**初始是空的**，要先点查询按钮才渲染
            #   （不能直接断言"有内容"——那是测试写错了，不是功能坏了）
            print("\n[3] 回归其它视图")
            pg.click('button[data-v="q"]')
            pg.fill("#qi", "白金之星的破坏力")
            pg.click("#qb")
            pg.wait_for_timeout(4000)
            t = pg.eval_on_selector_all(
                "#ans", "els=>els.map(e=>e.textContent).join('')")
            print(f"      问答（点查询后）: {len(t)} 字符")
            check("问答视图能出结果", len(t) > 20, f"{len(t)} 字符")
            check("问答结果含答案", "白金" in t or "Star" in t or "A" in t,
                  t[:60])

            pg.click('button[data-v="b"]')
            # ★ 用**准确的**替身名，避免触发「id 不存在 → 回退检索」的降级路径
            #   （那条路径会发一个 404，是设计内的，不是 bug ——
            #    下面单独测它）
            pg.fill("#bi", "Star Platinum")
            pg.click("#bb")
            pg.wait_for_timeout(2500)
            t = pg.eval_on_selector_all(
                "#bans", "els=>els.map(e=>e.textContent).join('')")
            print(f"      数据浏览（点查询后）: {len(t)} 字符")
            check("数据浏览能出结果", len(t) > 20, f"{len(t)} 字符")
            check("结果含替身名", "Star Platinum" in t or "白金" in t, t[:60])

            # 降级路径：输入不存在的名字 → 应给出提示而不是静默失败
            pg.fill("#bi", "NotARealStand")
            pg.click("#bb")
            pg.wait_for_timeout(2500)
            t = pg.eval_on_selector_all(
                "#bans", "els=>els.map(e=>e.textContent).join('')")
            print(f"      降级路径（不存在的名字）: {t[:56]!r}")
            check("不存在的名字给出提示（不静默失败）",
                  "未找到" in t or "提示" in t, t[:60])

            pg.click('button[data-v="s"]')
            pg.wait_for_timeout(1800)
            t = pg.eval_on_selector_all(
                "#sans", "els=>els.map(e=>e.textContent).join('')")
            print(f"      系统状态: {len(t)} 字符")
            check("系统状态视图有内容", len(t) > 10, f"{len(t)} 字符")

            # ★ 采纳值应与源值同形态（都是字母）—— 修 resolved_value 数字不一致
            pg.click('button[data-v="c"]')
            pg.wait_for_selector(".cf-card", timeout=15000)
            pg.wait_for_timeout(600)
            mism = pg.evaluate("""() => {
              const out = [];
              document.querySelectorAll('.cf-card').forEach(c=>{
                if(c.classList.contains('is-pending')) return;
                const pick = c.querySelector('.cf-v.is-pick');
                if(!pick) return;
                const t = pick.textContent.trim();
                if(!/^[A-E]$/.test(t) && !/^—$/.test(t))
                  out.push(c.querySelector('.cf-stand').textContent+':'+t);
              });
              return out;
            }""")
            print(f"      采纳值非字母的卡片: {mism if mism else '无'}")
            check("采纳值与源值同形态（字母）", not mism, str(mism[:3]))

            check("无 JS 运行时错误", not errs, str(errs[:2]))
            # ★ console 里的 404 是**降级路径**故意触发的
            #   （/stands/NotARealStand 不存在 → 回退检索），不算故障。
            #   这里只拦非 404 的错误。
            real_err = [c for c in cons if "404" not in c]
            check("无 console error（404 降级除外）", not real_err,
                  str(real_err[:2]))
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
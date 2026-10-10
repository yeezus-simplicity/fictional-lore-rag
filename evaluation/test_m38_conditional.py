"""M38 验证：条件值（stand_stat_conditional）是否真的被用上了。

★ 为什么需要这个测试
--------------------
这 23 条条件值**一直存在数据库里**，但服务层从未引用 ——
于是「黑色安息日的破坏力」只答 E，「白蛇的破坏力」答「无数据」，
用户永远不知道源数据里写着「束缚时 A」「可能 A（有出处）」。

这类 bug 的特点是：**答案看起来完整**。
答错会被用户发现，「答得不全」不会 —— 所以必须靠测试守住。

★ 验证的是「用户看得见的行为」，不是「函数返回值对」
  （M17/M22 栽过：接口全绿 ≠ 前端能看到）。
"""
from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORT = 8788
OUT = ROOT / ".tmp_preview"
PY = sys.executable
fails: list[str] = []

# 三个代表性替身，覆盖两种情况：
#   有值 + 条件（黑安息日）/ 无值 + 条件（白蛇、婴幼儿面部）
CASES = [
    ("黑色安息日的破坏力", ["条件限定", "束缚敌人时破坏力为 A", "常态"],
     "有 baseline，但带条件"),
    ("白蛇的破坏力", ["没有固定的等级", "JoJoveller", "不是「无数据」"],
     "无 baseline，源里给了推测与出处"),
    ("婴儿面孔的精密性", ["取决于", "教育", "不是「无数据」"],
     "无 baseline，条件依赖"),
]


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
    log = open(ROOT / "images" / "_m38.log", "wb")
    proc = subprocess.Popen(
        [PY, "-u", str(ROOT / "api" / "main.py"), "--port", str(PORT),
         "--no-vector"],
        cwd=str(ROOT), stdout=log, stderr=log, stdin=subprocess.DEVNULL)
    try:
        if not wait_ready(PORT):
            print("服务超时")
            return 1
        print(f"服务就绪 :{PORT}\n")

        # ---------- 接口层：字段是否真的返回了 --------------
        import http.client
        import json
        from urllib.parse import quote

        def ask(q: str) -> dict:
            c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=60)
            c.request("POST", f"/query?q={quote(q)}&top_k=1")
            d = json.loads(c.getresponse().read())
            c.close()
            return d

        print("[1] 接口返回是否带上条件值")
        for q, _keys, desc in CASES:
            d = ask(q)
            a = d.get("answer") or {}
            has = bool(a.get("caveat"))
            ev_tables = [e.get("table") for e in (d.get("evidence") or [])]
            print(f"      [{desc}] {q}")
            print(f"        caveat: {str(a.get('caveat'))[:72]}")
            check(f"{q} 返回 caveat", has, str(a)[:70])
            if has:
                check(f"{q} 证据含条件值表",
                      "stand_stat_conditional" in ev_tables, str(ev_tables))

        print("\n[1c] 顺带修的既有 bug：极值问句被误判为「编造实体」")
        # ★ 实测：模板 `([一-鿿]{2,4})的替身` 从中间截出假名字 ——
        #   「破坏力最高的替身」→「坏力最高」→ abstain。
        #   而「破坏力最高的替身」正是路由**自己列的示例问句**。
        # ★ 这条必须以「不能被那次修复破坏」的方式一起守：
        #   真实存在的人名（东方常秀 → Nut King Call）要答对，
        #   真正不存在的人名（甘道夫，指环王角色）要拒答。
        # ★ 注意：M20 的代码注释里写「东方常秀是《东方project》角色」——
        #   那句是**错的**：东方常秀 = Higashikata Joshu，JoJOLion 角色，
        #   替身 Nut King Call。现在系统能正确解析，注释该更正。
        for q, want_ok in [
                ("破坏力最高的替身", True),
                ("射程最高的替身前40", True),
                ("破坏力最高的前5个", True),
                ("东方常秀的替身是什么", True),    # 真实角色，应答对
                ("甘道夫的替身是什么", False)]:     # 编造人名，应拒答
            d = ask(q)
            ok = (d.get("route") != "abstain") == want_ok
            print(f"      {q:22s} route={d.get('route')}"
                  f"  {'✓' if ok else '✗'}")
            check(f"{q} 路由{'通过' if want_ok else '应拒答'}", ok,
                  f"route={d.get('route')}")

        print("\n[2] 无条件值的替身不该被误加 caveat")
        d = ask("白金之星的破坏力")
        a = d.get("answer") or {}
        check("白金之星无 caveat", not a.get("caveat"), str(a.get("caveat")))

        print("\n[3] 榜单：带条件限定的条目要被标出")
        d = ask("射程最高的替身前40")
        a = d.get("answer") or {}
        items = a.get("items") or []
        flagged = [x for x in items if x.get("conditional_note")]
        print(f"      共 {len(items)} 项，带条件 {len(flagged)} 项")
        for x in flagged[:4]:
            print(f"        {x['name'][:26]:28s} ★ {x.get('conditional_note_cn')}")
        check("榜单有条目被标注条件", len(flagged) > 0, f"{len(flagged)}")
        check("榜单给出 caveat 说明", bool(a.get("caveat")), "无")
        # ★ Moody Blues 的 RNG=5 是「仅回放时」——必须被标出来
        mb = [x for x in items if x["name"] == "Moody Blues"]
        if mb:
            check("Moody Blues 被标注（RNG=5 仅回放时）",
                  bool(mb[0].get("conditional_note")), str(mb[0]))

        # ---------- 浏览器：用户是否真能看到 --------------
        print("\n[4] 浏览器里用户能否看到（真点击 + 截图）")
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            br = pw.chromium.launch()
            pg = br.new_page(viewport={"width": 1150, "height": 900})
            errs: list[str] = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.goto(f"http://127.0.0.1:{PORT}/", wait_until="domcontentloaded")
            pg.wait_for_timeout(700)

            pg.fill("#qi", "黑色安息日的破坏力")
            pg.click("#qb")
            pg.wait_for_selector("#ans .card", timeout=40000)
            pg.wait_for_timeout(2500)
            txt = pg.eval_on_selector_all(
                "#ans", "els=>els.map(e=>e.textContent).join('')")
            print(f"      页面文本: {txt[:150].strip()}")
            check("页面出现「条件限定」", "条件限定" in txt, txt[:90])
            check("页面出现中文说明", "束缚" in txt, txt[:90])
            check("页面保留源数据原文", "Restraining Power" in txt, txt[:90])
            n_box = pg.eval_on_selector_all(".caveat", "els=>els.length")
            check("条件提示渲染成独立区块", n_box > 0, f"{n_box}")
            # ★ M38：类别徽标不该再显示英文原值（用户提过界面别出英文）
            #   category_cn 由后端给，前端那份重复映射已对齐。
            check("类别显示中文（不裸露 CONDITIONAL）",
                  "条件值" in txt and "CONDITIONAL" not in txt,
                  f"CONDITIONAL 仍可见: {'CONDITIONAL' in txt}")
            pg.screenshot(path=str(OUT / "m38_condition.png"), full_page=True)

            # 无 baseline 的情况
            pg.fill("#qi", "白蛇的破坏力")
            pg.click("#qb")
            pg.wait_for_timeout(3000)
            t2 = pg.eval_on_selector_all(
                "#ans", "els=>els.map(e=>e.textContent).join('')")
            check("白蛇：说明不是「无数据」", "不是「无数据」" in t2, t2[:110])
            check("白蛇：给出出处", "JoJoveller" in t2, t2[:110])
            pg.screenshot(path=str(OUT / "m38_conditional_none.png"),
                          full_page=True)

            check("无 JS 运行时错误", not errs, str(errs[:2]))
            br.close()
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=20)
        except Exception:
            proc.kill()
        log.close()

    print("\n" + "=" * 64)
    if fails:
        print(f"✗ {len(fails)} 项失败：{fails}")
        return 1
    print("★ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
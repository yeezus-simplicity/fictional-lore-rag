"""M39 浏览器验证：异译别名可见 + 译名可信级别标注 + 口语缩略名可查。

★ 为什么需要这个测试
--------------------
用户反馈「你的中文翻译是在哪里找的 感觉像机翻」。
排查后发现**数据没有翻错**：
  - 「黑色安息日」本来就是主译名（用户以为我们写的是「黑安息日」）
  - 「娃娃脸」是 Baby Face 的异译别名，**早在数据里**

真正的缺陷是**可见性**：
  1. 别名只用于匹配、从不显示 → 用户输入「娃娃脸」却看到「婴儿面孔」，
     输入与屏幕对不上 → 怀疑机器乱翻
  2. 156 条译名分 4 级可信度（A 官方 / B 双源 / C 单源 / D 反推），
     但卡片上不标 → 所有中文名看起来同等权威

★ 另一个坑（实测）：缩略名不能用规则生成
  对 156 条主译名跑「去掉 色/之/的」得 32 个候选，
  但「白金星 / 隐者紫 / 绿法皇」这类**中文圈根本没人这么叫**
  （白金之星本身就是通行译名，没有缩略形式）。
  加进去的害处：用户问「绿法皇的六维」会被当作**有效查询**，
  等于把自造词认成了真名 → 所以改成逐条确认的白名单。

★ 验证的是「用户看得见的行为」，不是「函数返回值对」。
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORT = 8789
OUT = ROOT / ".tmp_preview"
PY = sys.executable
fails: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}"
          f"{'' if ok else '  ← ' + detail}")
    if not ok:
        fails.append(name)


def wait_ready(port: int, timeout: int = 120) -> bool:
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


def api(port: int, q: str) -> dict:
    """POST /query（★ 注意：参数走 query string 的 q=，不是 JSON body）。"""
    url = (f"http://127.0.0.1:{port}/query?"
           + urllib.parse.urlencode({"q": q, "mode": "extract"}))
    req = urllib.request.Request(url, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    OUT.mkdir(exist_ok=True)
    log = open(ROOT / "images" / "_m39ui.log", "wb")
    proc = subprocess.Popen(
        [PY, "-u", str(ROOT / "api" / "main.py"), "--port", str(PORT),
         "--no-vector", "--chunk-merge", "512"],
        cwd=str(ROOT), stdout=log, stderr=log, stdin=subprocess.DEVNULL)
    try:
        if not wait_ready(PORT):
            print("服务超时")
            return 1
        print(f"服务就绪 :{PORT}\n")

        # ---------- 1. 接口层：异译别名与可信级别是否透传 ----------
        print("1) 接口层")
        r = api(PORT, "娃娃脸的破坏力是几级")
        zc = r.get("zh_card") or {}
        check("命中 baby_face", zc.get("stand_id") == "baby_face",
              f"实际 {zc.get('stand_id')}")
        check("主译名是「婴儿面孔」", zc.get("name_zh") == "婴儿面孔",
              f"实际 {zc.get('name_zh')}")
        check("异译「娃娃脸」被透传", "娃娃脸" in (zc.get("name_zh_variants") or []),
              f"实际 {zc.get('name_zh_variants')}")
        check("带可信级别 C", zc.get("name_zh_level") == "C",
              f"实际 {zc.get('name_zh_level')}")
        check("带来源字段", bool(zc.get("name_zh_source")),
              f"实际 {zc.get('name_zh_source')}")

        # ★ 缺失维度必须是 None，不能是 0（填 0 会被读成「能力为零」）
        sc = zc.get("stats_cn") or {}
        check("缺失维度是 None 而非 0",
              sc.get("精密性") is None and sc.get("成长性") is None,
              f"实际 {sc}")

        # ---------- 2. 口语缩略名（白名单）可查 ----------
        print("\n2) 口语缩略名")
        r2 = api(PORT, "黑安息日的破坏力是几级")
        z2 = r2.get("zh_card") or {}
        check("「黑安息日」命中 black_sabbath",
              z2.get("stand_id") == "black_sabbath",
              f"实际 {z2.get('stand_id')}")
        check("「黑安息日」答案仍是 E 级",
              (r2.get("answer") or {}).get("value_label") == "E",
              f"实际 {(r2.get('answer') or {}).get('value_label')}")

        r3 = api(PORT, "卖鸡小子的速度是几级")
        z3 = r3.get("zh_card") or {}
        check("「卖鸡小子」命中 magician_s_red",
              z3.get("stand_id") == "magician_s_red",
              f"实际 {z3.get('stand_id')}")

        # ---------- 3. 自造词必须**查不到**（防白名单变规则生成） ----------
        print("\n3) 自造缩略词不应被认作真名")
        for bad in ["绿法皇的六维是多少", "白金星的速度是几级"]:
            rb = api(PORT, bad)
            zid = (rb.get("zh_card") or {}).get("stand_id")
            check(f"「{bad[:3]}」不命中（应无替身）",
                  zid not in ("hierophant_green", "star_platinum"),
                  f"实际命中 {zid}")

        # ---------- 4. 浏览器：卡片上真的显示出来了 ----------
        print("\n4) 浏览器渲染")
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            br = pw.chromium.launch()
            pg = br.new_page(viewport={"width": 1280, "height": 900})
            pg.goto(f"http://127.0.0.1:{PORT}/", wait_until="networkidle")
            # ★ 选择器以实测为准：输入框 id 是 qi（不是 q）
            pg.fill("#qi", "娃娃脸的六维是多少")
            pg.click("button:has-text('查询')")
            pg.wait_for_timeout(3500)

            card = pg.query_selector(".stand-panel")
            check("替身档案卡已渲染", card is not None)
            txt = card.inner_text() if card else ""
            check("卡片含「其他译名」字样", "其他译名" in txt,
                  f"卡片文本片段：{txt[:120]}")
            check("卡片含「娃娃脸」", "娃娃脸" in txt)
            check("卡片含可信级别标注",
                  any(k in txt for k in ("单一来源", "官方译名", "双来源一致")),
                  f"卡片文本片段：{txt[:120]}")
            check("卡片标注缺失维度为「无数据」", "无数据" in txt)

            shot = OUT / "m39_variants.png"
            pg.screenshot(path=str(shot))
            print(f"     截图 → {shot}")
            br.close()

        print("\n" + "=" * 60)
        if fails:
            print(f"✗ {len(fails)} 项未通过：")
            for f in fails:
                print(f"    - {f}")
            return 1
        print("✓ 全部通过")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


if __name__ == "__main__":
    sys.exit(main())

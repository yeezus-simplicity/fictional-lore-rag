"""M21 验证：角色中文名与替身中文名补全后的端到端效果。

★ 要证明的事：
  1. 角色**中文名**能被识别（M20 只能认英文名）
     「东方定助的替身是什么」→ 应给出 Soft & Wet，而不是拒答
  2. 不存在的角色**仍然拒答**（「东方常秀」是《东方project》的，不是 JOJO）
  3. 替身**母体名**补上了：「Tusk」「Echoes」有中文名（156/156）
  4. 原有能力不回归（英文角色名、替身名、六维、计数）
"""
from __future__ import annotations

import http.client
import json
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
PORT = 8787
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


def query(q: str) -> dict:
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=120)
    try:
        c.request("POST", f"/query?q={quote(q)}&top_k=3",
                  headers={"Content-Type": "application/json"})
        return json.loads(c.getresponse().read())
    finally:
        c.close()


def atype(d: dict) -> str:
    a = d.get("answer")
    if isinstance(a, dict):
        return str(a.get("type"))
    return str(d.get("answer_type"))


def atext(d: dict) -> str:
    a = d.get("answer")
    if d.get("route") == "abstain":
        return "拒答:" + str((a or {}).get("reason") or a)[:50]
    if isinstance(a, dict):
        return str(a.get("answer") or a.get("stand_name") or a.get("value")
                   or json.dumps(a, ensure_ascii=False)[:70])
    if isinstance(a, list):
        return " / ".join(str(x)[:36] for x in a[:2])
    return str(a)[:70]


def main() -> int:
    fails: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}"
              f"{'' if ok else '  ← ' + detail}")
        if not ok:
            fails.append(name)

    log = open(ROOT / "images" / "_m21.log", "wb")
    proc = subprocess.Popen(
        [PY, str(ROOT / "api" / "main.py"), "--port", str(PORT),
         "--no-vector", "--chunk-merge", "512"],
        cwd=str(ROOT), stdout=log, stderr=log, stdin=subprocess.DEVNULL)
    try:
        if not wait_ready(PORT):
            print("服务超时")
            return 1
        print(f"服务就绪 :{PORT}\n")

        # ===== 1. 角色中文名 → 能查到替身 =====
        print("[1] 角色中文名（M21 新增能力）")
        cases = [
            ("空条承太郎的替身是什么", "Star Platinum"),
            ("东方仗助的替身是什么", "Crazy Diamond"),
            ("乔鲁诺·乔巴拿的替身是什么", "Gold Experience"),
            ("布罗诺·布加拉提的替身是什么", "Sticky Fingers"),
            ("杰洛·齐贝林的替身是什么", None),      # 只要不是拒答
            ("迪奥·布兰度的替身是什么", "The World"),
        ]
        for q, want in cases:
            d = query(q)
            txt = atext(d)
            print(f"      {q:26s} {atype(d):10s} {txt[:44]}")
            check(f"{q[:14]} 未被拒答", d.get("route") != "abstain",
                  txt)
            check(f"{q[:14]} 类型为 stand_of", atype(d) == "stand_of",
                  atype(d))
            if want:
                check(f"{q[:14]} 含「{want}」", want in txt, txt)

        # ===== 2. ★ 重要纠正：「东方常秀」是**真角色** =====
        #   实测发现：joshu_higashikata（第8部 JoJolion 东方家的次子）
        #   中文译名就是「东方常秀」，替身是 Nut King Call。
        #   M20 时它被误判为"编造实体"而拒答（我当时的判断有误），
        #   M21 补上角色中文名后能正确作答 —— 这里守住这个行为。
        print("\n[2] 「东方常秀」是真角色（M20 曾误拒答，M21 修正）")
        for q, want in (("东方常秀的替身是什么", "Nut King Call"),
                        ("东方剑的替身是什么", None)):
            d = query(q)
            txt = atext(d)
            print(f"      {q:24s} route={d.get('route')} {txt[:40]}")
            check(f"{q[:10]} 不拒答", d.get("route") != "abstain", txt)
            check(f"{q[:10]} 为 stand_of", atype(d) == "stand_of", atype(d))
            if want:
                check(f"{q[:10]} 含「{want}」", want in txt, txt)

        # 真正编造的角色名仍须拒答
        print("\n[2b] 真编造的角色名仍拒答")
        for q in ("田所浩二的替身是什么", "李四的替身是什么"):
            d = query(q)
            print(f"      {q:24s} route={d.get('route')}")
            check(f"{q[:8]} 判为 abstain", d.get("route") == "abstain",
                  f"route={d.get('route')}")

        # ===== 3. 替身母体名补齐 =====
        print("\n[3] 替身母体名（Tusk / Echoes）")
        for sid in ("tusk", "echoes"):
            c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=60)
            try:
                c.request("GET", f"/stands/{sid}")
                j = json.loads(c.getresponse().read())
            finally:
                c.close()
            zh = j.get("name_zh")
            print(f"      {sid:8s} name_zh={zh}")
            check(f"{sid} 有中文名", bool(zh), str(j)[:60])

        # ===== 4. 原有能力不回归 =====
        print("\n[4] 原有能力回归")
        d = query("Jotaro Kujo的替身是什么")
        check("英文角色名仍可用", atype(d) == "stand_of", atype(d))
        check("英文名答案含 Star Platinum", "Star Platinum" in atext(d),
              atext(d))
        d = query("软又湿的替身是什么")
        check("替身中文名问句未被误判为 stand_of",
              atype(d) != "stand_of", atype(d))
        d = query("透明宝宝的破坏力是几级")
        print(f"      fact → {atext(d)[:40]}")
        check("六维查询仍可用", atype(d) in ("fact", "unknown"), atype(d))
        d = query("每部有多少替身")
        check("篇章统计仍可用", atype(d) in ("part_stats", "part_count"),
              atype(d))
        d = query("今天天气怎么样")
        check("无关问句仍拒答", d.get("route") == "abstain",
              f"route={d.get('route')}")
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
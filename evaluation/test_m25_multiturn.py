"""M25 验证：多轮追问（指代消解）。

★ 为什么必须自己起服务：`nohup ... &` 启动的进程会随命令结束被回收，
  测试必须「起服务 → 测 → 关服务」在一个进程里完成。
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
PORT = 8783
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


def ask(q: str, sid: str | None = None, top_k: int = 3) -> dict:
    path = f"/query?q={quote(q)}&top_k={top_k}"
    if sid:
        path += f"&session_id={sid}"
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=120)
    try:
        c.request("POST", path, headers={"Content-Type": "application/json"})
        return json.loads(c.getresponse().read())
    finally:
        c.close()


def brief(d: dict) -> str:
    a = d.get("answer")
    if d.get("route") == "abstain":
        return "拒答"
    if isinstance(a, dict):
        return str(a.get("answer") or a.get("value")
                   or json.dumps(a, ensure_ascii=False)[:60])
    if isinstance(a, list):
        return " / ".join(str(x)[:40] for x in a[:2])
    return str(a)[:70]


def main() -> int:
    log = open(ROOT / "images" / "_m25.log", "wb")
    proc = subprocess.Popen(
        # ★ -u：不缓冲，否则初始化日志写不进文件（实测踩过）
        [PY, "-u", str(ROOT / "api" / "main.py"), "--port", str(PORT),
         "--no-vector", "--chunk-merge", "512"],
        cwd=str(ROOT), stdout=log, stderr=log, stdin=subprocess.DEVNULL)
    try:
        if not wait_ready(PORT):
            print("服务超时")
            return 1
        print(f"服务就绪 :{PORT}\n")

        # ===== 1. 基础：第 1 轮建立上下文 =====
        print("[1] 第 1 轮：建立上下文（无代词，不应消解）")
        d1 = ask("空条承太郎的替身是什么", "e2e-A")
        print(f"      route={d1.get('route')}  答: {brief(d1)}")
        check("第 1 轮可正常作答", d1.get("route") != "abstain", brief(d1))
        check("第 1 轮 coref 为空（本来就没代词）",
              d1.get("coref") is None, str(d1.get("coref")))

        # ===== 2. 核心：代词追问 =====
        print("\n[2] 第 2 轮：「它」应解析为 Star Platinum")
        d2 = ask("那它的速度呢", "e2e-A")
        cf = d2.get("coref") or {}
        print(f"      route={d2.get('route')}  答: {brief(d2)}")
        print(f"      coref={cf}")
        check("触发了指代消解", bool(cf), "coref 为空")
        check("代词识别为「它」", cf.get("pronoun") == "它",
              str(cf.get("pronoun")))
        check("解析到 Star Platinum",
              str(cf.get("resolved_to", "")).lower().replace(" ", "")
              in ("starplatinum", "白金之星"),
              str(cf.get("resolved_to")))
        check("第 2 轮答的是速度（不是拒答）",
              d2.get("route") != "abstain", brief(d2))

        # ===== 3. 上下文延续（连续追问）=====
        print("\n[3] 第 3 轮：再追问，上下文应延续")
        d3 = ask("那它的破坏力呢", "e2e-A")
        cf3 = d3.get("coref") or {}
        print(f"      route={d3.get('route')}  答: {brief(d3)}")
        print(f"      coref={cf3}")
        check("第 3 轮仍能消解", bool(cf3), "coref 为空")
        check("第 3 轮仍指向 Star Platinum",
              "star" in str(cf3.get("resolved_to", "")).lower()
              or "白金" in str(cf3.get("resolved_to", "")),
              str(cf3.get("resolved_to")))

        # ===== 4. ★ 本轮自带实体 → 不该被污染 =====
        print("\n[4] 第 4 轮：自带实体，不应被上一轮覆盖")
        d4 = ask("白金之星的射程呢", "e2e-A")
        print(f"      route={d4.get('route')}  答: {brief(d4)}")
        print(f"      coref={d4.get('coref')}")
        check("自带实体时 coref 为空", d4.get("coref") is None,
              str(d4.get("coref")))

        # ===== 5. 无 session → 不消解（向后兼容）=====
        print("\n[5] 无 session_id：应与单轮行为一致")
        d5 = ask("那它的速度呢", None)
        print(f"      route={d5.get('route')}  答: {brief(d5)}")
        check("无 session 时 coref 为空", d5.get("coref") is None,
              str(d5.get("coref")))

        # ===== 6. ★ 伪代词不该触发替换 =====
        print("\n[6] 伪代词（其他/其中/尤其）：不该触发消解")
        for q in ("其他替身有哪些", "其中哪些是第七部的"):
            d = ask(q, "e2e-A")
            print(f"      {q:22s} coref={d.get('coref')}")
            check(f"「{q[:4]}」未触发消解", d.get("coref") is None,
                  str(d.get("coref")))

        # ===== 7. 会话隔离 =====
        print("\n[7] 会话隔离：另一个 session 不该串上下文")
        d7a = ask("东方常秀的替身是什么", "e2e-B")
        print(f"      B 轮1 route={d7a.get('route')} 答: {brief(d7a)}")
        d7b = ask("那它的速度呢", "e2e-B")
        cf7 = d7b.get("coref") or {}
        print(f"      B 轮2 coref={cf7}")
        check("B 会话解析到 Nut King Call（不是 A 的 Star Platinum）",
              "nut" in str(cf7.get("resolved_to", "")).lower()
              or "纳" in str(cf7.get("resolved_to", "")),
              str(cf7.get("resolved_to")))
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
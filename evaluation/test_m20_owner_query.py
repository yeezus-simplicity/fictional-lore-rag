"""M20 回归测试：两类用户反馈。

  A. 「东方常秀的替身是什么」→ 应**拒答**
     东方常秀是《东方project》角色，不是 JOJO 角色。
     之前答成Soft & Wet 的能力介绍（那是东方**定助**的替身）。
  B. 「XX的替身是什么」→ 应**直接给替身名**，不是长段介绍
     用户明确要「最直接的答案」，而不是一整段能力原文。

★ 顺带守住回归：修 B 的规则不能误伤
     「软又湿的替身是什么」「Tusk 的替身是什么」——
     这两句里 XX 本身就是**替身名**，不该走「反查角色」。
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
PORT = 8790
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


def query(q: str, port: int) -> dict:
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    try:
        c.request("POST", f"/query?q={quote(q)}&top_k=3",
                  headers={"Content-Type": "application/json"})
        r = c.getresponse()
        return json.loads(r.read())
    finally:
        c.close()


def brief(d: dict) -> str:
    """把答案压成一行，便于断言与打印。"""
    a = d.get("answer")
    if d.get("route") == "abstain":
        if isinstance(a, dict):
            return "拒答:" + str(a.get("reason", ""))[:56]
        return "拒答:" + str(a)[:56]
    if isinstance(a, dict):
        if a.get("answer"):
            return f"[{a.get('type')}] {a['answer']}"
        if a.get("stand_name"):
            return f"[{a.get('type')}] {a.get('stand_name')}"
        if a.get("value"):
            return f"[{a.get('type')}] {a['value']}"
        return f"[{a.get('type')}] " + json.dumps(a, ensure_ascii=False)[:70]
    if isinstance(a, list):
        return f"[list] " + " / ".join(str(x)[:40] for x in a[:2])
    return str(a)[:80]


def main() -> int:
    fails: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}"
              f"{'' if ok else '  ← ' + detail}")
        if not ok:
            fails.append(name)

    log = open(ROOT / "images" / "_m20.log", "wb")
    proc = subprocess.Popen(
        [PY, str(ROOT / "api" / "main.py"), "--port", str(PORT),
         "--no-vector", "--chunk-merge", "512"],
        cwd=str(ROOT), stdout=log, stderr=log, stdin=subprocess.DEVNULL)
    try:
        if not wait_ready(PORT):
            print("服务超时")
            return 1
        print(f"服务就绪 :{PORT}\n")

        # ===== A. 不存在的角色必须拒答 =====
        print("[A] 不存在的角色 → 拒答")
        for q, bad in (("东方常秀的替身是什么", "Soft"),
                       ("常秀的替身是什么", "Soft")):
            d = query(q, PORT)
            txt = brief(d)
            print(f"      {q:22s} {txt}")
            check(f"{q[:12]} 判为 abstain", d.get("route") == "abstain",
                  f"route={d.get('route')}")
            # ★ 关键是答案里不能出现别的替身名（Soft & Wet）
            check(f"{q[:12]} 答案不含 Soft", bad not in txt,
                  f"泄漏了别的替身：{txt}")

        # ===== B. 「XX 的替身」→ 直接给名字 =====
        print("\n[B] 「某人的替身」→ 直接给替身名（不长段介绍）")
        cases = [
            ("Jotaro Kujo的替身是什么", "Star Platinum"),
            ("Giorno Giovanna的替身是什么", "Gold Experience"),
            ("Josuke Higashikata的替身是什么", "Crazy Diamond"),
            ("Yoshikage Kira的替身是什么", "Killer Queen"),
        ]
        for q, want in cases:
            d = query(q, PORT)
            txt = brief(d)
            print(f"      {q:28s} {txt[:64]}")
            check(f"{q[:20]} 答案含「{want}」", want in txt, txt)
            # ★ 必须短：不能再糊一整段原文（>160 字视为长段）
            check(f"{q[:20]} 答案简短（<160 字）", len(txt) < 160,
                  f"长度 {len(txt)}：{txt[:70]}")
            check(f"{q[:20]} 类型是 stand_of",
                  (d.get("answer") or {}).get("type") == "stand_of",
                  f"type={(d.get('answer') or {}).get('type')}")

        # ===== C. 不能误伤替身本身的名字 =====
        print("\n[C] 回归：「XX 的替身是什么」里 XX 本身是替身名")
        #★ 注意：这些问句里 XX 本身就是**替身名**，
        #   问的其实是「这个替身是什么人/什么来头」——
        #   回答形态不同（可能是 snippet 原文），但
        #   **绝不能**是 stand_of（那会去反查角色）。
        for q in ("软又湿的替身是什么", "Tusk的替身是什么",
                  "骇游天外的替身是什么"):
            d = query(q, PORT)
            a = d.get("answer")
            t = a.get("type") if isinstance(a, dict) else \
                ("list" if isinstance(a, list) else None)
            print(f"      {q:22s} route={str(d.get('route')):16s} "
                  f"type={t}")
            check(f"{q[:12]} 未误判为 stand_of", t != "stand_of",
                  f"route={d.get('route')} type={t}")

        # ===== D. 原有能力不能坏 =====
        print("\n[D] 原有功能回归")

        def atype(d: dict) -> str:
            """取出answer_type（answer 是 list 时回退到 answer_type 字段）。"""
            a = d.get("answer")
            if isinstance(a, dict):
                return str(a.get("type"))
            return str(d.get("answer_type"))

        d = query("透明宝宝的破坏力是几级", PORT)
        print(f"      fact→ {brief(d)[:50]}")
        check("fact 意图仍能答", atype(d) in ("fact", "unknown"),
              f"type={atype(d)}")
        d = query("Soft & Wet 的使用者是谁", PORT)
        print(f"      owner → {brief(d)[:50]}")
        check("owner 意图仍能答", atype(d) == "owner", f"type={atype(d)}")
        d = query("每部有多少替身", PORT)
        print(f"      count → {brief(d)[:50]}")
        # ★ 既有实现里 _part_count 返回的 type 是 "part_stats"（不是
        #   意图名 "part_count"）。这是 M14 就有的行为，M20 未改动它
        #   （git diff 全是新增，无删除）—— 别把断言写成 part_count。
        check("part_count 仍能答（part_stats）",
              atype(d) in ("part_stats", "part_count"), f"type={atype(d)}")
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
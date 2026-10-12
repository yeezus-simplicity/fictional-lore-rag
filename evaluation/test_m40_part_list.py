"""M40 回归测试：补第 8/9 部替身后，这些替身必须**查得到**。

背景（用户反馈）：
  「还有很多替身没加进去呢 第九部的替身我基本没看见」
  → 补了 31 个替身（第9部 0→13，第8部 7→24），数据写进库了。

但**入库成功 ≠ 用户查得到**。实测发现三类断链：

  A. `INTENT_PART` 只认阿拉伯数字
     `第\\s*(\\d+)\\s*部` → 「第九部」匹配不到 → part=None
     → 退回语义检索 → 返回一堆无关 wiki 片段。
     （★ 静态校验用的是 SQL 直接查 part=3，所以查不出这个洞。）

  B. 「第X部有哪些替身」没有对应意图
     它不含 INTENT_EXTREME 的词、也不含「多少个」
     → detect_intent 落到 unknown → abstain。

  C. 聚合父记录会**静默覆盖**真实母体
     load_data 对 echoes/tusk 形态组无条件补建聚合记录，
     与 stands.json 里新补的 `tusk` 母体**同 id** → executemany 的
     ON CONFLICT DO UPDATE 把 tusk 的 owner / name_ja / stand_type
     全部覆盖成 Aggregate。属数据丢失，且不报错。

本测试同时守住：A/B 的修复不能误伤原有的「多少个」计数与 owner 查询。
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
PORT = 8795
PY = sys.executable


def wait_ready(port: int, timeout: float = 120) -> bool:
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


def query(q: str, port: int, top_k: int = 3) -> dict:
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=150)
    try:
        c.request("POST", f"/query?q={quote(q)}&top_k={top_k}",
                  headers={"Content-Type": "application/json"})
        return json.loads(c.getresponse().read())
    finally:
        c.close()


def names_of(d: dict) -> list[str]:
    a = d.get("answer") or {}
    if isinstance(a, dict) and a.get("type") == "part_list":
        return [x.get("name") for x in (a.get("items") or [])]
    return []


def main() -> int:
    fails: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}"
              f"{'' if ok else '  ← ' + detail}")
        if not ok:
            fails.append(name)

    # ---------------------------------------------------------------
    # 静态部分：磁盘数据与 DB 的一致性（不需要起服务）
    # ---------------------------------------------------------------
    print("\n[1] 磁盘数据：第 8/9 部替身是否补齐")
    stands = json.loads(
        (ROOT / "dataset" / "processed" / "stands.json").read_text(
            encoding="utf-8"))
    by_part: dict[int, list[str]] = {}
    for s in stands:
        by_part.setdefault(s.get("part"), []).append(s.get("name_en"))

    n9, n8 = len(by_part.get(9, [])), len(by_part.get(8, []))
    check(f"第9部有替身（{n9} 个）", n9 >= 13, f"实际 {n9}")
    check(f"第8部有替身（{n8} 个）", n8 >= 20, f"实际 {n8}")
    check("第9部含主角替身 November Rain",
          "November Rain" in (by_part.get(9) or []))
    check("第8部含 Wonder of U",
          "Wonder of U" in (by_part.get(8) or []))
    # ★ 六维必须留空（官方未评级），绝不能是 0
    stats = json.loads(
        (ROOT / "dataset" / "processed" / "stand_stats.json").read_text(
            encoding="utf-8"))
    new_part_ids = {s["stand_id"] for s in stands
                    if s.get("part") in (8, 9)}
    new_stats = [r for r in stats if r.get("stand_id") in new_part_ids]
    zeros = [r["stand_id"] for r in new_stats
             if any(r.get(d) == 0 for d in ("pwr", "spd", "rng",
                                            "sta", "prc", "dev"))]
    check(f"新增替身六维无 0 值（{len(new_stats)} 行）", not zeros,
          f"出现 0 的：{zeros[:5]}")

    # ---------------------------------------------------------------
    # 起服务
    # ---------------------------------------------------------------
    print("\n[2] 启动服务")
    log = open(ROOT / "images" / "_m40.log", "wb")
    proc = subprocess.Popen(
        [PY, str(ROOT / "api" / "main.py"), "--port", str(PORT)],
        cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL)
    try:
        if not wait_ready(PORT):
            print("  [FAIL] 服务未就绪")
            return 1
        print(f"  [OK] 127.0.0.1:{PORT} 就绪")

        # -----------------------------------------------------------
        print("\n[3] 中文数字部数：必须识别（原只认阿拉伯数字）")
        for q, part, expect_min, sample in (
            ("第九部有哪些替身", 9, 13, "November Rain"),
            ("第八部有哪些替身", 8, 20, "Wonder of U"),
        ):
            d = query(q, PORT)
            a = d.get("answer") or {}
            ns = names_of(d)
            check(f"「{q}」→ part_list",
                  a.get("type") == "part_list",
                  f"实际 type={a.get('type')} route={d.get('route')}")
            check(f"  └ 部数 = {part}", a.get("part") == part,
                  f"实际 {a.get('part')}")
            check(f"  └ 至少 {expect_min} 个", len(ns) >= expect_min,
                  f"实际 {len(ns)}")
            check(f"  └ 含 {sample}", sample in ns)

        # ★★ 这一条是本轮最有价值的发现之一 ★★
        #   OUT_OF_RANGE_PART 原写死「9」，把「第9部」判为"不存在的内容"拒答；
        #   而它只认阿拉伯数字，中文「第九部」不被匹配
        #   → 同一问题两种写法，一个能答一个拒答。
        print("\n[4] 阿拉伯数字写法不能回归（原被越界规则误拒）")
        d = query("第9部有哪些替身", PORT)
        ns = names_of(d)
        a = d.get("answer") or {}
        check("「第9部有哪些替身」→ part_list",
              a.get("type") == "part_list",
              f"实际 type={a.get('type')} route={d.get('route')}")
        check("  └ 部数 = 9", a.get("part") == 9, f"实际 {a.get('part')}")
        check("  └ 含 November Rain", "November Rain" in ns,
              f"实际 {ns[:5]}")
        # 两种写法必须得到同一结果（一致性）
        d2 = query("第九部有哪些替身", PORT)
        check("  └ 与中文写法结果一致", names_of(d2) == ns,
              f"中文 {len(names_of(d2))} 个 vs 阿拉伯 {len(ns)} 个")

        print("\n[4b] 真正越界的部数仍应拒答（守住原行为）")
        for q in ("第10部有哪些替身", "第十部有哪些替身", "第0部有哪些替身"):
            d = query(q, PORT)
            check(f"「{q}」→ abstain",
                  d.get("route") == "abstain", f"实际 {d.get('route')}")

        print("\n[5] 「有多少个」计数不能回归")
        d = query("第九部有多少个替身", PORT)
        a = d.get("answer") or {}
        check("「第九部有多少个替身」→ count",
              a.get("type") == "count", f"实际 {a.get('type')}")
        check(f"  └ count = {n9}", a.get("count") == n9,
              f"实际 {a.get('count')}")
        check("  └ scope 写「第9部」", "9" in str(a.get("scope")),
              f"实际 {a.get('scope')}")

        print("\n[6] 新替身的 owner 查询（必须查到，不是拒答）")
        d = query("November Rain 的使用者是谁", PORT)
        a = d.get("answer") or {}
        check("「November Rain 的使用者是谁」非拒答",
              d.get("route") != "abstain",
              f"实际 {d.get('route')}")
        check("  └ 使用者 = Jodio Joestar",
              a.get("owner") == "Jodio Joestar",
              f"实际 {a.get('owner')}")

        print("\n[7] 原有的 owner / stand_of 不能回归")
        d = query("Star Platinum 的使用者是谁", PORT)
        a = d.get("answer") or {}
        check("「Star Platinum 的使用者是谁」→ owner",
              (a.get("type") == "owner"
               and a.get("owner") == "Jotaro Kujo"),
              f"实际 {a.get('type')} / {a.get('owner')}")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except Exception:
            proc.kill()
        log.close()

    # ---------------------------------------------------------------
    print("\n[8] 聚合父记录不能覆盖真实母体（DB 层断言）")
    sys.path.insert(0, str(ROOT / "database"))
    try:
        from load_db import connect  # noqa: E402
        c = connect()
        cur = c.cursor()
        cur.execute("SELECT stand_type, owner_name_raw, name_ja FROM stands "
                    "WHERE stand_id = 'tusk'")
        row = cur.fetchone()
        check("tusk 母体未被覆盖为 Aggregate",
              bool(row) and row[0] != "Aggregate (form group)",
              f"实际 stand_type={row[0] if row else None}")
        check("  └ tusk 的 owner 未丢失",
              bool(row) and bool(row[1]),
              f"实际 owner={row[1] if row else None}")
        check("  └ tusk 的日文名未丢失",
              bool(row) and bool(row[2]),
              f"实际 name_ja={row[2] if row else None}")
        # echoes 母体不在 stands.json → 仍应补建（既有行为保留）
        cur.execute("SELECT stand_type FROM stands WHERE stand_id = 'echoes'")
        r2 = cur.fetchone()
        check("echoes 仍由聚合逻辑补建（母体不在 stands.json）",
              bool(r2) and r2[0] == "Aggregate (form group)",
              f"实际 {r2[0] if r2 else None}")
        # DB 行数与磁盘推导一致
        cur.execute("SELECT count(*) FROM stands WHERE part IN (8, 9)")
        db_n = cur.fetchone()[0]
        check(f"DB 第8/9部行数 = {n8 + n9}", db_n == n8 + n9,
              f"实际 {db_n}")
        cur.close()
        c.close()
    except Exception as e:
        check("DB 断言可执行", False, f"{type(e).__name__}: {e}")

    print()
    if fails:
        print(f"✗ {len(fails)} 项失败：" + "、".join(fails))
        return 1
    print("✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())

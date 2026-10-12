"""M41 回归测试：新补替身的**中文名**必须能被搜到。

背景（用户反馈）：
  「还是缺失很多替身啊 比如繁荣之墓 再度失言 都是第7 9的替身」

★ 实测这两个替身**都在库里**：
    繁荣之墓 = Tomb of the Boom 1 2 3（第7部）
    再度失言 = Big Mouth Strikes Again（第9部）
  缺的是**中文名映射** —— 具体有两层：

  A. **完全没有中文名**
     stand_name_zh.json 只有 156 条，而 B 方案新补的 30 个替身
     （第8/9部为主）一条都没有 → 用户用中文搜，别名表查不到。
  B. **只收了一个通行译名，另一个匹配不到**
     Tomb of the Boom 库内主名是 bilibili 版「盛世之墓」，
     而用户用的是萌娘百科版「繁荣之墓」→ 换个写法就查不到。
     （与「娃娃脸 / 婴儿面孔」完全同一模式）

本测试守住三件事：
  1. 缺名的替身拿到中文名（主名）
  2. 已有名但缺异译的，把异译也收进 name_zh_variants
  3. 修复不能误伤：原有中文名查询照旧可用

★ 收录原则：每条译名都要有可核对出处（萌娘百科条目 / huijiwiki 替身列表 /
  bilibili 对照表 / 用户提供），**不推测、不机翻、不自造**。
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
PROC = ROOT / "dataset" / "processed"
PORT = 8796
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


def query(q: str, port: int) -> dict:
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=150)
    try:
        c.request("POST", f"/query?q={quote(q)}&top_k=3",
                  headers={"Content-Type": "application/json"})
        return json.loads(c.getresponse().read())
    finally:
        c.close()


def main() -> int:
    fails: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}"
              f"{'' if ok else '  ← ' + detail}")
        if not ok:
            fails.append(name)

    zh = json.loads((PROC / "stand_name_zh.json").read_text(encoding="utf-8"))
    stands = json.loads((PROC / "stands.json").read_text(encoding="utf-8"))

    # ---------------------------------------------------------------
    print("\n[1] 中文名覆盖率")
    t = zh.get("tomb_of_the_boom_1_2_3") or {}
    b = zh.get("big_mouth_strikes_again")
    n_have = len(zh)
    check(f"有中文名的替身 {n_have} 条（原 156，应显著增加）",
          n_have > 156, f"实际 {n_have}")
    check("Tomb of the Boom 有记录", bool(t), "无记录")
    check("Big Mouth Strikes Again 有记录", bool(b), "无记录")

    print("\n[2] ★ 用户举的两个译名必须能命中")
    # 繁荣之墓 = Tomb of the Boom 的萌娘版译名（库内主名是「盛世之墓」）
    t_names = [t.get("name_zh")] + list(t.get("name_zh_variants") or [])
    check("「繁荣之墓」在 Tomb of the Boom 的译名里",
          "繁荣之墓" in t_names, f"实际 {t_names}")
    check("  └ 主名「盛世之墓」仍保留",
          t.get("name_zh") == "盛世之墓", f"实际 {t.get('name_zh')}")
    # 再度失言 = Big Mouth Strikes Again
    b_names = [b.get("name_zh")] + list(b.get("name_zh_variants") or [])
    check("「再度失言」在 Big Mouth Strikes Again 的译名里",
          "再度失言" in b_names, f"实际 {b_names}")

    print("\n[3] 新补的第 8/9 部替身都要有中文名")
    new_parts = [s for s in stands if s.get("part") in (8, 9)]
    missing = [s["name_en"] for s in new_parts
               if s["stand_id"] not in zh]
    # 允许极少数确实无通行译名的（不推测）；但不能大面积缺
    rate = (len(new_parts) - len(missing)) / max(len(new_parts), 1)
    check(f"第8/9部中文名覆盖 {rate*100:.0f}%（{len(new_parts)} 个）",
          rate >= 0.85, f"缺失 {len(missing)} 个：{missing[:8]}")

    print("\n[4] 翻译可信度：不推测、不机翻")
    bad_level = [k for k, v in zh.items()
                 if v.get("level") not in ("A", "B", "C", "D")]
    check("所有条目都有 level 标注", not bad_level, f"{bad_level[:5]}")
    srcs = {v.get("source", "") for v in zh.values()}
    check("来源可追溯（无空来源）",
          all(s for s in srcs), f"空来源条目存在")
    # 六维缺失仍 must be None（不因补名而顺手填 0）
    stats = json.loads((PROC / "stand_stats.json").read_text(encoding="utf-8"))
    zeros = [r["stand_id"] for r in stats
             if any(r.get(d) == 0 for d in ("pwr", "spd", "rng",
                                            "sta", "prc", "dev"))]
    check(f"六维无 0 值（{len(stats)} 行）", not zeros, f"{zeros[:5]}")

    # ---------------------------------------------------------------
    print("\n[5] 起服务，端到端验证")
    log = open(ROOT / "images" / "_m41.log", "wb")
    proc = subprocess.Popen(
        [PY, str(ROOT / "api" / "main.py"), "--port", str(PORT)],
        cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL)
    try:
        if not wait_ready(PORT):
            check("服务就绪", False, "超时")
            return 1
        print(f"  [OK] 127.0.0.1:{PORT} 就绪")

        for q in ("繁荣之墓的破坏力是几级", "再度失言的使用者是谁"):
            d = query(q, PORT)
            a = d.get("answer") or {}
            zc = d.get("zh_card") or {}
            ok = (d.get("route") == "structured"
                  and d.get("answer_type") != "abstain")
            check(f"「{q}」非拒答", ok,
                  f"route={d.get('route')} type={d.get('answer_type')}")
            print(f"        命中：{zc.get('name_zh') or a.get('stand_name')}"
                  f" / 使用者 {a.get('owner') or zc.get('owner')}")

        print("\n[6] 原有中文名不能回归")
        for q, want in (("白金之星的速度是几级", "Star Platinum"),
                        ("软又湿的使用者是谁", None)):
            d = query(q, PORT)
            a = d.get("answer") or {}
            zc = d.get("zh_card") or {}
            nm = a.get("stand_name") or zc.get("name_en") or ""
            check(f"「{q}」仍可用", d.get("answer_type") != "abstain",
                  f"route={d.get('route')}")
            if want:
                check(f"  └ 命中 {want}", nm == want, f"实际 {nm}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except Exception:
            proc.kill()
        log.close()

    print()
    if fails:
        print(f"✗ {len(fails)} 项失败：" + "、".join(fails))
        return 1
    print("✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())

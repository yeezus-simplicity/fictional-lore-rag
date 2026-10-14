"""M58b 回归测试：点形态标签能**连续点**，且不把真形态判成编造。

用户反馈（两个现象，同一个根因串起来的）：
  「点击一个 act 之后 点击其他的就不管用了
    然后点击 tusk act4 就显示服务可能暂停」
  他还特别指出：「**实际上并没有停止服务**」

== 实测到的三个缺陷（都我这轮引入的）==
【1】★ 后端 500：`UnboundLocalError: cannot access local variable 'sid'`
    我加的形态链代码读 `sid`，但它只在
    `if route != "abstain" and _images_enabled():` 里面赋值 ——
    问句被判 abstain 时那段整块跳过 → **整个请求 500**。
【2】★ 前端把 500 说成「无法连接到服务 / 最可能的原因是 API 服务已停止」，
    而服务好好地活着 → 把人引向完全错误的方向。
【3】★ 形态标签会**叠加**：标签用"当前替身名 + ACTn"拼，
    点进 ACT1 后就变成「獠牙ACT1 ACT2」，越点越长，
    最后拼出「獠牙ACT1 ACT4」这种查不到的句子。
【4】★ 兜底：真形态被判成编造 —— `FABRICATED_HINTS` 里有 `act\\s*4`
    （T6 评测集的假想替身叫 ACT4），而 **Tusk ACT4 是真的** →
    问「獠牙 ACT4 的六维能力」直接 abstain →
    档案卡与形态标签整块不渲染 →【这就是"点一个之后其他都不管用"的真身】

== 本测试守什么 ==
  1. `sid` 在读取前就初始化（拒答路径不再 500）
  2. 形态标签用**母体**的名字，不会随点击变长
  3. 编造判据是**窄判**：真 ACT4 放行、T6 的 9 条编造题仍拒答
  4. 前端把"连不上"与"服务报错"分开提示
  5. 端到端：ACT1↔ACT4 连点都 200、abstain 类问句不 500
"""
from __future__ import annotations

import http.client
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "api" / "static" / "index.html"
PROC = ROOT / "dataset" / "processed"
PORT = 8803


def wait_http(port: int, timeout: float = 240) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            c.request("GET", "/health")
            if c.getresponse().status == 200:
                return True
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
        time.sleep(2)
    return False


def post(q: str, port: int) -> tuple[int, dict]:
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    try:
        c.request("POST", f"/query?q={quote(q)}&mode=extract",
                  headers={"Content-Type": "application/json"})
        r = c.getresponse()
        raw = r.read().decode("utf-8", "ignore")
        try:
            return r.status, json.loads(raw)
        except Exception:  # noqa: BLE001
            return r.status, {}
    finally:
        c.close()


def main() -> int:
    fails: list[str] = []
    n = 0

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal n
        n += 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}"
              f"{('   ' + detail) if detail and not ok else ''}")
        if not ok:
            fails.append(name)

    src = HTML.read_text(encoding="utf-8")
    mainpy = (ROOT / "api" / "main.py").read_text(encoding="utf-8")
    resolver = (ROOT / "services" / "conflict_resolver.py").read_text(encoding="utf-8")

    print("=" * 74)
    print("M58b：形态标签连点 / 真形态不被判编造 / 报错提示不误导")
    print("=" * 74)

    # ---- ① sid 初始化 ----
    print("\n[1] sid 必须在读之前初始化（否则拒答路径 500）")
    # ★ 用**行号**比较，别用 index()：我写的注释里恰好也出现了
    #   `if route != "abstain" and _images_enabled():` 这句原文，
    #   index() 会命中**注释里的那一处**，断言就误报了（我第一版就踩了）。
    def _line(pat: str) -> int:
        m = re.search(pat, mainpy, re.M)
        return m.start() if m else -1

    init = _line(r"^    sid = None$")
    media = _line(r'^    if route != "abstain" and _images_enabled\(\):$')
    read = _line(r'^    if sid and STATE\.get\("conn"\):$')
    check("sid 的初始化在媒体分支之前", 0 <= init < media, f"{init} vs {media}")
    check("★ sid 的初始化在形态链读取之前", 0 <= init < read, f"{init} vs {read}")
    check("初始化那处带注释说明来由",
          bool(re.search(r"M58b：sid 必须在这里就初始化", mainpy)))

    # ---- ② 标签用母体名 ----
    print("\n[2] 形态标签用母体名（不会越点越长）")
    _blk = mainpy[read:read + 700]
    check("形态链取名走 zh_map 的**母体**条目",
          '_base = re.sub(r"_act' in _blk and "zh_map" in _blk
          and 'get(_base)' in _blk,
          _blk[:160])
    check("★ 不再用当前替身的 name_zh 拼标签",
          "_forms_of(_c, sid, (zh_card or {}).get(\"name_zh\"))" not in mainpy)

    # ---- ③ 编造判据是窄判（单元级，不起服务） ----
    print("\n[3] 编造判据：真 ACT4 放行 / 编造题仍拒答")
    sys.path.insert(0, str(ROOT / "services"))
    import importlib
    import aliases as _al
    importlib.reload(_al)
    a2i, i2a = _al.build_alias_table()
    known = set(a2i) | set(i2a)
    from conflict_resolver import Router
    router = Router(known_stands={s["name_en"] for s in
                                  json.loads((PROC / "stands.json").read_text(encoding="utf-8"))
                                  if s.get("name_en")},
                    known_entities=known)
    must_pass = ["獠牙 ACT4 的六维能力", "獠牙ACT4 的能力", "獠牙 ACT2 的六维能力",
                 "回声 ACT3 的破坏力"]
    for q in must_pass:
        check(f"「{q}」不被判编造", router.route(q).route != "abstain",
              router.route(q).reason[:60])
    items = json.loads((PROC / "eval_set.json").read_text(encoding="utf-8"))
    fab = [x["question"] for x in items
           if any(k in x["question"].lower()
                  for k in ("ultimate", "overlord", "neon", "omega",
                            "revenant", "final form", "act 4", "act4"))]
    check(f"评测集里编造题共 {len(fab)} 条", len(fab) >= 8)
    leaked = [q for q in fab if router.route(q).route != "abstain"]
    check(f"★ 编造题仍全部拒答（放行 {len(leaked)} 条）", not leaked, str(leaked[:3]))

    # ---- ④ 前端区分「连不上」与「服务报错」 ----
    print("\n[4] 前端报错提示不误导")
    check("区分网络失败与 HTTP 状态码",
          "_net = !_st && /fetch|network|load|timeout/i.test(_m)" in src
          and "_st >= 500" in src)
    check("5xx 的文案明确说「服务还活着」", "服务还活着" in src)
    check("只有网络失败才提示重启服务",
          bool(re.search(r"\$\{_net \? `", src)))

    # ---- ⑤ 端到端：连点 + 不 500 ----
    print(f"\n[5] 端到端（127.0.0.1:{PORT}）")
    log = (ROOT / ".tmp_preview" / "_m58b_server.log").open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "api" / "main.py"),
         "--port", str(PORT), "--no-vector"],
        cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL)
    try:
        if not wait_http(PORT):
            check("服务就绪", False, "超时")
            return 1
        seq = ["獠牙 ACT1 的六维能力", "獠牙 ACT2 的六维能力",
               "獠牙 ACT4 的六维能力", "獠牙 ACT3 的六维能力",
               "獠牙 ACT1 的六维能力"]
        for q in seq:
            st, d = post(q, PORT)
            zc = d.get("zh_card") or {}
            check(f"「{q}」→ 200 且命中形态条目",
                  st == 200 and (zc.get("stand_id") or "").startswith("tusk_act")
                  and len(d.get("forms") or []) == 4,
                  f"HTTP {st} stand={zc.get('stand_id')} forms={len(d.get('forms') or [])}")
        # 拒答类（以前 500 的那条分支）
        for q in ["今天天气怎么样", "帮我写一首诗"]:
            st, d = post(q, PORT)
            check(f"拒答类「{q}」不返回 5xx", st < 500, f"HTTP {st}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except Exception:  # noqa: BLE001
            proc.kill()
        log.close()

    print()
    if fails:
        print(f"✗ {len(fails)}/{n} 项失败：" + "、".join(fails))
        return 1
    print(f"✓ 全部通过（{n} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
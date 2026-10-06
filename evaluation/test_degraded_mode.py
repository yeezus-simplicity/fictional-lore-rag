"""
降级模式回归测试（M10）。

★★ 为什么需要这个：
  Docker Desktop 未启动 → 数据库连不上 → 原先整个服务**直接崩**，
  连网页界面都打不开。但那时**语义检索其实完全可用**
  （M3 的索引是 pickle 缓存，不依赖数据库）。

  → 加了 DB 降级逻辑后必须有回归测试守住它，
    否则以后改代码又崩回去。

★★ 测法说明（这里也踩过坑）：
  **不能用 `spec_from_file_location` 加载 api/main.py**：
  那样模块名会变成 'apimod'，Pydantic 的 forward ref
  （Evidence 里的 Optional[str]）解析不到
  → 报「Evidence is not fully defined」。
  但 `python api/main.py` 正常启动时**完全正常** ——
  说明是**测试方式不对**，不是产品 bug。

  → 正确做法：**子进程起真实 uvicorn + HTTP 请求**（端到端）。

用法：
    python evaluation/test_degraded_mode.py
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        return int(sk.getsockname()[1])


def _start(port: int, env: dict, timeout: int = 70):
    """起服务并等它就绪。返回 None 表示失败。"""
    base = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "api" / "main.py"),
         "--port", str(port), "--no-vector"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace")
    for _ in range(timeout):
        time.sleep(1)
        try:
            urllib.request.urlopen(base + "/health", timeout=3)
            return proc
        except Exception:
            if proc.poll() is not None:
                out = proc.stdout.read()[-1200:] if proc.stdout else ""
                print("✗ 服务进程退出：\n" + out)
                return None
    return None


def _get(base: str, ep: str):
    return json.loads(urllib.request.urlopen(base + ep, timeout=25).read())


def _post(base: str, q: str, **kw):
    url = base + "/query?" + urllib.parse.urlencode({"q": q, **kw})
    r = urllib.request.Request(url, method="POST")
    return json.loads(urllib.request.urlopen(r, timeout=50).read())


def _stop(proc) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except Exception:
        proc.kill()


def main() -> int:
    print("=" * 68)
    print("降级模式回归测试（数据库不可用）")
    print("=" * 68)

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    # ★ PGPORT=1 → 必然连不上，模拟 Docker Desktop 未启动
    env = dict(os.environ, PGPORT="1")
    fails = 0

    print(f"\n[1] 服务应能启动（降级而非崩溃）  端口 {port}")
    proc = _start(port, env)
    if proc is None:
        return 1
    try:
        h = _get(base, "/health")
        if h.get("status") != "degraded":
            print(f"  ✗ status={h.get('status')}，期望 degraded")
            fails += 1
        else:
            print(f"  ✓ status={h['status']}  database={h['database']}")

        print("\n[2] 网页界面可访问（DB挂了也要能打开）")
        try:
            r = urllib.request.urlopen(base + "/", timeout=20)
            html = r.read().decode("utf-8")
            ok = "<title>" in html and "rag-kb" in html
            print(f"  {'✓' if ok else '✗'} 首页 {len(html)} 字节")
            fails += 0 if ok else 1
        except Exception as e:
            print(f"  ✗ 首页失败 {type(e).__name__}")
            fails += 1

        print("\n[3] 各路由的降级行为")
        cases = [
            ("Anubis 的外观形态方面有哪些描述？", "semantic", "snippet",
             "语义检索应正常"),
            ("Star Platinum 的破坏力是几级？", "structured", "none",
             "结构化降级为 none + 提示"),
            ("Star Platinum 的破坏力是几级？同时说明能力描述。", "hybrid",
             "none", "混合降级"),
            ("Star Platinum Ultimate 的能力值？", "abstain", "abstain",
             "拒答不依赖 DB"),
            ("Made in Heaven 的速度是多少？", "structured", "none",
             "异常值场景也走降级"),
        ]
        ok = 0
        for q, want_route, want_type, note in cases:
            try:
                r = _post(base, q, top_k=2)
                good = (r["route"] == want_route
                        and r["answer_type"] == want_type)
                ok += 1 if good else 0
                print(f"  {'✓' if good else '✗'} "
                      f"[{r['route']:10s}|{r['answer_type']:8s}] {note}")
            except Exception as e:
                print(f"  ✗ {type(e).__name__}: {q[:24]}")
        print(f"  → {ok}/{len(cases)} 通过")
        fails += len(cases) - ok

        print("\n[4] DB 依赖的端点应优雅降级（返回 error 而非 500）")
        for ep in ["/stats", "/conflicts?status=pending",
                   "/stands/star_platinum"]:
            try:
                r = _get(base, ep)
                good = isinstance(r, dict) and r.get("error")
                print(f"  {'✓' if good else '✗'} {ep:26s} "
                      f"→ {r.get('error') if good else '未返回 error'}")
                fails += 0 if good else 1
            except Exception as e:
                print(f"  ✗ {ep:26s} {type(e).__name__}")
                fails += 1

        print("\n[5] 静态资源")
        for ep in ["/docs", "/openapi.json"]:
            try:
                r = urllib.request.urlopen(base + ep, timeout=15)
                print(f"  ✓ {ep:16s} {r.status}")
            except Exception as e:
                print(f"  ✗ {ep:16s} {type(e).__name__}")
                fails += 1
    finally:
        _stop(proc)

    print("\n" + "=" * 68)
    if fails:
        print(f"✗ {fails} 项失败")
    else:
        print("✓ 全部通过")
    print("\n★ 降级的意义：DB 挂了仍能做语义问答 + 拒答")
    print("  不可用：结构化查询 / 数据浏览 / 冲突记录")
    print("  启动 DB：cd docker && docker compose up -d postgres")
    print("=" * 68)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

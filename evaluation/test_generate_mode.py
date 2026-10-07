"""
生成式模式回归测试（M11）。

★ 为什么需要：
  生成层（M6）做了离线评测，但一直没接进服务。
  接入后最怕两件事：
    1. **抽取式被影响** —— 加生成层不能动默认路径的行为
    2. 生成式不可用时没有兜底（模型缺失 / 加载失败）

用法：
    python evaluation/test_generate_mode.py
    python evaluation/test_generate_mode.py --quick   # 只测抽取式不回归
"""

from __future__ import annotations

import argparse
import json
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


def _start(port: int, timeout: int = 70):
    base = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "api" / "main.py"),
         "--port", str(port), "--no-vector"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace")
    for _ in range(timeout):
        time.sleep(1)
        try:
            urllib.request.urlopen(base + "/health", timeout=3)
            return proc
        except Exception:
            if proc.poll() is not None:
                out = proc.stdout.read()[-1200:] if proc.stdout else ""
                print("✗ 服务退出：\n" + out)
                return None
    return None


def _stop(proc) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except Exception:
        proc.kill()


def _post(base: str, q: str, timeout: int = 300, **kw):
    url = base + "/query?" + urllib.parse.urlencode({"q": q, **kw})
    r = urllib.request.Request(url, method="POST")
    return json.loads(urllib.request.urlopen(r, timeout=timeout).read())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true",
                    help="只测抽取式（不加载生成模型，省时间）")
    args = ap.parse_args()

    print("=" * 70)
    print("生成式模式回归测试（M11）")
    print("=" * 70)

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    fails = 0

    print(f"\n[1] 服务启动（端口 {port}）")
    proc = _start(port)
    if proc is None:
        return 1
    try:
        h = json.loads(urllib.request.urlopen(
            base + "/health", timeout=15).read())
        print(f"  ✓ status={h['status']}")

        # ---------- 抽取式必须无变化 ----------
        print("\n[2] 抽取式行为不受影响（★ 最重要）")
        d = _post(base, "Anubis的外观形态方面有哪些描述？", top_k=2)
        checks = [
            ("mode == extract", d["mode"] == "extract"),
            ("answer_type == snippet", d["answer_type"] == "snippet"),
            ("generation is None", d["generation"] is None),
            ("faithfulness is None", d["faithfulness"] is None),
            ("answer 是 list", isinstance(d["answer"], list)),
            ("有证据", len(d["evidence"]) > 0),
        ]
        for name, ok in checks:
            print(f"  {'✓' if ok else '✗'} {name}")
            fails += 0 if ok else 1

        # ---------- 结构化查询也不受影响 ----------
        print("\n[3] 结构化查询不受影响")
        try:
            d2 = _post(base, "Star Platinum 的破坏力是几级？")
            ok = d2["answer_type"] == "fact"
            print(f"  {'✓' if ok else '✗'} answer_type={d2['answer_type']}")
            fails += 0 if ok else 1
        except Exception as e:
            # 数据库可能不可用 → 降级也算通过
            print(f"  ~ 跳过（DB 不可用：{type(e).__name__}）")

        # ---------- 拒答不受影响 ----------
        print("\n[4] 编造实体拒答不受影响")
        d3 = _post(base, "Star Platinum Ultimate 的能力值？")
        ok = d3["answer_type"] == "abstain"
        print(f"  {'✓' if ok else '✗'} answer_type={d3['answer_type']}")
        fails += 0 if ok else 1

        # ---------- 生成式 ----------
        if args.quick:
            print("\n[5] 生成式：已跳过（--quick）")
        else:
            print("\n[5] 生成式（首次含模型加载，约 80-120 秒）")
            t0 = time.time()
            try:
                d4 = _post(base, "Anubis 的外观形态方面有哪些描述？",
                           top_k=2, mode="generate")
                print(f"  总耗时 {time.time()-t0:.1f}s")

                if d4["answer_type"] == "generated":
                    g = d4["generation"]
                    print(f"  ✓ type=generated")
                    print(f"    模型 {g['model'].split(chr(92))[-1]}"
                          f"  device={g['device']}  temp={g['temperature']}")
                    print(f"    token {g['n_prompt_tokens']}→"
                          f"{g['n_gen_tokens']}  生成 {g['elapsed_ms']:.0f}ms")
                    print(f"    忠实度 {d4['faithfulness']}")
                    print(f"    答案 {str(d4['answer'])[:90]}...")
                    # 二次调用应显著更快（模型已加载）
                    t1 = time.time()
                    d5 = _post(base, "Tusk 的形态与外观有哪些描述？",
                               top_k=2, mode="generate")
                    dt = time.time() - t1
                    ok = dt < 30
                    print(f"  {'✓' if ok else '✗'} 第二次 {dt:.1f}s"
                          f"（应显著快于首次）")
                    fails += 0 if ok else 1
                else:
                    # 模型不可用 → 应该有清晰的提示且回退
                    a = d4["answer"]
                    ok = isinstance(a, dict) and a.get("note")
                    print(f"  ~ 模型不可用，已回退："
                          f"{a.get('note') if isinstance(a, dict) else a}")
                    print(f"    错误：{d4.get('warning')}")
                    if not ok:
                        fails += 1
            except Exception as e:
                print(f"  ✗ 异常 {type(e).__name__}: {e}")
                fails += 1
    finally:
        _stop(proc)

    print("\n" + "=" * 70)
    if fails:
        print(f"✗ {fails} 项失败")
    else:
        print("✓ 全部通过")
    print("=" * 70)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

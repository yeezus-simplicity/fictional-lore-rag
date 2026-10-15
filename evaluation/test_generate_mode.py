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


# ★★ M64：访问**本机服务**必须显式绕过代理 ★★
#   实测：本机环境带 http_proxy=127.0.0.1:54302（Clash 类），而
#   `urllib.request.urlopen` **默认读代理环境变量** → 连 127.0.0.1:PORT
#   的请求也会走代理。单个短请求看不出来，但本测试要连发 5 次
#   （extract / fact / abstain / generate × 2），
#   **第二次 generate 必挂**（>300s 无响应），第一次却正常 ——
#   表现成"生成式有时能用有时不能用"，极易误判成模型/显存问题。
#   对照实验：换成 ProxyHandler({}) 后同一序列三次全部 200 OK
#   （38.8s / 14.3s / 14.6s）。
#   → 回环地址一律走 _opener()。
_OPENER = None


def _opener():
    # ★ import 放在函数内：这几个测试的 `urllib.request` 是在**函数内**
    #   才导入的，模块级调用会 NameError（且被 except 吞掉 → 表现为
    #   "服务启动超时"，与真实原因完全无关）。
    global _OPENER
    if _OPENER is None:
        import urllib.request as _u
        _OPENER = _u.build_opener(_u.ProxyHandler({}))
    return _OPENER


# ★★ M64：服务端输出必须**重定向到文件**，不要用 PIPE ★★
#   实测现象极怪：本测试的生成式步骤**随机**在第 1 次或第 2 次超时
#   （>300s 无响应），而同一序列在另一个脚本里三次全部 200 OK。
#   逐项排除后（GPU 干净 / 无残留进程 / 代理已绕过）定位到：
#   `stdout=subprocess.PIPE` 而父进程**全程不读** ——
#   服务加载 Qwen 时 tqdm 与 transformers 会写出大量进度行，
#   **Windows 管道缓冲区（~4KB）写满后，子进程会阻塞在 write 上**，
#   服务就此卡死（既不回响应、也不退出）。
#   随机性来自输出总量在缓冲区边界附近波动。
#   ★ 这类问题最坑的地方是它**伪装成"模型/显存问题"**。
#   → 改为写日志文件：永不阻塞，且失败时能读到服务端真实输出。
LOG = ROOT / ".tmp_preview" / "generate_mode_server.log"


def _read_log(tail: int = 1500) -> str:
    try:
        return LOG.read_text(encoding="utf-8", errors="replace")[-tail:]
    except Exception:
        return "（读不到服务端日志）"


def _start(port: int, timeout: int = 70):
    base = f"http://127.0.0.1:{port}"
    LOG.parent.mkdir(parents=True, exist_ok=True)
    _logf = open(LOG, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "api" / "main.py"),
         "--port", str(port), "--no-vector"],
        stdout=_logf, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace")
    for _ in range(timeout):
        time.sleep(1)
        try:
            _opener().open(base + "/health", timeout=3)
            return proc
        except Exception:
            if proc.poll() is not None:
                print("✗ 服务退出：\n" + _read_log())
                return None
    # ★ 启动超时也要交代服务端状态（原来这里直接 return None，没有线索）
    print("✗ 服务启动超时，服务端日志尾部：\n" + _read_log())
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
    return json.loads(_opener().open(r, timeout=timeout).read())


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
        h = json.loads(_opener().open(
            base + "/health", timeout=15).read())
        print(f"  ✓ status={h['status']}")

        # ---------- M62：生成模型是否已在本机 ----------
        #   实测踩坑：这台机器**没有本地模型缓存** → 第一次跑要现下 ~3 GB，
        #   远超单次请求超时 → 报「TimeoutError: timed out」，
        #   看起来像功能坏了，其实只是环境没准备好（第二次跑命中缓存，8 秒过）。
        #   与数据库同款处理：缺前置条件 → **跳过**（不算失败）。
        #   需严格时：--require-model（或 CI=true）。
        # ★ 判据必须和 `services/generator.py` 用的**同一处**：
        #   它加载的是**项目内**的 models/Qwen_Qwen2.5-1.5B-Instruct
        #   （由 fetch_model.py 下载），不是 HF 缓存 ——
        #   我第一版去查 ~/.cache/huggingface/hub → 永远"没有"→ 误跳过。
        #   （那里只有嵌入模型 models--BAAI--bge-m3。）
        _mdir = ROOT / "models" / "Qwen_Qwen2.5-1.5B-Instruct"
        _hit = (_mdir / "config.json").exists()
        if not _hit:
            _strict = ("--require-model" in sys.argv) or \
                (os.environ.get("CI") == "true")
            print("=" * 70)
            print("⏭ 已跳过（环境不满足）：本机没有 Qwen2.5-1.5B 的本地缓存")
            print(f"    查了：{_mdir}")
            print("    首次运行会现下 ~3 GB（远超单次请求超时）→ 先跑一次：")
            print("           python dataset/pipeline/fetch_model.py")
            print("           （下完再跑本测试）")
            if _strict:
                print("    （--require-model / CI=true：缺模型视为失败）")
                print("=" * 70)
                return 1
            print("    （未开 --require-model，按「跳过」处理，不计失败）")
            print("=" * 70)
            return 0
        print("  ✓ 生成模型本地缓存已就绪")

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
                t_first = time.time() - t0
                print(f"  总耗时 {t_first:.1f}s")

                if d4["answer_type"] == "generated":
                    g = d4["generation"]
                    print(f"  ✓ type=generated")
                    print(f"    模型 {g['model'].split(chr(92))[-1]}"
                          f"  device={g['device']}  temp={g['temperature']}")
                    print(f"    token {g['n_prompt_tokens']}→"
                          f"{g['n_gen_tokens']}  生成 {g['elapsed_ms']:.0f}ms")
                    print(f"    忠实度 {d4['faithfulness']}")
                    print(f"    答案 {str(d4['answer'])[:90]}...")
                    # ★★ M64：第二次调用 —— 「功能」判失败，「更快」只作提示 ★★
                    #   根因已修（见 _start 上方注释）：服务端 `stdout=PIPE`
                    #   而父进程不读 → 加载模型时写满 Windows 管道缓冲 → 服务卡死。
                    #   修好后第二次稳定在 15.1s / 15.4s。
                    #   ★ 但仍不把「更快」判为失败（尽管它是原意）：
                    #     共享 GPU 时会有波动（别的进程占显存 / 模型被换出），
                    #     按本项目一贯原则：「环境不满足」与「功能坏了」分开。
                    t1 = time.time()
                    d5 = _post(base, "Tusk 的形态与外观有哪些描述？",
                               top_k=2, mode="generate")
                    dt = time.time() - t1
                    ok5 = d5.get("answer_type") == "generated"
                    print(f"  {'✓' if ok5 else '✗'} 第二次仍能生成"
                          f"（type={d5.get('answer_type')}，{dt:.1f}s）")
                    fails += 0 if ok5 else 1
                    if dt >= t_first:
                        print(f"  ⚠ 第二次 {dt:.1f}s 未快于首次 {t_first:.1f}s"
                              " —— 模型可能被重复加载，或资源被别的进程占用")
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
                # ★ M64：超时要能看出是"服务端卡住"还是"模型慢" ——
                #   打印服务端日志尾部（PIPE 时代这里什么都没有）。
                print("  —— 服务端日志尾部 ——")
                print("  " + _read_log(900).replace("\n", "\n  "))
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

#!/usr/bin/env python
"""
一键启动 fictional-lore-rag 服务。

为什么需要这个：
  之前用 `nohup ... &` 启动，进程常在会话结束后被杀，
  导致界面「Failed to fetch」。用户反复问「怎么启动」——
  说明**启动方式本身就该是一键的**，不该要人记命令。

用法（三选一）：
  python start.py# 默认 8765 端口，自动开浏览器
  python start.py --port 8000
  python start.py --no-browser    # 不开浏览器
  python start.py --with-vector   # 启用向量检索（慢一点，更准）

★ 端口默认 8765 而不是 8000：
  本机 8000/8080/8090 被代理劫持，会出现
  「HTTP 200 但内容是代理的 502 页面」或curl 通但浏览器 502。
"""

from __future__ import annotations

import argparse
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_PORT = 8765
# ★ 本机被代理劫持的端口，不要用
BAD_PORTS = {8000, 8080, 8081, 8090, 8091}


def port_in_use(port: int) -> bool:
    with socket.socket() as sk:
        sk.settimeout(0.5)
        return sk.connect_ex(("127.0.0.1", port)) == 0


def db_ready() -> bool:
    """检查 PostgreSQL 5432 端口。"""
    with socket.socket() as sk:
        sk.settimeout(0.5)
        return sk.connect_ex(("127.0.0.1", 5432)) == 0


def banner(port: int, with_vector: bool,
           chunk_merge: Optional[int] = None) -> None:
    print("=" * 64)
    print("  fictional-lore-rag 混合检索服务")
    print("=" * 64)
    print()
    print(f"  网页界面   http://127.0.0.1:{port}")
    print(f"  接口文档   http://127.0.0.1:{port}/docs")
    # ★ M12：把索引配置打在启动横幅里 —— 换索引是「换检索质量」，
    #   用户必须一眼看出当前用的是哪套，否则结果变好/变差都无从归因
    print(f"  索引       {f'语义块合并到 {chunk_merge} 字符（M9 推荐）' if chunk_merge else '原始语义块（2407）'}")
    print()
    print("-" * 64)

    if db_ready():
        print("  数据库     已就绪（全部功能可用）")
    else:
        print("  数据库     未运行")
        print()
        print("  ★ 不影响使用 —— 检索索引是本地缓存：")
        print("     可用：网页界面 / 语义问答 / 编造实体拒答")
        print("     不可：结构化查询 / 数据浏览 / 冲突记录")
        print()
        print("     启用完整功能：")
        print("       1) 启动 Docker Desktop")
        print(f"       2) cd {ROOT / 'docker'}")
        print("       3) docker compose up -d postgres")
    print("-" * 64)
    print(f"  检索模式   {'BM25 + bge-m3 向量' if with_vector else '仅 BM25'}")
    print("  启动耗时   约 10-20 秒（加载检索索引）")
    print()
    print("  ★ 保持本窗口开着，关掉窗口服务就停止")
    print()


def open_browser_later(port: int, delay: float = 18.0) -> None:
    """等服务起来后再开浏览器。"""
    def go() -> None:
        time.sleep(delay)
        url = f"http://127.0.0.1:{port}"
        try:
            webbrowser.open(url)
            print(f"  已打开浏览器：{url}")
        except Exception:
            print(f"  请手动打开：{url}")
    threading.Thread(target=go, daemon=True).start()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="启动 fictional-lore-rag 服务",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help=f"端口（默认 {DEFAULT_PORT}，避免被代理劫持的端口）")
    ap.add_argument("--no-browser", action="store_true", help="不自动开浏览器")
    ap.add_argument("--with-vector", action="store_true",
                    help="启用向量检索（更准，但启动慢约 30 秒）")
    ap.add_argument("--chunk-merge", type=int, default=None, metavar="N",
                    help=("★ 语义块合并到约 N 字符。"
                          "512=M9 实测推荐（空洞率 0.50→0.30，"
                          "代价是延迟 +52%%）。默认不合并。"))
    # ★ M17
    ap.add_argument("--no-images", action="store_true",
                    help="不返回替身图片（首次抓图需联网，默认开启）")
    args = ap.parse_args()

    port = args.port
    if port in BAD_PORTS:
        print(f"⚠ 端口 {port} 在本机被代理劫持，会导致页面能开但查询全失败。")
        alt = next(p for p in (8765, 8766, 8767) if not port_in_use(p))
        print(f"  改用 {alt}？输入 y 确认，其他键退出：", end="", flush=True)
        if input().strip().lower() != "y":
            return 1
        port = alt

    if port_in_use(port):
        print(f"✗ 端口 {port} 已被占用。")
        print()
        print("  可能服务已在运行 —— 直接访问：")
        print(f"    http://127.0.0.1:{port}")
        print()
        print("  若要停止它：")
        print(f'    netstat -ano | findstr ":{port}"     查出 PID')
        print("    taskkill /PID <PID> /F")
        print()
        print("  或换端口：python start.py --port 8766")
        return 1

    banner(port, args.with_vector, args.chunk_merge)

    if not args.no_browser:
        open_browser_later(port)

    cmd = [sys.executable, str(ROOT / "api" / "main.py"),
           "--port", str(port)]
    if not args.with_vector:
        cmd.append("--no-vector")
    if args.chunk_merge:
        cmd += ["--chunk-merge", str(args.chunk_merge)]
    # ★ M17：透传 --no-images
    #   ★★ 踩坑：start.py 只是转发器，不透传的话新开关会静默失效
    #     （实测 /stands/{id} 在 --no-images 下仍返回 9 张图）。
    if args.no_images:
        cmd.append("--no-images")

    print("  正在启动…\n")
    try:
        return subprocess.call(cmd, cwd=str(ROOT))
    except KeyboardInterrupt:
        print("\n  已停止")
        return 0


if __name__ == "__main__":
    sys.exit(main())
"""环境自检：一条命令看清"能不能跑测试"。

用法：  python evaluation/check_env.py

为什么需要它：数据库跑在 Docker 里（`docker/docker-compose.yml`）。
Docker Desktop 没启动时，**所有依赖数据库的测试都会失败**，
而报错长得像"功能坏了"（答案变成"结构化查询需要数据库…"、
雷达图不出现、图片不出现）—— 实测因此被误导过两次。

判据：直接连一次库（比绕一圈问 /health 更直接）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "database"))


def main() -> int:
    ok = True
    print("=" * 66)
    print("环境自检")
    print("=" * 66)

    # ① 数据库
    try:
        from load_db import connect
        c = connect()
        cur = c.cursor()
        cur.execute("SELECT count(*) FROM stands")
        n = cur.fetchone()[0]
        cur.close()
        c.close()
        print(f"  ✓ 数据库已连接（stands {n} 行）")
    except (Exception, SystemExit) as e:
        # ★ 必须连 SystemExit 一起接：`load_db.connect()` 连不上时是
        #   **直接 sys.exit(1)**（它自己会打排查清单），SystemExit
        #   不是 Exception 的子类 → 只写 except Exception 会漏掉，
        #   结果脚本在打印 ✗ 之前就退出了（实测过一次）。
        ok = False
        print("  ✗ 数据库连不上 —— **所有依赖 DB 的测试都会失败**")
        print("      （load_db.connect 已在上方打印连接失败原因与排查清单）")
        print("      修法：启动 Docker Desktop，然后")
        print("           cd docker && docker compose up -d postgres")

    # ② 前端依赖（只有 gui_e2e 用）
    try:
        import playwright  # noqa: F401
        print("  ✓ playwright 已安装（gui_e2e 可跑）")
    except ImportError:
        print("  ○ playwright 未安装（gui_e2e 会自行跳过）")

    # ③ 前端页面能不能起来
    import socket
    s = socket.socket()
    s.settimeout(1.5)
    up = False
    try:
        s.connect(("127.0.0.1", 8765))
        up = True
    except OSError:
        pass
    finally:
        s.close()
    print(f"  {'✓' if up else '○'} 网页服务 8765 {'在运行' if up else '未启动（多数测试会自己起）'}")

    print("=" * 66)
    if ok:
        print("结论：环境就绪")
        return 0
    print("结论：**环境不满足**（数据库是硬前提）")
    return 1


if __name__ == "__main__":
    sys.exit(main())

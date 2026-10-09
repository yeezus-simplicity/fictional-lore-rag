"""后台常驻启动服务（Windows Git Bash 友好）。

★★ 为什么不用 bash 的 nohup/& ★★
实测（本机 Git Bash）：
  - `nohup ... &` 起的进程会随Bash 工具调用结束而被回收
    → 后续命令测端口一律ConnectionRefused（netstat 只剩 TIME_WAIT）
  - `setsid` 命令不存在（Windows Git Bash 没这个工具）
→ 正解：用 subprocess 以 DETACHED_PROCESS 启动，彻底脱离控制台，
  这样本次会话后续命令都能连上。

用法：python run_server_bg.py [--port 8765] [--chunk-merge 512]
"""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable


def main() -> int:
    port = 8765
    merge = 512
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--port" and i + 1 < len(args):
            port = int(args[i + 1])
        if a == "--chunk-merge" and i + 1 < len(args):
            merge = int(args[i + 1])

    cmd = [PY, str(ROOT / "start.py"), "--port", str(port),
           "--chunk-merge", str(merge)]
    log = open(ROOT / "images" / "_server.log", "ab", buffering=0)
    DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
    CREATE_NEW = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    p = subprocess.Popen(cmd, cwd=str(ROOT), stdout=log, stderr=log,
                         stdin=subprocess.DEVNULL,
                         creationflags=DETACHED | CREATE_NEW)
    print(f"已后台启动 PID={p.pid}  端口 {port}  合并 {merge}")
    print(f"日志 images/_server.log")
    for i in range(30):
        time.sleep(3)
        import socket
        s = socket.socket()
        s.settimeout(1.5)
        try:
            s.connect(("127.0.0.1", port))
            s.close()
            print(f"就绪（约 {(i+1)*3} 秒）")
            return 0
        except OSError:
            s.close()
    print("超时未就绪，请查 images/_server.log")
    return 1


if __name__ == "__main__":
    sys.exit(main())
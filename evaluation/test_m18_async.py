"""验证 M18 的异步图片方案（自起服务 → 断言 → 关服务，一个进程内）。

★ 要证明的三件事 ★★
1. 回答**立即返回**（不再是 87 秒）—— 没缓存的替身也应是秒级
2. 图片在后台**最终真的出现**（轮询 /images/status）
3. 已有缓存的替身**仍然秒出图**（没有回归）

★★为什么必须这么测 ★★
沙箱会在每次 Bash 调用结束时回收子进程，
「A 命令起服务 → B 命令测接口」必然失败（B 时服务已死）。
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
PORT = 8796
PY = sys.executable
SLOW = 8.0      # 回答耗时上限（秒）——异步方案下没缓存也该很快


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


def req(path: str, method: str = "GET", timeout: float = 60.0):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=timeout)
    t = time.time()
    try:
        c.request(method, path,
                  headers={"Content-Type": "application/json"}
                  if method == "POST" else {})
        r = c.getresponse()
        return r.status, time.time() - t, r.read()
    finally:
        c.close()


def main() -> int:
    fails: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}"
              f"{'' if ok else '  ← ' + detail}")
        if not ok:
            fails.append(name)

    log = open(ROOT / "images" / "_m18.log", "wb")
    proc = subprocess.Popen(
        [PY, str(ROOT / "api" / "main.py"), "--port", str(PORT),
         "--no-vector", "--chunk-merge", "512"],
        cwd=str(ROOT), stdout=log, stderr=log, stdin=subprocess.DEVNULL)
    try:
        if not wait_ready(PORT):
            print("服务启动超时")
            return 1
        print(f"服务就绪 :{PORT}\n")

        # ==========1) 已缓存替身：秒出图 ==========
        print("[1] 已缓存替身（star_platinum）")
        st, dt, d = req("/query?q=" + quote("Star Platinum 的能力")
                        + "&top_k=3", "POST")
        check(f"HTTP 200（{dt:.2f}s）", st == 200, f"got {st}")
        check(f"回答耗时 < {SLOW}s（实际 {dt:.2f}s）", dt < SLOW, f"{dt:.2f}s")
        j = json.loads(d)
        im = j.get("stand_images") or {}
        check("已缓存 → state=done", im.get("state") == "done",
              str(im.get("state")))
        check(f"已缓存 → 图片 {im.get('total', 0)} 张 > 0",
              (im.get("total") or 0) > 0, str(im)[:60])
        check("雷达图仍在", bool(j.get("radar_svg")))

        # ========== 2) 没缓存替身：回答也要快 ==========
        print("\n[2] 没缓存替身（异步关键：回答不能等抓图）")
        # ★ 必须挑一个本地**确实没有**缓存的替身，
        #   否则它立刻 state=done，测不到"后台任务真的跑起来了"。
        #   → 先删掉缓存目录，确保从零开始。
        import shutil
        cold_id = "c_moon"
        shutil.rmtree(ROOT / "images" / cold_id, ignore_errors=True)
        st, dt, d = req("/query?q=" + quote("C-Moon 的能力")
                        + "&top_k=3", "POST")
        check(f"HTTP 200（{dt:.2f}s）", st == 200, f"got {st}")
        check(f"★ 回答耗时 < {SLOW}s（实际 {dt:.2f}s）—— 不阻塞",
              dt < SLOW, f"{dt:.2f}s 说明还在同步等抓图")
        j2 = json.loads(d)
        im2 = j2.get("stand_images") or {}
        print(f"       state={im2.get('state')} total={im2.get('total')}"
              f" stand_id={im2.get('stand_id')}")
        check("★ 无缓存时 state=pending（不是 done）",
              im2.get("state") == "pending",
              f"state={im2.get('state')} 说明本地已有缓存，换个替身重测")
        check("pending 必须带 stand_id（前端要靠它轮询）",
              bool(im2.get("stand_id")), str(im2)[:80])
        check("雷达图仍然立即返回（不依赖图片）",
              bool(j2.get("radar_svg")))

        # ========== 3) 轮询直到图片出现 ==========
        print("\n[3] 轮询 /images/status 直到图片就绪")
        sid = im2.get("stand_id") or cold_id
        got, waited, last_state = False, 0.0, None
        stt: dict = {}
        t0 = time.time()
        while time.time() - t0 < 180:
            s2, _, b2 = req(f"/images/status/{sid}")
            stt = json.loads(b2)
            last_state = stt.get("state")
            if last_state == "done" and (stt.get("total") or 0) > 0:
                got = True
                waited = time.time() - t0
                break
            if last_state == "failed":
                break
            time.sleep(2)
        check(f"图片最终出现（等待 {waited:.0f}s，最后状态 {last_state}）",
              got, f"最后状态 {last_state}")
        if got:
            # 抽一张实际取一下，确认文件真在
            url = None
            for k in ("stand", "user", "manga", "anime", "misc"):
                if stt.get(k):
                    url = stt[k][0]["url"]
                    break
            if url:
                s3, _, b3 = req(url)
                magic = ("PNG" if b3[:4] == bytes([137, 80, 78, 71])
                         else "JPEG" if b3[:2] == bytes([255, 216]) else "?")
                check(f"图片可取{s3}（{len(b3)}B {magic}）",
                      s3 == 200 and magic != "?", f"{s3} {magic}")

        # ========== 4) 再问一次：有缓存应秒出 ==========
        print("\n[4] 抓完后同一替身再问（应秒出图）")
        st, dt, d = req("/query?q=" + quote("C-Moon 的能力")
                        + "&top_k=3", "POST")
        j4 = json.loads(d)
        im4 = j4.get("stand_images") or {}
        check(f"二次提问快（{dt:.2f}s）且图已就绪",
              dt < SLOW and im4.get("state") == "done"
              and (im4.get("total") or 0) > 0,
              f"{dt:.2f}s state={im4.get('state')} total={im4.get('total')}")

        # ========== 5) 无关问句仍不配图 ==========
        print("\n[5] 无关问句不该起抓图任务")
        st, dt, d = req("/query?q=" + quote("今天天气怎么样")
                        + "&top_k=3", "POST")
        j5 = json.loads(d)
        check("无关问句 → 无图、无雷达图",
              not (j5.get("stand_images") or {}).get("total")
              and not j5.get("radar_svg"),
              f"img={j5.get('stand_images')} radar={bool(j5.get('radar_svg'))}")

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
"""一次性验证 M17 的图片与雷达图链路（自起服务 → 测 → 关）。

★★ 为什么必须写成一个脚本 ★★
沙箱会在每次 Bash 调用结束时回收子进程（实测 nohup/setsid/DETACHED
都挡不住），所以「A 命令起服务、B 命令测接口」这种两段式必然失败：
B 命令执行时服务已被回收 → ConnectionRefused。
→ 只能把「起服务 + 等就绪 + 发请求 + 断言 + 关服务」写进同一个进程。
"""
from __future__ import annotations

import http.client
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]   # ★ 项目根（不是 evaluation/）
PORT = 8799                                  # 独立端口，避开 8765 占用/代理干扰
PY = sys.executable


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


def hit(port: int, path: str, method: str = "GET", body: str = ""
        ) -> tuple[int, str, bytes]:
    """发请求。★ path 必须已 percent-encode，否则中文会 UnicodeEncodeError
    （http.client 的 putrequest 只接受 ASCII）。

    ★★ 踩坑：HTTP 4xx/5xx 时 getresponse() 返回的response 对象带status，
      **不会抛异常**（抛异常的是 send/putrequest 阶段的连接错误）。
      所以早期版本用 try/except 抓 status 全都漏了，
      把"被正确拒绝的 404/400"误报成"竟返回 404"→ 假失败。
    → 现在直接返回 status，由调用方断言。
    """
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    try:
        hdr = {"Content-Type": "application/json"} if body else {}
        c.request(method, path, body=body.encode("utf-8") if body else None,
                  headers=hdr)
        r = c.getresponse()
        data = r.read()
        return r.status, r.getheader("Content-Type") or "", data
    finally:
        c.close()


def q_path(q: str, top_k: int = 3) -> str:
    """把问句拼成已编码的 query 路径。"""
    from urllib.parse import quote
    return f"/query?q={quote(q)}&top_k={top_k}"


def main() -> int:
    log = open(ROOT / "images" / "_e2e.log", "wb")
    # ★ 直调 api/main.py，绕开 start.py（它默认会尝试开浏览器，
    #   非交互环境会卡住；而且它是转发层，多一层就多一个排查点）
    proc = subprocess.Popen(
        [PY, str(ROOT / "api" / "main.py"), "--port", str(PORT),
         "--no-vector", "--chunk-merge", "512"],
        cwd=str(ROOT), stdout=log, stderr=log, stdin=subprocess.DEVNULL)
    fails: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}"
              f"{'' if ok else '  ← ' + detail}")
        if not ok:
            fails.append(name)

    try:
        if not wait_ready(PORT):
            print("服务启动超时，见 images/_e2e.log")
            print((ROOT / "images" / "_e2e.log")
                  .read_text(encoding="utf-8", errors="ignore")[-1500:])
            return 1
        print(f"服务就绪 :{PORT}\n")

        # ---------- 1. /stands/{id} 雷达图 + 图片 ----------
        print("[1] /stands/{id} 雷达图 + 图片")
        st, _, b = hit(PORT, "/stands/achtung_baby")
        check("GET /stands/achtung_baby -> 200", st == 200, f"got {st}")
        if st == 200:
            d = json.loads(b)
            svg = d.get("radar_svg") or ""
            check("含 radar_svg", "<svg" in svg, f"len={len(svg)}")
            det = d.get("stats_detail") or {}
            check("stats_detail 有 missing 名单", "missing" in det,
                  str(det)[:80])
            # ★ 缺失维度必须在图上标注「无数据」且不能出现 0 值点
            miss = det.get("missing") or []
            if miss:
                check(f"缺失维度 {miss} 图上标注无数据",
                      "无数据" in svg and "未推测为 0" in svg,
                      "缺标注")
            im = d.get("images") or {}
            check("图片总数 > 0", (im.get("total") or 0) > 0, str(im)[:80])
            for kind in ("stand", "user", "manga"):
                check(f"含 {kind} 类图", bool(im.get(kind)),
                      f"有={list(im.keys())}")

        # ---------- 2. 图片文件真能取到 ----------
        print("\n[2] 图片文件可加载 + 安全防护")
        if st == 200:
            im = (json.loads(b).get("images") or {})
            for kind in ("stand", "user", "manga"):
                for item in (im.get(kind) or [])[:1]:
                    s2, ct2, b2 = hit(PORT, item["url"])
                    magic = ("PNG" if b2[:4] == bytes([137, 80, 78, 71])
                             else "JPEG" if b2[:2] == bytes([255, 216]) else "?")
                    check(f"{kind} 图可取{s2}", s2 == 200 and magic != "?",
                          f"{s2} {ct2} magic={magic} {len(b2)}B")
        for bad, why in (("/images/..%2F..%2Fstart.py", "路径穿越"),
                         ("/images/achtung_baby/_images.json", "清单文件"),
                         ("/images/achtung_baby/nope.png", "不存在")):
                s3, _, _ = hit(PORT, bad)
                check(f"{why}应拒绝(得{s3})",
                      s3 in (400, 404), f"竟返回 {s3}")

        # ---------- 3. 查询接口带图 ----------
        print("\n[3] /query 问属性/问替身")
        for q, want_svg, want_img in (
            ("透明宝宝的六维能力", True, True),
            ("Achtung Baby 的能力", True, True),
            ("今天天气怎么样", False, False),      # 无关问句：不该配图
        ):
            stq, _, bq = hit(PORT, q_path(q), "POST")
            if stq != 200:
                check(f"query {q[:14]}", False, f"HTTP {stq}")
                continue
            dq = json.loads(bq)
            got_svg = bool(dq.get("radar_svg"))
            got_img = bool((dq.get("stand_images") or {}).get("total"))
            check(f"{q[:16]} 雷达图={got_svg}(期望{want_svg})",
                  got_svg == want_svg)
            check(f"{q[:16]} 图片={got_img}(期望{want_img})",
                  got_img == want_img)

        # ---------- 4. --no-images 开关 ----------
        print("\n[4] --no-images 关闭开关")
        proc.terminate()
        proc.wait(timeout=20)
        log2 = open(ROOT / "images" / "_e2e2.log", "wb")
        # ★ 直接调 api/main.py（不经过 start.py）：
        #   start.py 默认会 open_browser_later()，非交互环境会卡住，
        #   而且它是转发层，多一层就多一个开关失效的排查点。
        #   ★★ 本次真bug 就是「开关只写在 app.state、没传进 STATE」，
        #      而这只有直连 api/main.py 才测得到。
        proc2 = subprocess.Popen(
            [PY, str(ROOT / "api" / "main.py"), "--port", str(PORT),
             "--no-vector", "--chunk-merge", "512", "--no-images"],
            cwd=str(ROOT), stdout=log2, stderr=log2, stdin=subprocess.DEVNULL)
        try:
            if wait_ready(PORT):
                stn, _, bn = hit(PORT, "/stands/achtung_baby")
                dn = json.loads(bn)
                check("--no-images 时无 images",
                      not (dn.get("images") or {}).get("total"),
                      str(dn.get("images"))[:60])
                check("--no-images 时雷达图仍可用",
                      "<svg" in (dn.get("radar_svg") or ""), "雷达图也关了")
            else:
                check("--no-images 模式启动", False, "超时")
        finally:
            proc2.terminate()
            try:
                proc2.wait(timeout=20)
            except Exception:
                proc2.kill()

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
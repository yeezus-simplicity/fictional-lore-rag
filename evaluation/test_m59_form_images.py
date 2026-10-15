"""M59 回归测试：形态（ACT）要有**自己的图**，不能四份一模一样。

用户反馈：「为什么 act1-4 的图片都是一样的呢」

== 实测根因（两层）==
【1】源站没有形态页
    jojowiki 上 `Tusk ACT1`~`ACT4`、`Echoes ACT1`~`ACT3` 全部**重定向到主条目**
    → 用形态标题去抓，拿到的必然是主条目那一批文件
    → 五个目录（母体 + 4 形态）的本体图 **md5 逐字节相同**
【2】形态专属图其实存在，只是没去分
    主条目页里就有：`Tusk ACT1 SBR Infobox Manga.png` …
    `Echoes ACT3 DU Infobox Manga.png`（Tusk 5 张、Echoes 10 张）
    → 之前没按 `ACTn` 过滤，全被当成了"这个替身的图"

== 顺带发现的数据 bug ==
  抓取标题用的是 **`Echoes (ACT1)`（带括号）**，而那个标题在 jojowiki 上
  **根本不存在** → Echoes 的三个形态是**零图**（`Echoes ACT1` 才存在）。

== 本轮改法 ==
  形态成员（`*_actN`）：页面标题用 `<母体> ACTn`，且**只收文件名匹配 ACTn 的图**
  母体：自身无独立本体图 → 收全部，但**带 ACT 的优先**，图上标出 ACTn（一份"形态图集"）
  前端：题注优先用 manifest 的 caption

== 本测试守什么 ==
  1. ACT 判别**不能用 `\\bact\\d`** —— api.php 返回的文件名里空格已换成下划线
     （`Tusk_ACT2_...`），`_` 是单词字符，`\\b` 在它前面不成立
     → 实测 act_label 全返回 None，且按 ACT 过滤会把形态图**全滤光**（零图）
  2. 每个形态成员**必须有自己的图**，且题注 == 自己的 ACTn
  3. **不同形态的图不能字节相同**（用户报的原始症状）
  4. 母体的本体图优先是形态图（不是周边商品）
  5. 端到端：/query 的 stand_images 带 caption
"""
from __future__ import annotations

import hashlib
import http.client
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
IMG = ROOT / "images"
FETCHER = ROOT / "dataset" / "sources" / "images" / "fetch_stand_images.py"
HTML = ROOT / "api" / "static" / "index.html"
MEDIA = ROOT / "services" / "media.py"
PORT = 8804

FORMS = {"tusk": ["tusk_act1", "tusk_act2", "tusk_act3", "tusk_act4"],
         "echoes": ["echoes_act1", "echoes_act2", "echoes_act3"]}


def load_fetcher():
    spec = importlib.util.spec_from_file_location("fsi59", FETCHER)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


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

    fs = load_fetcher()
    src = HTML.read_text(encoding="utf-8")
    media = MEDIA.read_text(encoding="utf-8")
    # ★★ M63：CI / 新克隆没有图片缓存 → 本测试无可检查项 ★★
    #   `images/` 在 .gitignore 里（约 400MB），GitHub Actions 的 checkout 是空的。
    #   这些测试逐条核对的正是"抓取产物"（缩略图高度 / 形态专属图），
    #   没有缓存就没有可检查的东西 → **跳过不算失败**（与 DB 前置检查同一套处理）。
    #   本地抓图：python dataset/sources/images/fetch_stand_images.py --from-stands
    if not any(IMG.glob("*/_images.json")):
        print("=" * 74)
        print("⏭ 已跳过（环境不满足）：本地没有图片缓存（images/ 被 .gitignore）")
        print("    本测试逐条核对抓取产物，无缓存则无可检查项")
        print("    抓图：python dataset/sources/images/fetch_stand_images.py --from-stands")
        print("=" * 74)
        return 0


    print("=" * 74)
    print("M59：形态（ACT）要有自己的图")
    print("=" * 74)

    # ---- ① ACT 判别的边界（下划线坑）----
    print("\n[1] ACT 判别：文件名里空格已换成下划线")
    for f, want in [("Tusk_ACT2_SBR_Infobox_Manga.png", "ACT2"),
                    ("Echoes_ACT3_DU_Infobox_Manga.png", "ACT3"),
                    ("Act2johnny.jpg", "ACT2"),
                    ("Act_4_SBR_Infobox_Manga.png", "ACT4"),
                    ("Contact2.jpeg", None),          # 不能把 cont**act2** 当 ACT2
                    ("Echoes_ACT1_VA_Infobox_Manga.png", "ACT1")]:
        got = fs.act_label(f)
        check(f"act_label({f[:36]}) = {want!r}", got == want, f"实际 {got!r}")
    check("form_no(tusk_act3) = 3", fs.form_no("tusk_act3") == 3)
    check("form_no(star_platinum) = None", fs.form_no("star_platinum") is None)

    # ---- ② 形态必须有自己的图 ----
    print("\n[2] 每个形态成员有自己的图，且题注是自己的 ACTn")
    hashes: dict[str, str] = {}
    for base, members in FORMS.items():
        for i, sid in enumerate(members, 1):
            d = IMG / sid
            mf = d / "_images.json"
            if not mf.exists():
                check(f"{sid} 有清单", False, "目录/清单不存在（该形态零图）")
                continue
            man = json.loads(mf.read_text(encoding="utf-8"))
            stand = [x for x in man.get("images", []) if x.get("kind") == "stand"]
            check(f"{sid} 有本体图（原来 Echoes 形态是 0 张）", bool(stand),
                  f"实际 {len(stand)} 张")
            caps = {x.get("caption") for x in stand}
            check(f"{sid} 的本体图题注都是 ACT{i}",
                  caps == {f"ACT{i}"}, f"实际 {caps}")
            for x in stand:
                f = d / x["file"]
                if f.exists():
                    hashes.setdefault(hashlib.md5(f.read_bytes()).hexdigest(),
                                      []).append(sid)

    # ---- ③ 不同形态不能字节相同（用户报的原始症状）----
    print("\n[3] 不同形态的图不重复")
    dup = {h: v for h, v in hashes.items()
           if len({s.rsplit("_", 1)[0] for s in v}) > 1}
    check(f"不同形态之间没有共用同一张图（重复 {len(dup)} 组）", not dup,
          str(list(dup.values())[:3]))
    n_forms = len({s.rsplit("_", 1)[0] for s in hashes})
    check(f"形态图集覆盖 {n_forms} 个形态", n_forms >= 6, f"实际 {n_forms}")

    # ---- ④ 母体：本体图优先形态图 ----
    print("\n[4] 母体的图片是一份「形态图集」")
    for base in FORMS:
        mf = IMG / base / "_images.json"
        man = json.loads(mf.read_text(encoding="utf-8"))
        stand = [x for x in man.get("images", []) if x.get("kind") == "stand"]
        acts = [x.get("caption") for x in stand if x.get("caption")]
        check(f"{base} 的本体图里有形态图（题注 ACTn）", bool(acts),
              f"题注 {[(x.get('file'), x.get('caption')) for x in stand][:3]}")
        merch = [x for x in stand if re_merch(x.get("source_name", ""))]
        check(f"{base} 的本体图里没有周边商品摆件", not merch,
              str([x.get("source_name") for x in merch][:2]))

    # ---- ⑤ 链路：manifest → API → 前端题注 ----
    print("\n[5] 题注一路传到界面上")
    check("抓取器把 caption 写进清单",
          '"caption": act_label(fname)' in FETCHER.read_text(encoding="utf-8"))
    check("public_images 优先用 caption",
          'item.get("caption") or item.get("source_name"' in media)
    check("前端 figcaption 优先用 caption",
          "esc(x.caption||IMG_KIND_CN[k]||k)" in src)
    check("形态成员按 ACTn 过滤（page_images 收 form_n）",
          "form_n: Optional[int] = None" in FETCHER.read_text(encoding="utf-8"))
    check("形态页面标题用 <母体> ACTn（Echoes 带括号的标题不存在）",
          'page_title = f"{_base} ACT{form_n}"' in FETCHER.read_text(encoding="utf-8"))

    # ---- ⑥ 端到端 ----
    print(f"\n[6] 端到端（127.0.0.1:{PORT}）")
    log = (ROOT / ".tmp_preview" / "_m59_server.log").open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "api" / "main.py"),
         "--port", str(PORT), "--no-vector"],
        cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL)
    try:
        if not wait_http(PORT):
            check("服务就绪", False, "超时")
            return 1
        for q, want_n in [("獠牙ACT2 的六维能力", 2), ("回声 ACT3 的破坏力", 3)]:
            c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=60)
            try:
                c.request("POST", f"/query?q={quote(q)}&mode=extract",
                          headers={"Content-Type": "application/json"})
                d = json.loads(c.getresponse().read())
            finally:
                c.close()
            st = (d.get("stand_images") or {}).get("stand") or []
            caps = {x.get("caption") for x in st}
            check(f"「{q}」的本体图带题注 ACT{want_n}",
                  bool(st) and caps <= {f"ACT{want_n}"},
                  f"实际 {len(st)} 张 题注 {caps}")
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


def re_merch(name: str) -> bool:
    """周边商品/摆件类文件名（不是替身本体图）。"""
    import re
    return bool(re.search(r"Acrylic|Vol\d|Jump Shop|JOJO ?CAFE|Figure|Figure Stand",
                         name or "", re.I))


if __name__ == "__main__":
    sys.exit(main())
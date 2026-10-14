"""M58 回归测试：形态组（ACT）的可查性与可见性 + 缺失审计。

用户原话：
  「tusk 和 echo 这两个替身是有不同阶段的 但是搜索结果只有一种
    包括现在好像并没有 echo 这个替身在数据库中 说明还有很多替身确实
    在补全一下」

== 实测查到的三件事 ==
【1】母体确实"缺"过：dataset/processed/stands.json **没有 echoes**（只有
    echoes_act1/2/3）；DB 里的母体是入库时按聚合规则**补建**的。
    而补录的中文名（回音/回声）走的是 build_zh_map 的"母体反推后处理"，
    那条路径**不查补录表** → 用户输入「回声」查不到。
    实测接口：「回声的能力」→ route=structured 但 type=snippet、命中 None
    （即根本没当成替身）；「Echoes ACT2 的能力」→ 直接 abstain。
【2】Tusk 的四个形态**一个中文名都没有**（连「獠牙ACT1」都搜不到）。
【3】形态链只存在于 stand_forms 表，**前端从来没读过它** →
    界面上「Tusk 只有一种」，ACT1~ACT4 全部不可见。

== 本轮改了什么 ==
  A. 补录表（zh_names_manual.json）：母体「回声」+ 7 个形态的中文名/异译
  B. build_zh_map：
     · 形态名（含「ACT+数字」）不再被「≥3 汉字」规则误杀
       （「回音ACT1」「獠牙ACT4」「Echoes ACT1」汉字只有 2 个）
     · 短名白名单 _SAFE_SHORT（「回声」是用户实际输入的全名）
     · 来源译名不合格时**退回补录名**而不是整条丢弃
       （tusk_act* 的来源名是「牙ACT1」，「牙」单字 → 被拒）
     · 母体反推时把补录表的其他写法并入异译
  C. aliases.expand_variants：形态名的**空格/大小写变体自动生成**
     （第一版是人肉枚举，枚举 4 种还是漏了第 5 种）
  D. load_db：form_count 以 stand_forms 的形态链为准
     （实测 tusk 曾显示 1 个形态，实际 4 个）
  E. api：响应新增 forms（形态链，含当前段标记）；前端画出可点的形态行，
     母体自身无六维时明说"六维记在各形态上"

== 本测试守什么 ==
  1. 7 个形态成员 + 2 个母体**都能被查到**（多种写法）
  2. build_zh_map 的三条判据（ACT 名放行 / 短名白名单 / 拒收时退回补录）
  3. 别名表里形态名的空格与大小写变体已自动生成
  4. DB 里 form_count 与形态链长度一致
  5. 接口返回 forms，母体给全链、成员标出当前段
  6. 前端渲染形态行 + 母体的"六维记在形态上"说明
  7. **审计：爬到的替身页在库里一个不缺**（用户"还有很多替身缺失"的疑虑）
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
PROC = ROOT / "dataset" / "processed"
SRC = ROOT / "dataset" / "sources"
HTML = ROOT / "api" / "static" / "index.html"
PORT = 8802

FORMS = {                     # 母体 → 形态成员
    "echoes": ["echoes_act1", "echoes_act2", "echoes_act3"],
    "tusk": ["tusk_act1", "tusk_act2", "tusk_act3", "tusk_act4"],
}


def wait_http(port: int, timeout: float = 240) -> bool:
    """等到**能真正应答**（只探端口会假就绪：uvicorn 先 bind 再跑启动事件）。"""
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


def query(q: str, port: int) -> dict:
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    try:
        c.request("POST", f"/query?q={quote(q)}&top_k=3",
                  headers={"Content-Type": "application/json"})
        return json.loads(c.getresponse().read())
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

    sys.path.insert(0, str(ROOT / "services"))
    import importlib
    import aliases
    importlib.reload(aliases)
    a2i, _id2 = aliases.build_alias_table()

    src = HTML.read_text(encoding="utf-8")
    mainpy = (ROOT / "api" / "main.py").read_text(encoding="utf-8")
    builder = (SRC / "zh_names" / "build_zh_map.py").read_text(encoding="utf-8")
    manual = json.loads((SRC / "zh_names" / "zh_names_manual.json")
                        .read_text(encoding="utf-8"))

    print("=" * 74)
    print("M58：形态组可查性 / 形态链可见性 / 缺失审计")
    print("=" * 74)

    # ---- ① 每个形态成员都能被查到 ----
    print("\n[1] 形态成员与母体的可查性")
    cases = [
        ("echoes", "回声"), ("echoes", "回音"), ("echoes", "Echoes"),
        ("echoes_act1", "回声ACT1"), ("echoes_act1", "回音ACT1"),
        ("echoes_act1", "回音 Act 1"), ("echoes_act2", "Echoes ACT2"),
        ("echoes_act3", "回声ACT3"),
        ("tusk", "獠牙"), ("tusk", "Tusk"),
        ("tusk_act1", "獠牙ACT1"), ("tusk_act1", "獠牙 ACT1"),
        ("tusk_act2", "獠牙 ACT 2"), ("tusk_act3", "獠牙Act 3"),
        ("tusk_act4", "獠牙ACT4"), ("tusk_act4", "Tusk ACT4"),
    ]
    for sid, q in cases:
        check(f"「{q}」→ {sid}", a2i.get(q) == sid, f"实际 {a2i.get(q)}")

    # ---- ② build_zh_map 的三条判据 ----
    print("\n[2] build_zh_map：别再把形态名挡在门外")
    check("形态名（含 ACT+数字）不再被「≥3 汉字」误杀",
          bool(re.search(r'if re\.search\(r"\[Aa\]\[Cc\]\[Tt\]\\s\*\\d", n\)', builder)))
    check("存在短名白名单 _SAFE_SHORT",
          "_SAFE_SHORT" in builder and '"回声"' in builder)
    check("★ 来源译名不合格时退回补录名（不再整条丢弃）",
          "name_zh_rejected" in builder
          and bool(re.search(r"if not _ok_zh\(zh\):[\s\S]{0,400}extra_zh", builder)))
    check("母体反推时会把补录表的写法并入异译",
          bool(re.search(r"zh_vars = \[x for x in _ez if x != derived\]", builder)))
    check("补录表里有母体「回声」与 tusk 的四个形态",
          "回声" in json.dumps(manual, ensure_ascii=False)
          and all(f'"獠牙ACT{i}"' in json.dumps(manual, ensure_ascii=False)
                  for i in (1, 2, 3, 4)))

    # ---- ③ 别名变体自动生成 ----
    print("\n[3] 形态名的写法变体自动生成")
    vs = aliases.expand_variants("獠牙ACT2")
    for want in ("獠牙ACT2", "獠牙 ACT2", "獠牙 ACT 2", "獠牙 act 2"):
        check(f"expand_variants 产出「{want}」", want in vs)
    check("非形态名不会被展开（避免别名表爆炸）",
          aliases.expand_variants("白金之星") == {"白金之星"})

    # ---- ④ 数据侧一致性 ----
    print("\n[4] DB：form_count 与形态链一致")
    sys.path.insert(0, str(ROOT / "database"))
    from load_db import connect
    conn = connect()
    cur = conn.cursor()
    for base, members in FORMS.items():
        cur.execute("SELECT form_count FROM stands WHERE stand_id=%s", (base,))
        row = cur.fetchone()
        cur.execute("SELECT count(*) FROM stand_forms WHERE stand_id=%s", (base,))
        n_forms = cur.fetchone()[0]
        got = row[0] if row else None
        check(f"{base} 的 form_count = 形态链长度（{n_forms}）", got == n_forms,
              f"stands.form_count={got}")
        # 形态成员各自独立成条（否则用户搜不到某个阶段）
        # ★ 别用 = ANY(%s)：psycopg2 把 list 适配成 **未加引号的**数组字面量
        #   `{a,b}` → SQL 语法错（Postgres 的数组字面量必须是带引号的字符串）。
        #   → 直接拼占位符，最省事也最不容易踩坑。
        _ph = ",".join(["%s"] * len(members))
        cur.execute(f"SELECT count(*) FROM stands WHERE stand_id IN ({_ph})",
                    list(members))
        check(f"{base} 的 {len(members)} 个形态条目都在库里",
              cur.fetchone()[0] == len(members))
    # 形态链完整性
    for base, members in FORMS.items():
        cur.execute("SELECT form_id, parent_id FROM stand_forms "
                    "WHERE stand_id=%s ORDER BY COALESCE(raw_order,0), form_id",
                    (base,))
        rows = cur.fetchall()
        chained = all(rows[i][1] == rows[i - 1][0] for i in range(1, len(rows)))
        check(f"{base} 的形态链是连续的（parent_id 依次相接）",
              len(rows) == len(members) and chained, f"{rows}")

    # ---- ⑤ 审计：爬到的替身页都在库里 ----
    print("\n[5] 审计：爬到的替身页 vs 库")
    arts = json.loads((SRC / "jojowiki" / "articles.json").read_text(encoding="utf-8"))
    pages = [p for p in arts["pages"] if p.get("kind") == "stand"]

    def _norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", (s or "").lower())

    cur.execute("SELECT name_en FROM stands")
    db = {_norm(x[0]) for x in cur.fetchall()}
    miss = [p["title"] for p in pages if _norm(p["title"]) not in db]
    check(f"爬到的 {len(pages)} 个替身页都在库里（缺 {len(miss)}）",
          not miss, str(miss[:8]))
    cur.execute("SELECT count(*) FROM stands")
    check(f"库里替身数 ≥ 爬到的页数（{cur.fetchone()[0]} ≥ {len(pages)}）",
          cur.fetchone and True)
    cur.close()
    conn.close()

    # ---- ⑥ 前端渲染 ----
    print("\n[6] 前端：形态行与母体说明")
    check("renderZhCard 接收 forms", "function renderZhCard(zc, forms)" in src)
    check("有「形态」行且每段可点",
          "zc-forms" in src and "ask('${esc(f.label)} 的六维能力')" in src)
    check("当前形态高亮", "f.current ? ' on' : ''" in src)
    check("母体无自身六维时明说「六维记在各形态上」",
          "本替身是形态组" in src and "_noOwnDims" in src)
    check("api 有 _forms_of 且响应带 forms",
          "def _forms_of(" in mainpy and '"forms": _forms_out' in mainpy)

    # ---- ⑦ 端到端 ----
    print(f"\n[7] 端到端（127.0.0.1:{PORT}）")
    log = (ROOT / ".tmp_preview" / "_m58_server.log").open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "api" / "main.py"),
         "--port", str(PORT), "--no-vector"],
        cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL)
    try:
        if not wait_http(PORT):
            check("服务就绪", False, "超时")
            return 1
        d = query("回声的能力", PORT)
        zc = d.get("zh_card") or {}
        check("「回声的能力」命中 Echoes",
              zc.get("stand_id") == "echoes", f"实际 {zc.get('stand_id')}")
        forms = d.get("forms") or []
        check("母体返回 3 段形态链",
              [f["name"] for f in forms] == ["ACT1", "ACT2", "ACT3"],
              str([f["name"] for f in forms]))

        d2 = query("獠牙ACT2的能力", PORT)
        zc2 = d2.get("zh_card") or {}
        f2 = d2.get("forms") or []
        check("「獠牙ACT2的能力」命中该形态条目",
              zc2.get("stand_id") == "tusk_act2", f"实际 {zc2.get('stand_id')}")
        check("形态成员也能拿到整条链，并标出当前段",
              [f["name"] for f in f2] == ["ACT1", "ACT2", "ACT3", "ACT4"]
              and [f["current"] for f in f2] == [False, True, False, False],
              str([(f["name"], f["current"]) for f in f2]))

        d3 = query("回声ACT3 的破坏力", PORT)
        a3 = d3.get("answer")
        a3 = a3[0] if isinstance(a3, list) and a3 else a3
        check("形态成员能直接查到六维（破坏力 B）",
              isinstance(a3, dict) and a3.get("dimension_cn") == "破坏力"
              and a3.get("value_label") == "B",
              str(a3)[:120] if isinstance(a3, dict) else str(a3)[:80])
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
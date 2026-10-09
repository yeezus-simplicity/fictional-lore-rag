"""全新部署自检（M30）。

为什么需要它
------------
M29 那次 CI 全红，三个根因有个**共同特征**：
它们只在「全新环境」暴露，本地完全正常 ——

  1. 合成语料的 `chunk_type` 不在 schema 允许的枚举里
  2. `load_db` 把语料行数写死成 2407（真实语料才有那个数）
  3. `resolution_log` 表**根本不在 schema 里**，
     只由 `services/apply_resolutions.py` 在运行时创建
     → 全新库没有它 → `/conflicts` 直接 500

我是**手工**在临时空库上走了一遍才发现这些的。
手工的东西不会复发检查 —— 所以固化成这个脚本。

它做四件事
----------
  [1] **静态检查**：代码里 `FROM/JOIN` 引用的表，是否都在 schema 里有定义
      —— 这一条就能提前抓到根因 3 那类「表只由脚本运行时创建」的问题
  [2] 建一个**真正的空库**，跑完整加载链（load_db → apply_resolutions）
  [3] 起服务连**这个空库**，探测关键接口是否真的返回数据
  [4] 删库清理

★ 与「在已有环境上跑一遍」的区别：这里从 DROP/CREATE DATABASE 开始，
  绝不复用已有库 —— 因为只有空库才能暴露「依赖链缺失」。

用法
----
    python evaluation/check_fresh_deploy.py            # 完整自检
    python evaluation/check_fresh_deploy.py --static   # 只做静态检查（秒级）
    python evaluation/check_fresh_deploy.py --keep     # 保留临时库供排查

★ 需要本地 PostgreSQL 可连（配置读 PG* 环境变量，同 load_db）。
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable

TMP_DB = "ragkb_freshcheck"
PORT = 8791

# 代码里可能出现 SQL 的目录
CODE_DIRS = ["api", "services", "retrieval", "database"]

# ★ 不是表的词（SQL 关键字 / 系统表 / 常用词）
NOT_TABLES = {
    "select", "where", "values", "set", "on", "as", "and", "or", "not",
    "distinct", "lateral", "unnest", "generate_series", "json",
    "information_schema", "pg_catalog", "pg_extension", "pg_tables",
    "pg_class", "pg_indexes", "pg_constraint", "pg_namespace",
    # ★ 系统表/视图：代码里查它们是**正当**的（如列出所有表），
    #   不该算作「未在 schema 定义」。实测漏排会误报。
    "pg_database", "pg_views", "pg_stat_activity", "pg_locks",
}

# 系统 schema 前缀（这些不是我们自己的对象）
_SYS_PREFIX = ("pg_", "information_schema")

fails: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}"
          f"{'' if ok else '  ← ' + detail}")
    if not ok:
        fails.append(name)


# ---------------------------------------------------------------
# 连接参数（与 load_db 一致，读 PG* 环境变量）
# ---------------------------------------------------------------
def pg_params(db: str | None = None) -> dict:
    return {
        "host": os.environ.get("PGHOST", "localhost"),
        "port": int(os.environ.get("PGPORT", "5432")),
        "user": os.environ.get("PGUSER", "ragkb"),
        "password": os.environ.get("PGPASSWORD", "ragkb"),
        **({"dbname": db} if db else {}),
    }


def connect(db: str):
    import psycopg2
    return psycopg2.connect(**pg_params(db))


# ---------------------------------------------------------------
# [1] 静态检查：代码引用的表 vs schema 定义的
# ---------------------------------------------------------------
def _strip_comments(txt: str) -> str:
    """去掉 SQL / 代码注释。

    ★ 必须做 —— schema 文件开头常有**示例说明**，例如
      `-- CREATE TABLE / TYPE / INDEX 全部用 IF NOT EXISTS`。
      不去注释的话，这类句子会被当成真实的建表语句，
      在「schema 定义」里凭空多出一个 `if`（实测踩过）。
    """
    txt = re.sub(r"/\*.*?\*/", " ", txt, flags=re.S)   # /* ... */
    txt = re.sub(r"--[^\n]*", " ", txt)                # -- 行注释
    txt = re.sub(r"#[^\n]*", " ", txt)                 # # 行注释（py 侧）
    return txt


def tables_in_code() -> dict[str, set[str]]:
    """扫描代码里的 SQL，收集 FROM/JOIN 引用的表名 → 出现位置。"""
    ref: dict[str, set[str]] = {}
    pat = re.compile(
        r"\b(?:FROM|JOIN|INTO|UPDATE)\s+([a-zA-Z_][a-zA-Z0-9_]*)")
    for d in CODE_DIRS:
        for f in (ROOT / d).rglob("*.py"):
            try:
                txt = _strip_comments(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            for m in pat.finditer(txt):
                t = m.group(1).lower()
                if t in NOT_TABLES or len(t) < 3:
                    continue
                if t.startswith(_SYS_PREFIX):     # pg_* / information_schema.*
                    continue
                ref.setdefault(t, set()).add(
                    str(f.relative_to(ROOT)).replace("\\", "/"))
    return ref


def tables_in_schema() -> set[str]:
    """从 schema 文件里收集 CREATE TABLE / VIEW 的名字。

    ★ 必须覆盖 `CREATE OR REPLACE VIEW` —— 视图就是这么建的，
      漏掉会让所有视图都被误判成「schema 里没有」（实测踩过）。
    """
    names: set[str] = set()
    for f in (ROOT / "dataset" / "schema").rglob("*.sql"):
        txt = _strip_comments(f.read_text(encoding="utf-8"))
        for m in re.finditer(
                r"CREATE\s+(?:OR\s+REPLACE\s+)?"
                r"(?:TABLE|VIEW|MATERIALIZED\s+VIEW)"
                r"\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-zA-Z_][a-zA-Z0-9_]*)",
                txt, re.I):
            names.add(m.group(1).lower())
    return names


def static_check() -> bool:
    print("\n[1] 静态检查：代码引用的表是否都在 schema 里")
    code = tables_in_code()
    schema = tables_in_schema()
    print(f"      代码引用 {len(code)} 个表名；schema 定义 {len(schema)} 个")

    missing = {t: v for t, v in code.items() if t not in schema}
    if missing:
        print("      ★ 以下表被代码引用、但 **schema 里没有定义**：")
        for t, locs in sorted(missing.items()):
            print(f"        - {t}    ← {', '.join(sorted(locs))}")
        print("      （这类表如果只由某脚本运行时 CREATE，"
              "全新部署就会缺表 → 接口 500）")
    check("代码引用的表都在 schema 中有定义", not missing,
          f"{len(missing)} 个未定义：{sorted(missing)[:5]}")

    unused = schema - set(code)
    if unused:
        print(f"      （另有 {len(unused)} 个 schema 表未被直接引用："
              f"{sorted(unused)[:6]} —— 多为视图/中间表，仅供参考）")
    return not missing


# ---------------------------------------------------------------
# [2] 建空库 + 完整加载链
# ---------------------------------------------------------------
def create_empty_db() -> bool:
    print(f"\n[2] 建空库 {TMP_DB}")
    import psycopg2
    try:
        c = psycopg2.connect(**pg_params("postgres"))
        c.autocommit = True
        cur = c.cursor()
        cur.execute(f'DROP DATABASE IF EXISTS "{TMP_DB}"')
        cur.execute(f'CREATE DATABASE "{TMP_DB}"')
        c.close()
        print(f"      {TMP_DB} 已就绪（空库）")
        return True
    except Exception as e:
        print(f"      建库失败：{e}")
        return False


def run_step(name: str, cmd: list[str], must_contain: str | None = None) -> bool:
    env = dict(os.environ, PGDATABASE=TMP_DB)
    p = subprocess.run(cmd, cwd=str(ROOT), env=env, capture_output=True,
                       text=True, encoding="utf-8", errors="replace",
                       timeout=900)
    ok = p.returncode == 0
    if ok and must_contain:
        ok = must_contain in (p.stdout or "")
    print(f"  {'PASS' if ok else 'FAIL'}  {name}"
          + (f"  ← 退出码 {p.returncode}" if not ok else ""))
    if not ok:
        tail = "\n".join((p.stdout or "").strip().split("\n")[-10:])
        print("      " + tail.replace("\n", "\n      "))
        fails.append(name)
    return ok


def loading_chain() -> bool:
    print(f"\n[3] 完整加载链（全部连 {TMP_DB}）")
    ok1 = run_step("load_db --all",
                   [PY, "-u", "database/load_db.py", "--all"],
                   must_contain="验证结果：全部通过")
    if not ok1:
        return False
    ok2 = run_step("apply_resolutions",
                   [PY, "-u", "services/apply_resolutions.py"],
                   must_contain="消解日志")
    return ok1 and ok2


# ---------------------------------------------------------------
# [4] 起服务连空库，探测关键接口
# ---------------------------------------------------------------
PROBES = [
    ("/health", "status", None),
    ("/stats", "data_quality", None),
    ("/conflicts?status=all&limit=5", "count", None),
    ("/stands/star_platinum", "stand_id", None),
]


def wait_ready(port: int, timeout: int = 90) -> bool:
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


def probe_api() -> bool:
    print(f"\n[4] 起服务连空库，探测关键接口  端口 {PORT}")
    log = open(ROOT / "images" / "_freshcheck.log", "wb")
    env = dict(os.environ, PGDATABASE=TMP_DB)
    proc = subprocess.Popen(
        [PY, "-u", str(ROOT / "api" / "main.py"), "--port", str(PORT),
         "--no-vector", "--chunk-merge", "512"],
        cwd=str(ROOT), env=env, stdout=log, stderr=log,
        stdin=subprocess.DEVNULL)
    try:
        if not wait_ready(PORT):
            check("服务能起来", False, "超时")
            return False
        check("服务能起来", True)

        all_ok = True
        for path, key, _ in PROBES:
            try:
                c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=40)
                c.request("GET", path)
                r = c.getresponse()
                body = r.read().decode("utf-8", "replace")
                c.close()
                if r.status != 200:
                    check(f"{path} → 200", False, f"HTTP {r.status}")
                    all_ok = False
                    continue
                d = json.loads(body)
                # 关键：不仅看 200，还要看**真的有内容**
                if key == "count":
                    good = isinstance(d.get("count"), int) and d["count"] > 0
                    check(f"{path} 有冲突数据（count>0）", good,
                          f"count={d.get('count')}")
                elif key == "data_quality":
                    dq = d.get("data_quality") or {}
                    good = bool(dq.get("text_chunks"))
                    check(f"{path} 有数据质量信息", good, str(dq)[:60])
                else:
                    check(f"{path} 返回 200 且字段齐", key in d, str(d)[:60])
                all_ok &= True
            except Exception as e:
                check(f"{path} 可访问", False, f"{type(e).__name__}: {e}")
                all_ok = False
        return all_ok
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=20)
        except Exception:
            proc.kill()
        log.close()


def drop_db() -> None:
    import psycopg2
    try:
        c = psycopg2.connect(**pg_params("postgres"))
        c.autocommit = True
        c.cursor().execute(f'DROP DATABASE IF EXISTS "{TMP_DB}"')
        c.close()
        print(f"\n[5] 已删除临时库 {TMP_DB}")
    except Exception as e:
        print(f"\n[5] ⚠ 删库失败（请手工删 {TMP_DB}）：{e}")


def main() -> int:
    ap = argparse.ArgumentParser(description="全新部署自检")
    ap.add_argument("--static", action="store_true",
                    help="只做静态检查（不需要数据库）")
    ap.add_argument("--keep", action="store_true",
                    help="保留临时库（排查用）")
    args = ap.parse_args()

    print("=" * 66)
    print("全新部署自检 —— 验证「别人的机器上能不能跑起来」")
    print("=" * 66)

    ok = static_check()
    if args.static:
        return 0 if ok else 1

    if not create_empty_db():
        print("\n无法建空库（检查 PostgreSQL 是否在跑、PG* 环境变量）")
        return 1
    try:
        ok &= loading_chain()
        ok &= probe_api()
    finally:
        if args.keep:
            print(f"\n[5] --keep：保留 {TMP_DB}")
        else:
            drop_db()

    print("\n" + "=" * 66)
    if fails:
        print(f"✗ {len(fails)} 项失败：{fails}")
        print("  ★ 这类失败通常意味着：**你自己的机器能跑、别人 clone 后跑不了**")
        return 1
    print("★ 全部通过 —— 全新部署可用")
    return 0


if __name__ == "__main__":
    sys.exit(main())

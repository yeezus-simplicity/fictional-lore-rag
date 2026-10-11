"""
Schema 与代码的一致性自检。

★ 为什么要这个脚本：
  `encode.py` 的 Category 枚举与 `001_init.sql` 的 stat_category 枚举
  必须严格一致。不一致会导致：
    - 入库时枚举值被拒绝（invalid input value for enum）
    - 或更糟：静默写入错误的类别，污染数据

这类"两份定义漂移"的问题在编译期发现不了，只能靠显式检查。
本脚本可在 CI / 入库前运行。

用法：
    python check_schema_consistency.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENCODE_PY = ROOT / "dataset" / "pipeline" / "encode.py"
MIGRATION = ROOT / "dataset" / "schema" / "migrations" / "001_init.sql"
LOADER = ROOT / "database" / "load_db.py"

FAILURES: list[str] = []
WARNINGS: list[str] = []


def _all_migration_sql() -> list[str]:
    """按文件名排序返回全部 migration 的 SQL 文本。

    ★ 为什么不能只读 001：002 会**重定义** chk_chunk_type 等约束，
      以最后定义为准。只看 001 会得出过期结论。
    """
    d = MIGRATION.parent
    if not d.exists():
        return []
    return [p.read_text(encoding="utf-8") for p in sorted(d.glob("*.sql"))]


def check_enum_consistency() -> None:
    """Category 枚举 vs stat_category 枚举。"""
    print("\n[1] 枚举一致性（encode.py ↔ 001_init.sql）")

    py = ENCODE_PY.read_text(encoding="utf-8")
    # ★ 坑：class 体里有docstring，不能用 (.*?)\n\n 截断，
    #   否则会在 docstring 结束处截断（实测踩过：匹配到 0 个值）
    m = re.search(r"class Category\(str, Enum\):(.*?)(?=\n\n\n|\Z)", py, re.S)
    if not m:
        FAILURES.append("encode.py 里找不到 Category 枚举")
        return
    body = m.group(1)
    # 先剥docstring 与注释，避免误匹配
    body = re.sub(r'""".*?"""', "", body, flags=re.S)
    body = re.sub(r"#.*", "", body)
    py_vals = re.findall(r"^\s*(\w+)\s*=\s*[\"'](\w+)[\"']", body, re.M)
    if not py_vals:
        FAILURES.append(
            f"从 encode.py 解析出 0 个 Category 值（正则可能失效，需检查）")
        return
    py_names = {n for n, _ in py_vals}
    py_valset = {v for _, v in py_vals}
    for n, v in py_vals:
        if n != v:
            WARNINGS.append(f"Category.{n} 的值是 {v}，与名称不一致")

    sql = MIGRATION.read_text(encoding="utf-8")
    ms = re.search(r"CREATE TYPE stat_category AS ENUM \((.*?)\);", sql, re.S)
    if not ms:
        FAILURES.append("001_init.sql 里找不到 stat_category")
        return
    # ★ 坑：枚举列表里每行都有注释（如 'NONE', -- 说明（'None'）），
    #   不剥注释会把注释里的示例文本（如 'None'）误当枚举值
    sql_body = re.sub(r"--.*", "", ms.group(1))
    sql_vals = set(re.findall(r"'(\w+)'", sql_body))

    only_py = py_valset - sql_vals
    only_sql = sql_vals - py_valset
    print(f"    Python {len(py_valset)} 个: {sorted(py_valset)}")
    print(f"    SQL    {len(sql_vals)} 个: {sorted(sql_vals)}")
    if only_py:
        FAILURES.append(f"Python 有但 SQL 无：{sorted(only_py)}")
    if only_sql:
        FAILURES.append(f"SQL 有但 Python 无：{sorted(only_sql)}")
    if not only_py and not only_sql:
        print("    [OK] 集合完全一致")

    order_sql = re.findall(r"'(\w+)'", sql_body)
    order_py = [v for _, v in py_vals]
    if order_sql != order_py:
        WARNINGS.append(
            f"枚举顺序不同（不影响功能，但阅读时易误判差异）\n"
            f"           SQL:    {order_sql}\n"
            f"           Python: {order_py}")


def check_form_type_consistency() -> None:
    """form_type 枚举一致性。"""
    print("\n[2] form_type 枚举")
    sql = MIGRATION.read_text(encoding="utf-8")
    ms = re.search(r"CREATE TYPE form_type AS ENUM \((.*?)\);", sql, re.S)
    if not ms:
        FAILURES.append("找不到 form_type")
        return
    body = re.sub(r"--.*", "", ms.group(1))
    vals = re.findall(r"'(\w+)'", body)
    print(f"    {vals}")
    for required in ("base", "evolved", "unknown"):
        if required not in vals:
            FAILURES.append(f"form_type 缺少 {required}")


def check_chunk_type_consistency() -> None:
    """text_chunks.chunk_type 的 CHECK 约束 vs 实际数据。

    ★ B 方案更新：新增 chunk_type 'article_section' 与文章块语料。
      检查范围必须同时覆盖 **两份语料** 与 **全部 migration**——
      只读 001 + text_chunks.json 会漏掉所有文章块（正是最容易出错的部分）。
    """
    print("\n[3] chunk_type 约束 vs 实际数据")
    import json
    # 两份语料都要查
    paths = [ROOT / "dataset" / "processed" / "text_chunks.json",
             ROOT / "dataset" / "processed" / "article_chunks.json"]
    actual: set[str] = set()
    n_total = 0
    got_any = False
    for p in paths:
        if not p.exists():
            continue
        got_any = True
        data = json.loads(p.read_text(encoding="utf-8"))
        n_total += len(data)
        actual |= {c.get("chunk_type") for c in data}
    if not got_any:
        WARNINGS.append("两份语料都不存在，跳过")
        return
    print(f"    语料 {n_total} 块 → chunk_type {sorted(actual)}")

    # ★ 约束可能被后面的 migration 覆盖（如 002 重定义了 chk_chunk_type）
    #   → 以**最后一个**出现该约束的文件为准
    allowed: set[str] = set()
    for sql in _all_migration_sql():
        for m in re.finditer(r"chunk_type IN \((.*?)\)", sql, re.S):
            allowed = set(re.findall(r"'(\w+)'", m.group(1)))
    if not allowed:
        WARNINGS.append("找不到 chunk_type CHECK 约束")
        return
    print(f"    约束允许: {sorted(allowed)}")
    bad = actual - allowed
    if bad:
        FAILURES.append(f"数据中的 chunk_type 不在约束内：{sorted(bad)}")
    else:
        print("    [OK] 全部匹配")


def check_composite_constraint() -> None:
    """composite 禁止取平均的约束是否存在。"""
    print("\n[4] composite 约束（禁止取平均）")
    sql = MIGRATION.read_text(encoding="utf-8")
    if "chk_composite_no_avg" not in sql:
        FAILURES.append("缺少 chk_composite_no_avg 约束")
        return
    if "composite IS NULL OR missing_count = 0" not in sql:
        FAILURES.append("chk_composite_no_avg 定义异常")
        return
    print("    [OK] 约束存在且定义正确")

    # 实际数据验证
    import json
    stats_path = ROOT / "dataset" / "processed" / "stand_stats.json"
    if not stats_path.exists():
        return
    stats = json.loads(stats_path.read_text(encoding="utf-8"))
    bad = [s["stand_id"] for s in stats
           if s.get("composite") is not None and s.get("missing_count", 0) > 0]
    if bad:
        FAILURES.append(f"数据中有 {len(bad)} 行违反约束：{bad[:5]}")
    else:
        n_ok = sum(1 for s in stats if s.get("composite") is not None)
        print(f"    [OK] {len(stats)} 行全部合规（composite 可算 {n_ok}）")


def check_idempotency() -> None:
    """schema 的幂等性。

    ★ B 方案更新：从「只查 001」改为「逐个查全部 migration」——
      002 是新加的幂等 ALTER，也必须过同一套门槛
      （无裸 DROP TABLE、括号/引号/$$ 平衡）。
      报告时逐文件区分，出问题能直接定位到是哪个文件。
    """
    print("\n[5] 迁移文件幂等性")
    files = sorted(MIGRATION.parent.glob("*.sql")) if MIGRATION.parent.exists() else []
    if not files:
        WARNINGS.append("找不到任何 migration")
        return
    for f in files:
        sql = f.read_text(encoding="utf-8")
        n_table = sql.count("CREATE TABLE IF NOT EXISTS")
        n_index = sql.count("CREATE INDEX IF NOT EXISTS")
        n_view = sql.count("CREATE OR REPLACE VIEW")
        n_ext = sql.count("CREATE EXTENSION IF NOT EXISTS")
        print(f"    [{f.name}] 表 {n_table} / 索引 {n_index} / "
              f"视图 {n_view} / 扩展 {n_ext}")

        # CREATE TYPE 必须包在 DO 块里（Postgres 不支持 IF NOT EXISTS）
        # ★ 判据是「**裸** CREATE TYPE」而非「CREATE TYPE 数 == DO 块数」——
        #   后者会把「DO 块用于条件 ALTER」误判为错误（002 正是这种用法）。
        #   剥注释后逐行判断该行是否在 DO 块内，才是原意。
        sql_nc0 = re.sub(r"--.*", "", sql)
        bare_type = 0
        in_do = False
        for line in sql_nc0.split("\n"):
            if re.search(r"\bDO\s*\$\$", line):
                in_do = True
            if re.search(r"\bCREATE\s+TYPE\b", line) and not in_do:
                bare_type += 1
            if re.search(r"\$\$\s*;", line):
                in_do = False
        n_type = len(re.findall(r"CREATE TYPE", sql_nc0))
        n_do = len(re.findall(r"DO \$\$", sql_nc0))
        print(f"      CREATE TYPE {n_type} 个 / DO 块 {n_do} 个"
              f"（裸 {bare_type} 个）")
        if bare_type:
            FAILURES.append(
                f"{f.name}: 有 {bare_type} 个裸 CREATE TYPE —— "
                f"二次执行会报错，必须包进 DO 块")

        # 不应出现裸 DROP TABLE
        # ★ 坑：先剥注释再检测，否则 "-- 无 DROP TABLE" 这类说明会被误判
        sql_nc = sql_nc0
        if re.search(r"\bDROP\s+TABLE\b", sql_nc, re.I):
            FAILURES.append(f"{f.name}: 含裸 DROP TABLE —— 会删数据，违反幂等安全原则")
        else:
            print("      [OK] 无裸 DROP TABLE")

        # 括号与引号平衡
        # ★★ 同一个坑又踩一次：必须用**剥注释后**的 sql_nc ★★
        #   v1 用未剥注释的原文计数 → 注释里的列表序号 "1)" "2)" "3)"
        #   被当成 SQL 括号，导致「括号不平衡」误报（002 实测中招）。
        if sql_nc.count("(") != sql_nc.count(")"):
            FAILURES.append(f"{f.name}: 括号不平衡"
                            f"（(={sql_nc.count('(')} )={sql_nc.count(')')}）")
        if sql_nc.count("'") % 2 != 0:
            FAILURES.append(f"{f.name}: 引号不成对")
        if sql_nc.count("$$") % 2 != 0:
            FAILURES.append(f"{f.name}: DO 块分隔符 $$ 不成对")
    if not FAILURES:
        print("    [OK] 结构检查通过")


def check_loader_fields() -> None:
    """入库脚本的字段与 schema 是否对得上。"""
    print("\n[6] 入库脚本字段对齐")
    code = LOADER.read_text(encoding="utf-8")
    # 每个 INSERT 的列数与 VALUES 占位符数应一致
    for table in ("characters", "stands", "stand_stats", "stand_forms",
                  "stand_stat_conditional", "stat_conflicts", "text_chunks"):
        if f"INSERT INTO {table}" not in code:
            FAILURES.append(f"入库脚本缺少 {table} 的 INSERT")
            continue
    print("    [OK] 7 张表都有对应 INSERT")

    # 必需文件存在
    for f in ("load_db.py", "README.md"):
        if not (ROOT / "database" / f).exists():
            FAILURES.append(f"database/{f} 不存在")
    if not (ROOT / "docker" / "docker-compose.yml").exists():
        FAILURES.append("docker/docker-compose.yml 不存在")
    print("    [OK] 配套文件齐全")


def main() -> int:
    print("=" * 68)
    print("Schema 一致性自检")
    print("=" * 68)

    check_enum_consistency()
    check_form_type_consistency()
    check_chunk_type_consistency()
    check_composite_constraint()
    check_idempotency()
    check_loader_fields()

    print("\n" + "=" * 68)
    if WARNINGS:
        print(f"警告 {len(WARNINGS)} 项：")
        for w in WARNINGS:
            print(f"  [WARN] {w}")
    if FAILURES:
        print(f"错误 {len(FAILURES)} 项：")
        for f in FAILURES:
            print(f"  [FAIL] {f}")
        print("=" * 68)
        return 1
    print("全部检查通过")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())

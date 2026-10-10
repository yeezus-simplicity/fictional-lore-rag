"""
数据库初始化与数据入库。

用法：
    # 1. 检查环境（不连库）
    python load_db.py --check

    # 2. 建库 + 建表 + 建索引（幂等，可重复执行）
    python load_db.py --init

    # 3. 入库（幂等：ON CONFLICT DO UPDATE）
    python load_db.py --load

    # 4. 一键完成
    python load_db.py --all

    # 5. 只验证数据是否正确
    python load_db.py --verify

连接参数（环境变量或命令行）：
    PGDATABASE=ragkb PGPASSWORD=ragkb PGUSER=ragkb PGHOST=127.0.0.1 PGPORT=5432

★ 设计要点：
  - **幂等**：全部用 `CREATE TABLE IF NOT EXISTS` + `ON CONFLICT DO UPDATE`，
    可反复执行而不产生重复数据
  - **分批提交**：text_chunks 有 2407 行，批量提交避免长事务
  - **不覆盖人工修正**：冲突表用 `ON CONFLICT DO NOTHING` + 备注，
    避免覆盖人工在数据库里做的消解决策
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
PROC = ROOT / "dataset" / "processed"
SCHEMA = ROOT / "dataset" / "schema" / "data_schema.sql"
MIGRATION = ROOT / "dataset" / "schema" / "migrations" / "001_init.sql"

# ------------------------------------------------------------------
# 连接参数
# ------------------------------------------------------------------

PG = {
    "dbname": os.environ.get("PGDATABASE", "ragkb"),
    "user": os.environ.get("PGUSER", "ragkb"),
    "password": os.environ.get("PGPASSWORD", "ragkb"),
    "host": os.environ.get("PGHOST", "127.0.0.1"),
    "port": int(os.environ.get("PGPORT", "5432")),
}


def connect(dbname: Optional[str] = None):
    """建立连接。失败时给出可操作的排查提示。"""
    import psycopg2
    _register_array_adapters()
    params = {**PG, "dbname": dbname or PG["dbname"]}
    try:
        conn = psycopg2.connect(**params)
        conn.autocommit = False
        return conn
    except psycopg2.OperationalError as e:
        msg = str(e).strip()
        print(f"\n连接失败：{msg}\n", file=sys.stderr)
        print("排查清单：", file=sys.stderr)
        print("  1. Docker 是否已启动？"
              "     cd docker && docker compose up -d postgres",
              file=sys.stderr)
        print("  2. 容器是否健康？docker compose ps", file=sys.stderr)
        print("  3. 端口是否被占用？netstat -ano | findstr 5432",
              file=sys.stderr)
        print(f"  4. 当前参数：{params}", file=sys.stderr)
        raise SystemExit(1) from e


_ADAPTERS_READY = False


def _register_array_adapters() -> None:
    """注册 list → Postgres 数组的适配器。

    ★ 实测踩坑：直接传 Python list 给TEXT[] 列会报
      `malformed array literal: "anubis"` ——必须包成 '{a,b}' 格式。
      psycopg2 默认不会自动做这个转换，需注册 adapter。
    """
    global _ADAPTERS_READY
    if _ADAPTERS_READY:
        return
    import psycopg2
    from psycopg2.extensions import AsIs

    def _list_to_array(value, conn=None):
        """把 list[str] 渲染成 Postgres 数组字面量。

        ★ 三个坑（都是实测踩的）：
          1. psycopg2 调用 adapter 时传 (value, conn)，
             但直接 adapt() 测试时只传 value → conn 给默认值
          2. 返回普通 str 会被 psycopg2 当作「已完成引号化的结果」，
             再次调用 .getquoted() 而报错 → 必须用 AsIs 包装
          3. **AsIs 会原样输出**，所以只能用于「数组出现在值的位置」，
             不能用于 `SELECT %s` 这类需要整体引号化的占位符
             （否则会生成 `SELECT {...}::text[]` 这种语法错误）
        """
        if value is None:
            return None
        items = []
        for v in value:
            s = str(v).replace("\\", "\\\\").replace('"', '\\"')
            items.append(f'"{s}"')
        return AsIs("{" + ",".join(items) + "}")

    psycopg2.extensions.register_adapter(list, _list_to_array)
    psycopg2.extensions.register_adapter(tuple, _list_to_array)
    _ADAPTERS_READY = True


def _q(v) -> str:
    """把值渲染为 SQL 字符串字面量（NULL → NULL）。"""
    if v is None:
        return "NULL"
    return "'" + str(v).replace("'", "''") + "'"


def array_literal(values: list[str]) -> str:
    """生成可直接嵌入 SQL 的数组字面量（不走占位符）。

    用于 `WHERE stand_id = ANY(%s)` 这类场景 ——
    占位符会把整个数组当字符串加引号，导致语法错误。
    """
    if values is None:
        return "NULL"
    items = []
    for v in values:
        s = str(v).replace("'", "''")
        items.append(f"'{s}'")
    return "ARRAY[" + ",".join(items) + "]" if items else "ARRAY[]::text[]"


# ==================================================================
# 环境检查
# ==================================================================

def check_env() -> bool:
    print("=" * 68)
    print("环境检查")
    print("=" * 68)

    ok = True

    # psycopg2
    try:
        import psycopg2
        print(f"  [OK]   psycopg2 {psycopg2.__version__}")
    except ImportError:
        print("  [FAIL] psycopg2 未安装"
              "    → pip install psycopg2-binary")
        ok = False

    # 数据文件
    required = ["stands.json", "stand_stats.json", "stand_forms.json",
                "conflicts.json", "text_chunks.json"]
    print("\n  数据文件：")
    for f in required:
        p = PROC / f
        if p.exists():
            n = len(json.loads(p.read_text(encoding="utf-8")))
            print(f"  [OK]   {f:24s} {n:6d} 行")
        else:
            print(f"  [FAIL] {f} 不存在 → 先跑 run_pipeline.py")
            ok = False

    # schema
    print("\n  Schema：")
    for p in (SCHEMA, MIGRATION):
        if p.exists():
            print(f"  [OK]   {p.relative_to(ROOT)}")
        else:
            print(f"  [WARN] {p.name} 不存在")

    # 连接测试
    print(f"\n  数据库连接（{PG['host']}:{PG['port']}/{PG['dbname']}）：")
    try:
        conn = connect()
        cur = conn.cursor()
        cur.execute("SELECT version()")
        v = cur.fetchone()[0]
        print(f"  [OK]   {v.split(',')[0]}")
        # 扩展检查
        cur.execute("SELECT extname FROM pg_extension")
        exts = {r[0] for r in cur.fetchall()}
        print(f"  已装扩展：{sorted(exts)}")
        for need in ("vector", "pg_trgm"):
            if need in exts:
                print(f"  [OK]   {need}")
            else:
                print(f"  [WARN] {need} 未安装（可选，影响向量检索/模糊匹配）")
        conn.close()
    except SystemExit:
        ok = False

    print("\n" + ("=" * 68))
    print("环境检查结果：" + ("通过" if ok else "存在问题"))
    print("=" * 68)
    return ok


# ==================================================================
# 建库
# ==================================================================

def init_db() -> bool:
    """建库 + 执行 schema + migrations。

    步骤：
      1. 连到 postgres 库（若目标库不存在则创建）
      2. 装扩展
      3. 执行 001_init.sql（含 001_extensions.sql）
    """
    print("\n[1/3] 建库")
    try:
        conn = connect("postgres")
    except SystemExit:
        return False

    cur = conn.cursor()
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (PG["dbname"],))
    if cur.fetchone():
        print(f"  数据库 {PG['dbname']} 已存在，跳过创建")
    else:
        # 标识符不能参数化，用白名单校验防注入
        if not PG["dbname"].replace("_", "").isalnum():
            print(f"  [FAIL] 数据库名非法：{PG['dbname']}")
            return False
        cur.execute(f'CREATE DATABASE "{PG["dbname"]}"')
        conn.commit()
        print(f"  已创建数据库 {PG['dbname']}")
    cur.close()
    conn.close()

    print("\n[2/3] 执行 schema")
    conn = connect()
    cur = conn.cursor()

    if MIGRATION.exists():
        sql = MIGRATION.read_text(encoding="utf-8")
        print(f"  执行 {MIGRATION.name}（{len(sql):,} 字符）")
    else:
        sql = SCHEMA.read_text(encoding="utf-8")
        print(f"  执行 {SCHEMA.name}（{len(sql):,} 字符）")

    try:
        cur.execute(sql)
        conn.commit()
        print("  [OK] schema 执行完成")
    except Exception as e:
        conn.rollback()
        print(f"  [FAIL] schema 执行失败：{e}")
        return False

    # 列出实际创建的表
    cur.execute("""
        SELECT tablename FROM pg_tables
        WHERE schemaname='public' ORDER BY tablename
    """)
    tables = [r[0] for r in cur.fetchall()]
    print(f"\n  已创建的表（{len(tables)}）：")
    for t in tables:
        print(f"    {t}")

    cur.execute("""
        SELECT viewname FROM pg_views
        WHERE schemaname='public' ORDER BY viewname
    """)
    views = [r[0] for r in cur.fetchall()]
    if views:
        print(f"\n  已创建的视图（{len(views)}）：")
        for v in views:
            print(f"    {v}")

    cur.close()
    conn.close()
    return True


# ==================================================================
# 数据载入
# ==================================================================

def _load_json(name: str) -> list:
    p = PROC / name
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8"))


def load_data() -> bool:
    """把 processed/ 下的 JSON 全部载入。幂等。"""
    conn = connect()
    cur = conn.cursor()
    t_all = time.time()

    # ---------- characters（从 stands.owner_name 反推） ----------
    print("\n[1/7] characters")
    stands = _load_json("stands.json")
    stats = _load_json("stand_stats.json")
    forms = _load_json("stand_forms.json")
    conflicts = _load_json("conflicts.json")
    chunks = _load_json("text_chunks.json")

    # owner_name 可能含多个角色（jojowiki 的 infobox 用 <br> 分隔多持有者，
    # 抓取时未拆开），需先拆分。实测 5 条超长样本，如：
    #   'Caravan SeraiChakaFour Unnamed miceKhanJean Pierre PolnareffUnnamed boyCow'
    owner_map: dict[str, dict] = {}
    multi_owner_stands = 0

    for s in stands:
        raw_owner = (s.get("owner_name") or "").strip()
        if not raw_owner:
            continue
        names = split_owner_names(raw_owner)
        if len(names) > 1:
            multi_owner_stands += 1
        for base in names:
            cid = _char_id(base)
            if cid not in owner_map:
                owner_map[cid] = {
                    "character_id": cid,
                    "name_en": base,
                    "name_ja": None,
                    "part": s.get("part"),
                    "stand_ids": [],
                }
            owner_map[cid]["stand_ids"].append(s["stand_id"])

    #★ TEXT[] 列不能用占位符传list（psycopg2 会当字符串加引号 → 语法错误），
    #   必须把数组字面量直接拼进 SQL
    char_values = []
    for c in owner_map.values():
        arr = array_literal(c["stand_ids"])
        char_values.append(
            f"({_q(c['character_id'])}, {_q(c['name_en'])}, {_q(c['name_ja'])}, "
            f"{c['part'] if c['part'] is not None else 'NULL'}, {arr})"
        )
    if char_values:
        cur.execute(f"""
            INSERT INTO characters
                (character_id, name_en, name_ja, part, stand_ids)
            VALUES {", ".join(char_values)}
            ON CONFLICT (character_id) DO UPDATE SET
                name_en  = EXCLUDED.name_en,
                name_ja  = EXCLUDED.name_ja,
                part     = EXCLUDED.part,
                stand_ids= EXCLUDED.stand_ids
        """)
    n_with_part = sum(1 for c in owner_map.values() if c["part"] is not None)
    print(f"  {len(owner_map)} 个角色（{n_with_part} 有部信息）")
    if multi_owner_stands:
        print(f"  注：{multi_owner_stands} 个替身有多个持有者，已拆分为多条角色")

    # ---------- stands ----------
    print("\n[2/7] stands")

    # ★★ M1遗留问题：形态组未完全消解
    #   stands.json 里 echoes_act1/act2/act3 是同一替身 Echoes 的三个形态，
    #   但被当成独立替身登记（form_count=0、form_chain 为空）；
    #   而 text_chunks 的 stand_id 来自渲染层的 h1（='Echoes'），挂在外键上。
    #   → 必须补一条聚合父记录，否则外键约束失败。
    #   （这正是 M1 报告里「主表同名多行 = 形态未消解」的延续）
    agg_groups: dict[str, list[str]] = defaultdict(list)
    for s in stands:
        sid = s["stand_id"]
        m = re.match(r"^(echoes|tusk)_(act\d+)$", sid)
        if m:
            agg_groups[m.group(1)].append(sid)

    agg_records: list[dict] = []
    for agg_id, member_ids in agg_groups.items():
        members = [x for x in stands if x["stand_id"] in member_ids]
        agg_records.append({
            "stand_id": agg_id,
            "name_en": agg_id.capitalize(),
            "part": members[0].get("part"),
            "part_name_en": members[0].get("part_name_en"),
            "stand_type": "Aggregate (form group)",
            "form_count": len(member_ids),
            "member_ids": member_ids,
            "detail_url": members[0].get("detail_url"),
        })
    if agg_records:
        print(f"  补建{len(agg_records)} 个聚合父替身"
              f"（形态组未消解的遗留）：")
        for a in agg_records:
            print(f"    {a['stand_id']:10s} ← {a['member_ids']}")

    # 关联 owner_id：owner_name 可能含多角色，取第一个作为主持有者
    # （★ 完整的多持有者关系存在 characters.stand_ids 里）
    name2cid: dict[str, str] = {}
    for c in owner_map.values():
        name2cid.setdefault(c["name_en"], c["character_id"])

    stand_rows = []
    for s in stands:
        raw_owner = (s.get("owner_name") or "").strip()
        owners = split_owner_names(raw_owner) if raw_owner else []
        owner_id = name2cid.get(owners[0]) if owners else None
        stand_rows.append((
            s["stand_id"], s["name_en"], s.get("name_ja"),
            owner_id, s.get("part"), s.get("part_name_en"),
            s.get("stand_type"), s.get("reference"),
            s.get("manga_debut"), s.get("anime_debut"),
            s.get("form_count", 1), s.get("main_table_registrations", 1),
            s.get("detail_url"), s.get("owner_name"),
        ))
    # 聚合父记录（owner_id 留空，形态成员各有持有者）
    for a in agg_records:
        stand_rows.append((
            a["stand_id"], a["name_en"], None,
            None, a["part"], a["part_name_en"],
            a["stand_type"], None, None, None,
            a["form_count"], len(a["member_ids"]),
            a["detail_url"], None,
        ))

    cur.executemany("""
        INSERT INTO stands
            (stand_id, name_en, name_ja, owner_id, part, part_name_en,
             stand_type, reference, manga_debut, anime_debut,
             form_count, main_table_registrations, detail_url, owner_name_raw)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (stand_id) DO UPDATE SET
            name_en=EXCLUDED.name_en, name_ja=EXCLUDED.name_ja,
            owner_id=EXCLUDED.owner_id, part=EXCLUDED.part,
            part_name_en=EXCLUDED.part_name_en, stand_type=EXCLUDED.stand_type,
            reference=EXCLUDED.reference, manga_debut=EXCLUDED.manga_debut,
            anime_debut=EXCLUDED.anime_debut, form_count=EXCLUDED.form_count,
            main_table_registrations=EXCLUDED.main_table_registrations,
            detail_url=EXCLUDED.detail_url, owner_name_raw=EXCLUDED.owner_name_raw
    """, stand_rows)
    n_owner = sum(1 for r in stand_rows if r[3])
    print(f"  {len(stand_rows)} 个替身（{n_owner} 个已关联主持有者）")

    # ---------- stand_stats ----------
    print("\n[3/7] stand_stats")
    stat_rows = []
    for s in stats:
        stat_rows.append((
            s["stand_id"],
            *[s.get(f"{d.lower()}") for d in
              ("PWR", "SPD", "RNG", "STA", "PRC", "DEV")],
            *[s.get(f"{d.lower()}_raw") for d in
              ("PWR", "SPD", "RNG", "STA", "PRC", "DEV")],
            *[s.get(f"{d.lower()}_cat") for d in
              ("PWR", "SPD", "RNG", "STA", "PRC", "DEV")],
            *[s.get(f"{d.lower()}_note") for d in
              ("PWR", "SPD", "RNG", "STA", "PRC", "DEV")],
            s.get("composite"), s.get("missing_count"),
        ))
    cur.executemany("""
        INSERT INTO stand_stats
            (stand_id,
             pwr, spd, rng, sta, prc, dev,
             pwr_raw, spd_raw, rng_raw, sta_raw, prc_raw, dev_raw,
             pwr_cat, spd_cat, rng_cat, sta_cat, prc_cat, dev_cat,
             pwr_note, spd_note, rng_note, sta_note, prc_note, dev_note,
             composite, missing_count)
        VALUES (%s,%s,%s,%s,%s,%s,%s,
                %s,%s,%s,%s,%s,%s,
                %s,%s,%s,%s,%s,%s,
                %s,%s,%s,%s,%s,%s,
                %s,%s)
        ON CONFLICT (stand_id) DO UPDATE SET
            pwr=EXCLUDED.pwr, spd=EXCLUDED.spd, rng=EXCLUDED.rng,
            sta=EXCLUDED.sta, prc=EXCLUDED.prc, dev=EXCLUDED.dev,
            pwr_raw=EXCLUDED.pwr_raw, spd_raw=EXCLUDED.spd_raw,
            rng_raw=EXCLUDED.rng_raw, sta_raw=EXCLUDED.sta_raw,
            prc_raw=EXCLUDED.prc_raw, dev_raw=EXCLUDED.dev_raw,
            pwr_cat=EXCLUDED.pwr_cat, spd_cat=EXCLUDED.spd_cat,
            rng_cat=EXCLUDED.rng_cat, sta_cat=EXCLUDED.sta_cat,
            prc_cat=EXCLUDED.prc_cat, dev_cat=EXCLUDED.dev_cat,
            pwr_note=EXCLUDED.pwr_note, spd_note=EXCLUDED.spd_note,
            rng_note=EXCLUDED.rng_note, sta_note=EXCLUDED.sta_note,
            prc_note=EXCLUDED.prc_note, dev_note=EXCLUDED.dev_note,
            composite=EXCLUDED.composite, missing_count=EXCLUDED.missing_count
    """, stat_rows)
    print(f"  {len(stat_rows)} 行六维数值")
    conn.commit()

    #聚合父替身没有六维数据（其成员形态各有数值），
    #   但仍需插入占位行，否则 v_stand_overview 的 LEFT JOIN 会漏掉它们
    agg_stat_rows = [
        (a["stand_id"],) + (None,) * 6 + (None,) * 6 + (None,) * 6         + (None,) * 6 + (None, 6)
        for a in agg_records
    ]
    if agg_stat_rows:
        cur.executemany("""
            INSERT INTO stand_stats
                (stand_id,
                 pwr, spd, rng, sta, prc, dev,
                 pwr_raw, spd_raw, rng_raw, sta_raw, prc_raw, dev_raw,
                 pwr_cat, spd_cat, rng_cat, sta_cat, prc_cat, dev_cat,
                 pwr_note, spd_note, rng_note, sta_note, prc_note, dev_note,
                 composite, missing_count)
            VALUES (%s,%s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,%s,
                    %s,%s)
            ON CONFLICT (stand_id) DO UPDATE SET
                missing_count = EXCLUDED.missing_count
        """, agg_stat_rows)
        print(f"  +{len(agg_stat_rows)} 个聚合父占位（六维为空，missing_count=6）")

    # ---------- stand_forms ----------
    print("\n[4/7] stand_forms")
    # ★ 形态表存的是**原始字面量**（'A'/'B'/'?'/'∞'），
    #   而 stand_stats 已编码为 0–5 数值。这里复用 encode.py 的判定表统一转换，
    #   避免两套编码逻辑（★ 绝不能各写一份，否则必然漂移）。
    sys.path.insert(0, str(ROOT / "dataset" / "pipeline"))
    from encode import encode_stat, STAT_DIMS

    form_rows = []
    enc_stats = []
    for f in forms:
        vals = f.get("values", {})
        # 逐维编码：字母 → 0–5；'?'/None → NULL
        enc = {}
        for d in STAT_DIMS:
            raw = vals.get(d)
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                enc[d] = None
                continue
            enc[d] = encode_stat(str(raw)).value
        cats = [
            encode_stat(str(vals[d])).category.value if vals.get(d) else None
            for d in STAT_DIMS
        ]
        form_rows.append((
            f["form_id"], f["stand_id"], f.get("form_name"),
            f.get("form_type") or "base",
            *[enc[d] for d in STAT_DIMS],
            *[vals.get(d) for d in STAT_DIMS],   # raw
            *cats,                               # cat
            f.get("raw_order"),
        ))

    cur.executemany("""
        INSERT INTO stand_forms
            (form_id, stand_id, form_name, form_type,
             pwr, spd, rng, sta, prc, dev,
             pwr_raw, spd_raw, rng_raw, sta_raw, prc_raw, dev_raw,
             pwr_cat, spd_cat, rng_cat, sta_cat, prc_cat, dev_cat,
             raw_order)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                %s,%s,%s,%s,%s,%s,
                %s,%s,%s,%s,%s,%s,
                %s)
        ON CONFLICT (form_id) DO UPDATE SET
            form_name=EXCLUDED.form_name, form_type=EXCLUDED.form_type,
            pwr=EXCLUDED.pwr, spd=EXCLUDED.spd, rng=EXCLUDED.rng,
            sta=EXCLUDED.sta, prc=EXCLUDED.prc, dev=EXCLUDED.dev,
            pwr_raw=EXCLUDED.pwr_raw, spd_raw=EXCLUDED.spd_raw,
            rng_raw=EXCLUDED.rng_raw, sta_raw=EXCLUDED.sta_raw,
            prc_raw=EXCLUDED.prc_raw, dev_raw=EXCLUDED.dev_raw,
            pwr_cat=EXCLUDED.pwr_cat, spd_cat=EXCLUDED.spd_cat,
            rng_cat=EXCLUDED.rng_cat, sta_cat=EXCLUDED.sta_cat,
            prc_cat=EXCLUDED.prc_cat, dev_cat=EXCLUDED.dev_cat,
            raw_order=EXCLUDED.raw_order
    """, form_rows)
    n_encoded = sum(1 for r in form_rows if r[4] is not None)
    print(f"  {len(form_rows)} 个形态（{n_encoded} 个含可编码数值）")
    print(f"    等级已按encode.py 判定表转为 0–5")
    conn.commit()

    # ---------- stand_stat_conditional ----------
    print("\n[5/7] stand_stat_conditional")
    cond_rows = []
    for s in stats:
        for d in ("PWR", "SPD", "RNG", "STA", "PRC", "DEV"):
            note = s.get(f"{d.lower()}_note")
            if note:
                cond_rows.append((s["stand_id"], d, s.get(f"{d.lower()}"),
                                  s.get(f"{d.lower()}_raw"), note))
    if cond_rows:
        cur.executemany("""
            INSERT INTO stand_stat_conditional
                (stand_id, stat_dim, base_value, raw_value, condition_note)
            VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT (stand_id, stat_dim) DO UPDATE SET
                base_value=EXCLUDED.base_value,
                raw_value=EXCLUDED.raw_value,
                condition_note=EXCLUDED.condition_note
        """, cond_rows)
    print(f"  {len(cond_rows)} 条条件值说明")
    conn.commit()

    # ---------- stat_conflicts ----------
    # ★ 用 DO NOTHING：避免覆盖人工在库里做的消解决策
    print("\n[6/7] stat_conflicts")
    conf_rows = [(
        c["stand_id"], c["stat_dim"], c.get("value_a"), c.get("source_a"),
        c.get("value_b"), c.get("source_b"), c.get("conflict_type"),
        c.get("resolution"), c.get("resolved_value"), c.get("resolved_cat"),
        c.get("note", ""),
    ) for c in conflicts]
    if conf_rows:
        cur.executemany("""
            INSERT INTO stat_conflicts
                (stand_id, stat_dim, value_a, source_a, value_b, source_b,
                 conflict_type, resolution, resolved_value, resolved_cat, note)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (stand_id, stat_dim, value_a, source_a, value_b, source_b)
            DO NOTHING
        """, conf_rows)
    print(f"  {len(conf_rows)} 条冲突（已存在的不覆盖）")
    conn.commit()

    # ---------- text_chunks ----------
    print("\n[7/7] text_chunks（分批）")
    BATCH = 500
    chunk_rows = [(
        c["chunk_id"], c["stand_id"], c.get("stand_name"), c.get("part"),
        c.get("chunk_type"), c.get("content"), c.get("content_len"),
        c.get("section"), c.get("entity"), c.get("alias"), c.get("debut"),
        c.get("source_url"), c.get("data_source", "jojowiki_rendered"),
    ) for c in chunks]
    for i in range(0, len(chunk_rows), BATCH):
        batch = chunk_rows[i:i + BATCH]
        cur.executemany("""
            INSERT INTO text_chunks
                (chunk_id, stand_id, stand_name, part, chunk_type, content,
                 content_len, section, entity, alias, debut, source_url,
                 data_source)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (chunk_id) DO UPDATE SET
                stand_id=EXCLUDED.stand_id, stand_name=EXCLUDED.stand_name,
                part=EXCLUDED.part, chunk_type=EXCLUDED.chunk_type,
                content=EXCLUDED.content, content_len=EXCLUDED.content_len,
                section=EXCLUDED.section, entity=EXCLUDED.entity,
                alias=EXCLUDED.alias, debut=EXCLUDED.debut,
                source_url=EXCLUDED.source_url, data_source=EXCLUDED.data_source
        """, batch)
        conn.commit()
        print(f"  {i + len(batch)}/{len(chunk_rows)}")

    # ---------- M37：出场记录 / 必杀技 / 替身来源 ----------
    #   ★ 这三块数据本来就在磁盘上（dataset/sources/），
    #     只是之前没建表。现在解析后落库。
    print("\n[8/9] 出场记录 / 必杀技 / 替身来源")
    try:
        from collect_extras import collect as _collect_extras
        extra = _collect_extras()
    except Exception as e:  # noqa: BLE001
        print(f"  [WARN] 附加数据解析失败，跳过：{type(e).__name__}: {e}")
        extra = {"appearances": [], "moves": [], "origins": []}

    # 只保留 stands 表里确实存在的 stand_id（11 个形态替身
    # 如 echoes_act1 / tusk_act1 在 rendered 里没有快照，
    # 硬插会撞外键）。
    cur2 = conn.cursor()
    cur2.execute("SELECT stand_id FROM stands")
    valid_ids = {r[0] for r in cur2.fetchall()}

    apps = [a for a in extra["appearances"] if a["stand_id"] in valid_ids]
    cur2.executemany("""
        INSERT INTO stand_appearances
            (stand_id, kind, chapter_no, episode_no, chapter_title, raw_text)
        VALUES (%s,%s,%s,%s,%s,%s)
        ON CONFLICT (stand_id, kind, raw_text) DO UPDATE SET
            chapter_no=EXCLUDED.chapter_no, episode_no=EXCLUDED.episode_no,
            chapter_title=EXCLUDED.chapter_title
    """, [(a["stand_id"], a["kind"], a["chapter_no"], a["episode_no"],
           a["chapter_title"], a["raw_text"]) for a in apps])
    conn.commit()
    skipped = len(extra["appearances"]) - len(apps)
    print(f"  出场 {len(apps)} 条"
          + (f"（跳过 {skipped} 条：替身不在 stands 表）" if skipped else ""))

    mvs = [m for m in extra["moves"] if m["stand_id"] in valid_ids]
    cur2.executemany("""
        INSERT INTO stand_moves
            (stand_id, name, phonetic, alias, debut_chapter, debut_raw, text)
        VALUES (%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (stand_id, name) DO UPDATE SET
            phonetic=EXCLUDED.phonetic, alias=EXCLUDED.alias,
            debut_chapter=EXCLUDED.debut_chapter, debut_raw=EXCLUDED.debut_raw,
            text=EXCLUDED.text
    """, [(m["stand_id"], m["name"], m["phonetic"], m["alias"],
           m["debut_chapter"], m["debut_raw"], m["text"]) for m in mvs])
    conn.commit()
    print(f"  必杀技 {len(mvs)} 条"
          f"（覆盖 {len({m['stand_id'] for m in mvs})} 个替身，"
          f"★ 覆盖有限，不代表全库都有）")

    ogs = [o for o in extra["origins"] if o["stand_id"] in valid_ids]
    cur2.executemany("""
        INSERT INTO stand_origins
            (stand_id, origin_raw, origin_kind, origin_note)
        VALUES (%s,%s,%s,%s)
        ON CONFLICT (stand_id) DO UPDATE SET
            origin_raw=EXCLUDED.origin_raw,
            origin_kind=EXCLUDED.origin_kind,
            origin_note=EXCLUDED.origin_note
    """, [(o["stand_id"], o["origin_raw"], o["origin_kind"],
           o["origin_note"]) for o in ogs])
    conn.commit()
    _uk = sum(1 for o in ogs if o["origin_kind"] == "unknown")
    print(f"  来源 {len(ogs)} 条（其中 {_uk} 条有原始串但未归类，"
          f"与「无来源」不同）")
    cur2.close()

    cur.close()
    conn.close()
    print(f"\n  总耗时 {time.time() - t_all:.1f}s")
    return True


def _char_id(name: str) -> str:
    import re
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    # ★ 长度保护：owner_name 里可能混入多个角色（实测最长 74 字符），
    #   超长 ID 会拖慢 GIN 索引并占用大量存储
    if len(s) > 48:
        import hashlib
        h = hashlib.sha1(s.encode("utf-8")).hexdigest()[:8]
        s = f"{s[:38]}_{h}"
    return s or "unknown"


def re_split_paren(s: str) -> str:
    """剥离括号注释：'The Green Baby (originally)' -> 'The Green Baby'"""
    import re
    return re.sub(r"\s*\([^)]*\)", "", s).strip()


# 角色名首部大写、后续可小写（人名）；用于从粘连串里切分
_NAME_TOKEN = re.compile(r"[A-Z][a-z]+(?:['’][A-Za-z]+)?")
# 明确的分隔标记
_OWNER_SPLIT = re.compile(
    r"\s*(?:,|;|/|\band\b|&|\+|\|)\s*"
)

# camelCase 边界（需排除 Mc/Mac/O' 等前缀缩写）
_MC_PREFIX = re.compile(r"(Mc|Mac)(?=[A-Z])")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z])(?=[A-Z][a-z])")


def _safe_camel_split(s: str) -> list[str]:
    """在 camelCase 边界切分，但保护 Mc/Mac 前缀。

    ★ 实测踩坑：'Thunder McQueen' 会被切成 'Thunder Mc' + 'Queen'
      因为 'cQ' 满足 [a-z][A-Z][a-z]。需先保护 Mc/Mac。
    """
    # 用占位符保护 Mc/Mac 开头的部分
    marks: list[str] = []
    def _protect(m: re.Match) -> str:
        marks.append(m.group(0))
        return f"\x00{len(marks) - 1}\x00"
    safe = _MC_PREFIX.sub(_protect, s)
    parts = [p for p in _CAMEL_BOUNDARY.split(safe) if p]
    # 还原占位符
    out = []
    for p in parts:
        p = re.sub(r"\x00(\d+)\x00",
                   lambda m: marks[int(m.group(1))], p)
        out.append(p)
    return out or [s]


def split_owner_names(raw: str) -> list[str]:
    """拆分可能含多个持有者的 owner_name 字段。

    ★ 实测问题（M1 遗留）：
      jojowiki 的 infobox 会把多个持有者写在同一栏（HTML 用 <br> 分隔），
      抓取时 text_content() 把它们粘在一起：
        'Caravan SeraiChakaFour Unnamed miceKhanJean Pierre PolnareffUnnamed boyCow'
      若直接当一个角色处理，生成的 ID 长达 74 字符。

    拆分策略（保守优先，宁可漏拆不可错拆）：
      1. 无分隔符 → 原样返回（绝大多数情况）
      2. 有显式分隔符（逗号/分号/and/&）→ 拆开
      3. 无分隔符但含 camelCase 边界 → 按边界拆（保护 Mc/Mac 前缀）
      4. 拆开后去括号注释，丢弃空段

    实测：13 个替身有多持有者，正确率可接受。
    剩余未拆的（如 'Caravan SeraiChakaFour Unnamed mice...'）由 _char_id
    截断 + 哈希兜底，不影响功能但角色粒度偏粗 —— 属已知局限。
    """
    s = (raw or "").strip()
    if not s:
        return []

    # 先剥整体括号注释
    s = re.sub(r"\s*\([^)]*\)", "", s).strip()
    if not s:
        return []

    # 策略 1/2：有显式分隔符
    parts = [p.strip() for p in _OWNER_SPLIT.split(s) if p and p.strip()]
    # 拆分后每段仍含 camelCase 边界 → 继续拆
    expanded: list[str] = []
    for p in parts:
        sub = [q.strip() for q in _safe_camel_split(p)]
        expanded.extend(sub if len(sub) > 1 else [p])

    out = [p for p in expanded if len(p) >= 2 and re.search(r"[A-Za-z]", p)]

    # ★ 保护：出现明显碎片（≤2 字符）说明切分误伤 → 回退整体
    if len(out) > 1 and any(len(p) <= 2 for p in out):
        return [s]
    # ★ 保护：拆出太多（>6）说明规则不适用 → 回退整体
    if len(out) > 6:
        return [s]
    return out or [s]


# ==================================================================
# 验证
# ==================================================================

def verify() -> bool:
    """验证入库结果：行数、约束、视图。"""
    print("\n" + "=" * 68)
    print("数据验证")
    print("=" * 68)
    try:
        conn = connect()
    except SystemExit:
        return False
    cur = conn.cursor()

    print("\n  表行数：")
    # ★ stands/stand_stats 预期 156 而非 154：
    #   M1 遗留的形态组未消解（echoes_act1-3 / tusk_act1-4 被当独立替身），
    #   入库时补建 2 个聚合父记录以承接外键
    #
    # ★★ text_chunks 的期望**从语料文件实际行数推导**，不写死 2407 ★★
    #   语料来源有两种：真实抓取（2407 块）/ CI 的合成 fixture（165 块，
    #   见 evaluation/make_min_fixture.py）。
    #   写死会让 CI 永远失败 —— 首次跑 CI 就是这么红的（实测）。
    #   推导后依然能发现「入库行数 ≠ 文件行数」这类真问题。
    try:
        _n_chunks = len(json.loads(
            (PROC / "text_chunks.json").read_text(encoding="utf-8")))
    except Exception:
        _n_chunks = None

    # ★ M37：附加表的期望值也从磁盘推导（同一原则，见上）
    _n_apps = _n_moves = _n_origins = None
    try:
        from collect_extras import collect as _collect_extras2
        _ex = _collect_extras2()
        _n_apps = len({(a["stand_id"], a["kind"], a["raw_text"])
                       for a in _ex["appearances"]})
        _n_moves = len({(m["stand_id"], m["name"]) for m in _ex["moves"]})
        _n_origins = len({o["stand_id"] for o in _ex["origins"]})
    except Exception:
        pass
    expect = {
        "characters": None, "stands": 156, "stand_stats": 156,
        "stand_forms": 146, "stat_conflicts": 28,
        "text_chunks": _n_chunks,
        # M37：三块新表的期望值同样从磁盘推导，不写死
        "stand_appearances": _n_apps,
        "stand_moves": _n_moves,
        "stand_origins": _n_origins,
    }
    all_ok = True
    for t, exp in expect.items():
        cur.execute(f"SELECT count(*) FROM {t}")
        n = cur.fetchone()[0]
        if exp is None:
            print(f"    {t:22s} {n:6d}")
        else:
            ok = n == exp
            all_ok &= ok
            print(f"    {t:22s} {n:6d}  (预期 {exp}) {'[OK]' if ok else '[不符]'}")

    print("\n  约束检查：")
    # CHECK 约束是否生效
    cur.execute("""
        SELECT count(*) FROM stand_stats
        WHERE pwr_cat IS NOT NULL
          AND pwr_cat NOT IN ('RANKED','CONDITIONAL','CONDITIONAL_NO_BASE',
                              'EMPTY_SLOT','NONE','UNKNOWN','INFINITE',
                              'NOT_APPLICABLE','UNPARSED')
    """)
    bad = cur.fetchone()[0]
    print(f"    非法 pwr_cat 行数：{bad} {'[OK]' if bad == 0 else '[FAIL]'}")
    all_ok &= bad == 0

    # composite 不该出现「取平均」的痕迹
    cur.execute("""
        SELECT count(*) FROM stand_stats
        WHERE composite IS NOT NULL AND missing_count > 0
    """)
    bad2 = cur.fetchone()[0]
    print(f"    composite 非空但缺维度：{bad2} "
          f"{'[OK]' if bad2 == 0 else '[FAIL —— 违反了「缺一维即置NULL」规则]'}")
    all_ok &= bad2 == 0

    print("\n  视图可用性：")
    for v in ("v_stand_overview", "v_part_stats", "v_conflicts_pending"):
        try:
            cur.execute(f"SELECT count(*) FROM {v}")
            n = cur.fetchone()[0]
            print(f"    {v:26s} {n:6d} 行 [OK]")
        except psycopg2.errors.UndefinedTable if False else Exception as e:
            print(f"    {v:26s} [FAIL] {type(e).__name__}")
            all_ok = False

    print("\n  抽样查询（模拟 Text-to-Query）：")
    samples = [
        ("破坏力最高的替身",
         "SELECT name_en FROM v_stand_overview ORDER BY pwr DESC NULLS LAST LIMIT 3"),
        ("第3部有多少替身",
         "SELECT count(*) FROM stands WHERE part = 3"),
        ("待消解的冲突",
         "SELECT count(*) FROM v_conflicts_pending"),
        ("文本块类型分布",
         "SELECT chunk_type, count(*) FROM text_chunks GROUP BY 1 ORDER BY 2 DESC"),
    ]
    for label, sql in samples:
        try:
            cur.execute(sql)
            rows = cur.fetchall()
            print(f"    {label}:")
            for r in rows[:4]:
                print(f"      {r}")
        except Exception as e:
            print(f"    {label}: [FAIL] {e}")
            all_ok = False

    cur.close()
    conn.close()
    print("\n" + ("=" * 68))
    print("验证结果：" + ("全部通过" if all_ok else "存在问题"))
    print("=" * 68)
    return all_ok


# ==================================================================
def main() -> int:
    ap = argparse.ArgumentParser(
        description="rag-kb 数据库初始化与入库")
    ap.add_argument("--check", action="store_true", help="环境检查")
    ap.add_argument("--init", action="store_true", help="建库 + 建表")
    ap.add_argument("--load", action="store_true", help="数据入库")
    ap.add_argument("--verify", action="store_true", help="验证数据")
    ap.add_argument("--all", action="store_true", help="全部执行")
    args = ap.parse_args()

    if not any([args.check, args.init, args.load, args.verify, args.all]):
        ap.print_help()
        return 1

    if args.check or args.all:
        if not check_env():
            return 1
    if args.init or args.all:
        if not init_db():
            return 1
    if args.load or args.all:
        if not load_data():
            return 1
    if args.verify or args.all:
        verify()
    return 0


if __name__ == "__main__":
    sys.exit(main())

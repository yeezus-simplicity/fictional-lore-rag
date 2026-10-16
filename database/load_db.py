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
MIGRATIONS_DIR = ROOT / "dataset" / "schema" / "migrations"
# ★ B 方案：文章块语料（若有则追加；缺失时行为与以前完全一致）
ARTICLES = PROC / "article_chunks.json"

# ★ M69：替身类型串切分（dataset/pipeline/stand_type_parts.py）
#   与 api/main.py 共用同一份词表 → 展示层与底层不会再各切各的。
sys.path.insert(0, str(ROOT / "dataset" / "pipeline"))
try:
    from stand_type_parts import (  # type: ignore
        is_aggregate as _st_is_aggregate,
        split_stand_type as _st_split,
    )
except ImportError:  # 词表模块缺失 → 不阻断装载，只跳过派生列
    _st_split = None

    def _st_is_aggregate(raw: Any) -> bool:
        return False


def _stand_type_cols(raw: Optional[str]) -> tuple:
    """返回 (range, kinds, extras, residual, is_agg, cn)。

    ★ 派生列缺失时返回全空 —— 让旧 schema 也能跑（向后兼容）。
    """
    if _st_split is None:
        return (None, None, None, None, "", False, None)
    if _st_is_aggregate(raw):
        return (None, None, None, None, "", True, None)
    p = _st_split(raw)
    cn = p.readable_cn() if p.all_en() else None
    # ★ 数组列传「竖线分隔的字符串」，由 SQL 侧 string_to_array(..., '|') 还原。
    #   不能直接传 list（psycopg2 adapter 渲染 AsIs('{"a"}')，
    #   裸数组字面量在 SELECT/VALUES 里无法推断类型 → 报
    #   `syntax error at or near "{"`，实测 ARRAY['a'] 也不行）；
    #   也不能用 array_literal()（它是给**内联进 SQL 字符串**用的，
    #   走占位符会被整体加引号）。
    #   用 '|' 而非 ','：词表里没有竖线，而 type 串里出现过逗号。
    return (p.range_type, "|".join(p.ranges) or None,
            "|".join(p.kinds) or None, "|".join(p.extras) or None,
            p.residual, False, cn)


def migrations() -> list[Path]:
    """按文件名排序返回全部 migration。

    ★ 为什么从「单个 001」改成「目录下全部」：
      002 是对**已存在的库**做幂等 ALTER（001 用的 CREATE TABLE
      IF NOT EXISTS 对已存在的表不会生效）——
      只跑 001 的话，旧库拿不到 character_id 列，入库会直接报错。
    """
    if not MIGRATIONS_DIR.exists():
        return []
    return sorted(MIGRATIONS_DIR.glob("*.sql"))

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
    # ★ B 方案：从「检查单个 MIGRATION 常量」改为「检查 migrations 目录」
    #   （支持 002 等后续 migration；原 MIGRATION 常量已移除）
    print("\n  Schema：")
    for p in ([SCHEMA] + migrations()):
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

    migs = migrations()
    if migs:
        ok_all = True
        for mig in migs:
            sql = mig.read_text(encoding="utf-8")
            print(f"  执行 {mig.name}（{len(sql):,} 字符）")
            try:
                cur.execute(sql)
                conn.commit()
            except Exception as e:          # noqa: BLE001
                conn.rollback()
                print(f"  [FAIL] {mig.name} 执行失败：{e}")
                ok_all = False
                break
        if not ok_all:
            return False
        print("  [OK] schema 执行完成")
    else:
        sql = SCHEMA.read_text(encoding="utf-8")
        print(f"  执行 {SCHEMA.name}（{len(sql):,} 字符）")
        try:
            cur.execute(sql)
            conn.commit()
            print("  [OK] schema 执行完成")
        except Exception as e:              # noqa: BLE001
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

    # ★ M45：角色日文名（解决 M38 遗留的「角色日文名 0%」）
    #   数据来自 B 方案已抓的角色页 wikitext —— 每个 {{Character Infobox}}
    #   都带 |ja_kanji=，只是此前**从没被提取过**。
    #   → dataset/processed/character_ja.json（由 extract_character_ja.py 生成）
    #   ★ key 用英文名：characters.name_en 是从 stands.owner_name 反推的，
    #     与 jojowiki 页面标题一致，用它对齐不引入新的 id 规则。
    _char_ja: dict[str, str] = {}
    _ja_path = PROC / "character_ja.json"
    if _ja_path.exists():
        try:
            _char_ja = json.loads(_ja_path.read_text(encoding="utf-8"))
        except Exception:
            _char_ja = {}

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
                    # ★ M45：日文名（原名硬编码 None → 长期 0% 覆盖）
                    "name_ja": _char_ja.get(base),
                    "part": s.get("part"),
                    "stand_ids": [],
                }
            owner_map[cid]["stand_ids"].append(s["stand_id"])

    # ★★ M70：`characters` 表**不能只从 owner_map 构建**。
    #   owner_map = 「替身持有者」，而 `text_chunks.character_id` 的外键
    #   指向的是**语料块归属的角色**，两者不是同一集合：
    #   M70 补抓了 196 个「分类里有、但不是任何替身持有者」的角色
    #   （`Lisa Lisa` / `Jonathan Joestar` / `Caesar Anthonio Zeppeli` …），
    #   它们的语料块存在、但表里没有行 → 直接撞外键约束
    #   `text_chunks_character_id_fkey`（实测 Key (memetaa_frog) is not present）。
    #   → 这里 union 上 characters_ext.json 的全量角色；
    #     stand_ids 仍只填 owner_map 里有的（没有替身就是空数组，不编造）。
    _ext_path = PROC / "characters_ext.json"
    _ext: list[dict] = []
    if _ext_path.exists():
        try:
            _ext = json.loads(_ext_path.read_text(encoding="utf-8")).get(
                "characters") or []
        except Exception:
            _ext = []
    n_added = 0
    for c in _ext:
        cid = c.get("character_id")
        if not cid or cid in owner_map:
            continue
        owner_map[cid] = {
            "character_id": cid,
            "name_en": c.get("name_en") or c.get("title") or cid,
            "name_ja": c.get("name_ja"),
            "part": c.get("part"),
            "stand_ids": [],
        }
        n_added += 1
    if n_added:
        print(f"  M70 并入 characters_ext 的 {n_added} 个非持有者角色"
              f"（否则其语料块会撞外键）")

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
    n_ja = sum(1 for c in owner_map.values() if c.get("name_ja"))
    print(f"  {len(owner_map)} 个角色（{n_with_part} 有部信息）")
    if owner_map:
        print(f"  日文名 {n_ja}/{len(owner_map)}"
              f"（{n_ja / len(owner_map) * 100:.0f}%）"
              f"  ← M45 从已抓的角色页 infobox 提取")
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

    _existing_ids = {s["stand_id"] for s in stands}
    agg_records: list[dict] = []
    for agg_id, member_ids in agg_groups.items():
        # ★ 母体已存在于 stands.json → 不再补建。
        #   否则合成记录会在 executemany 里对同一 stand_id 触发
        #   ON CONFLICT DO UPDATE，把真实数据（owner / name_ja /
        #   stand_type）**静默覆盖**成 Aggregate。
        #   实测：B 方案补入 Tusk 母体后，DB 里 tusk 的 owner 与
        #   name_ja 被抹掉、stand_type 变成 'Aggregate (form group)'。
        if agg_id in _existing_ids:
            print(f"  跳过聚合父记录 {agg_id}：母体已在 stands.json"
                  f"（{len(member_ids)} 个形态）")
            continue
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
    # ★★ M58：form_count 以 **stand_forms.json 的形态链**为准 ★★
    #   实测不一致：stands.json 里 tusk 的 form_count=1（只有 base），
    #   而形态链里是 ACT1~ACT4 共 4 个 → 界面「形态数」显示 1，
    #   用户会以为 Tusk 只有一个形态。形态链（M45 建的那份）才是权威。
    #   取不到时回退 stands.json 的原值。
    _form_n: dict[str, int] = {}
    for f in forms:
        _fid = f.get("stand_id")
        if _fid:
            _form_n[_fid] = _form_n.get(_fid, 0) + 1
    for s in stands:
        raw_owner = (s.get("owner_name") or "").strip()
        owners = split_owner_names(raw_owner) if raw_owner else []
        owner_id = name2cid.get(owners[0]) if owners else None
        stand_rows.append((
            s["stand_id"], s["name_en"], s.get("name_ja"),
            owner_id, s.get("part"), s.get("part_name_en"),
            s.get("stand_type"), s.get("reference"),
            s.get("manga_debut"), s.get("anime_debut"),
            _form_n.get(s["stand_id"], s.get("form_count", 1)),
            s.get("main_table_registrations", 1),
            s.get("detail_url"), s.get("owner_name"),
        ) + _stand_type_cols(s.get("stand_type")))
    # 聚合父记录（owner_id 留空，形态成员各有持有者）
    for a in agg_records:
        stand_rows.append((
            a["stand_id"], a["name_en"], None,
            None, a["part"], a["part_name_en"],
            a["stand_type"], None, None, None,
            a["form_count"], len(a["member_ids"]),
            a["detail_url"], None,
        ) + _stand_type_cols(a.get("stand_type")))

    # ★ M69：派生列可能不存在（老 schema）→ 先探测再决定是否写入。
    _has_type_cols = True
    try:
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'stands'
              AND column_name = 'stand_type_ranges'
        """)
        _has_type_cols = cur.fetchone() is not None
    except Exception:
        conn.rollback()
        _has_type_cols = False

    if _has_type_cols:
        cur.executemany("""
            INSERT INTO stands
                (stand_id, name_en, name_ja, owner_id, part, part_name_en,
                 stand_type, reference, manga_debut, anime_debut,
                 form_count, main_table_registrations, detail_url, owner_name_raw,
                 stand_type_range, stand_type_ranges, stand_type_kinds,
                 stand_type_extras, stand_type_residual,
                 stand_type_aggregate, stand_type_cn)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    %s,coalesce(string_to_array(%s,'|'),'{}'::text[]),
                    coalesce(string_to_array(%s,'|'),'{}'::text[]),
                    coalesce(string_to_array(%s,'|'),'{}'::text[]),
                    %s,%s,%s)
            ON CONFLICT (stand_id) DO UPDATE SET
                name_en=EXCLUDED.name_en, name_ja=EXCLUDED.name_ja,
                owner_id=EXCLUDED.owner_id, part=EXCLUDED.part,
                part_name_en=EXCLUDED.part_name_en, stand_type=EXCLUDED.stand_type,
                reference=EXCLUDED.reference, manga_debut=EXCLUDED.manga_debut,
                anime_debut=EXCLUDED.anime_debut, form_count=EXCLUDED.form_count,
                main_table_registrations=EXCLUDED.main_table_registrations,
                detail_url=EXCLUDED.detail_url, owner_name_raw=EXCLUDED.owner_name_raw,
                stand_type_range=EXCLUDED.stand_type_range,
                stand_type_ranges=EXCLUDED.stand_type_ranges,
                stand_type_kinds=EXCLUDED.stand_type_kinds,
                stand_type_extras=EXCLUDED.stand_type_extras,
                stand_type_residual=EXCLUDED.stand_type_residual,
                stand_type_aggregate=EXCLUDED.stand_type_aggregate,
                stand_type_cn=EXCLUDED.stand_type_cn
        """, stand_rows)
    else:
        # 老 schema：只写原有 14 列（派生列由 migration 004 补齐后重跑）
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
        """, [r[:14] for r in stand_rows])
        print("  ⚠ 未找到 stand_type_range 列：跳过 M69 派生列"
              "（先跑 migration 004 再重跑 load_db）")
    n_owner = sum(1 for r in stand_rows if r[3])
    print(f"  {len(stand_rows)} 个替身（{n_owner} 个已关联主持有者）")
    # 派生列布局（下标）：14=range 15=ranges 16=kinds 17=extras
    #                   18=residual 19=aggregate 20=cn
    # ★ 这里硬编码下标很脆（我第一次就写成了 17 → 把 extras 当 residual，
    #   报出「残余 106」这种看着像真问题的假警报）。
    #   → 用 tuple unpack 拿，名字对不上就会立刻报错而不是静默算错。
    _n_res = 0
    for _r in stand_rows:
        (_sid, _en, _ja, _oid, _pt, _ptn, _st, _ref, _md, _ad,
         _fc, _mtr, _du, _own,
         _rng, _rngs, _kinds, _extras, _res, _agg, _cn) = _r
        if _res:
            _n_res += 1
    if _has_type_cols:
        print(f"  M69 类型切分：{len(stand_rows)} 条"
              f"（未识别残余 {_n_res} 条）")

    # ---------- ★★ 清掉本轮不再产生的角色行 ★★ ----------
    #   `ON CONFLICT (character_id) DO UPDATE` 只更新/插入，**从不删除** ——
    #   修好 owner 拆分（M67）之后，那 10 个"粘连 id"会**永久留在库里**，
    #   于是「本地库」比「全新库」多出一批脏行 —— 这正是 M12/CI 那类
    #   「本地能跑 ≠ 全新部署能跑」的翻版（**只是这次是数据多、不是代码缺**）。
    #   ★ 只删**没有任何 stands.owner_id 指向**的行：绝不误删在用角色。
    #   ★ 必须放在 stands 入库**之后** —— 在那之前 owner_id 还指着旧值，
    #     删了会踩外键。
    if owner_map:
        #   ★ 数组要用 `array_literal` 直接拼（本项目惯例）——
        #     占位符传 list 会被当成字符串加引号 → `syntax error at or near "{"`
        #     （实测踩到，与本文件里 TEXT[] 那两个字段同一个坑）。
        _ids = array_literal(list(owner_map))
        cur.execute(f"""
            DELETE FROM characters c
             WHERE NOT (c.character_id = ANY({_ids}))
               AND NOT EXISTS (SELECT 1 FROM stands s
                                WHERE s.owner_id = c.character_id)
        """)
        if cur.rowcount:
            print(f"  清理已不再产生的角色行：{cur.rowcount} 条"
                  f"（owner 拆分修复后的残留，仅删无人引用的）")

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
            # ★ M45：形态链的父子指针。此前根本没读这个字段 →
            #   即便 stand_forms.json 里有 parent_id，DB 里也永远是 NULL
            #   （schema 早已定义该列并带外键，只是入库时漏了）。
            f.get("parent_id"),
            *[enc[d] for d in STAT_DIMS],
            *[vals.get(d) for d in STAT_DIMS],   # raw
            *cats,                               # cat
            f.get("raw_order"),
        ))

    cur.executemany("""
        INSERT INTO stand_forms
            (form_id, stand_id, form_name, form_type, parent_id,
             pwr, spd, rng, sta, prc, dev,
             pwr_raw, spd_raw, rng_raw, sta_raw, prc_raw, dev_raw,
             pwr_cat, spd_cat, rng_cat, sta_cat, prc_cat, dev_cat,
             raw_order)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                %s,%s,%s,%s,%s,%s,
                %s,%s,%s,%s,%s,%s,
                %s)
        ON CONFLICT (form_id) DO UPDATE SET
            form_name=EXCLUDED.form_name, form_type=EXCLUDED.form_type,
            parent_id=EXCLUDED.parent_id,
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

    # ---------- 地点维度（M67 补：M65 只建了表，没写 loader）----------
    #   ★★ 为什么必须在这一步补上（实测踩到）★★
    #     `text_chunks.location_id` 有外键指向 `locations`，
    #     而 `locations` 一直是**空表**（M65 只跑了 `--init` 建表）→
    #     地点块入库时 ForeignKeyViolation：
    #       Key (location_id)=(air_supplena_island) is not present in table "locations"
    #     ★ 服务层从 JSON 读、不查 DB，所以**线上功能一直是好的** ——
    #       空表只在"真正把全量语料灌进全新库"时才会炸。
    #       这正是「本地能跑 ≠ 全新部署能跑」的又一形态。
    print("\n[6.5/7] 地点维度（locations / location_places / location_mentions）")
    loc_p = PROC / "locations.json"
    if loc_p.exists():
        loc_raw = json.loads(loc_p.read_text(encoding="utf-8"))
        kept = [e for e in loc_raw.get("locations", []) if e.get("included")]
        zh_map = {}
        zh_p = PROC / "location_name_zh.json"
        if zh_p.exists():
            try:
                for e in json.loads(zh_p.read_text(encoding="utf-8")).get("entries", []):
                    if e.get("name_zh"):
                        zh_map[e["location_id"]] = e["name_zh"]
            except Exception:                       # noqa: BLE001
                pass

        def _debut_chapter(s) -> int | None:
            m = re.search(r"Chapter\s+(\d+)", str(s or ""))
            return int(m.group(1)) if m else None

        # ★ `TEXT[]` 走**拼接**而不是占位符（本项目惯例，见 array_literal 的说明）：
        #   实测占位符传 list 时，psycopg2 会渲染成 `{"a","b"}` 这种**未加引号**的
        #   数组字面量 → `syntax error at or near "{"`（SQL 里数组字面量必须整体
        #   写成字符串 `'{"a"}'`）。→ 用 `array_literal` 直接拼进 SQL。
        loc_vals = []
        for e in kept:
            loc_vals.append(
                "(" + ", ".join([
                    _q(e["location_id"]), _q(e["title"]),
                    _q(e.get("name_ja")), _q(e.get("name_romaji")),
                    _q(e.get("type")), array_literal(e.get("types") or []),
                    str(e["part"]) if e.get("part") is not None else "NULL",
                    _q(e.get("part_basis")), _q(e.get("mangadebut")),
                    _q(e.get("animedebut")),
                    (str(_debut_chapter(e.get("mangadebut")))
                     if _debut_chapter(e.get("mangadebut")) is not None else "NULL"),
                    _q(e.get("source_url") or
                       f"https://jojowiki.com/{str(e['title']).replace(' ', '_')}"),
                    (str(e["n_infobox_fields"])
                     if e.get("n_infobox_fields") is not None else "NULL"),
                    _q("jojowiki_location"),
                ]) + ")"
            )
        if loc_vals:
            cur.execute("""
                INSERT INTO locations
                    (location_id, name_en, name_ja, name_romaji, primary_type,
                     types, part, part_basis, mangadebut, animedebut,
                     debut_chapter, source_url, n_infobox_fields, data_source)
                VALUES """ + ", ".join(loc_vals) + """
                ON CONFLICT (location_id) DO UPDATE SET
                    name_en=EXCLUDED.name_en, name_ja=EXCLUDED.name_ja,
                    name_romaji=EXCLUDED.name_romaji,
                    primary_type=EXCLUDED.primary_type, types=EXCLUDED.types,
                    part=EXCLUDED.part, part_basis=EXCLUDED.part_basis,
                    mangadebut=EXCLUDED.mangadebut,
                    animedebut=EXCLUDED.animedebut,
                    debut_chapter=EXCLUDED.debut_chapter,
                    source_url=EXCLUDED.source_url,
                    n_infobox_fields=EXCLUDED.n_infobox_fields,
                    data_source=EXCLUDED.data_source
            """)
        loc_rows = kept          # 仅用于下面的计数打印
        print(f"  {len(loc_rows)} 个地点"
              f"（有中文名 {len(zh_map)}）")

        # 归属层级：**先删后插**（`place_paths` 是整体的，增量更新没意义）
        cur.execute("DELETE FROM location_places")
        place_rows = []
        for e in kept:
            for pi, chain in enumerate(e.get("place_paths") or []):
                for di, nm in enumerate(chain):
                    place_rows.append((e["location_id"], pi, di, nm))
        if place_rows:
            cur.executemany("""
                INSERT INTO location_places
                    (location_id, path_index, depth, place_name)
                VALUES (%s,%s,%s,%s)
                ON CONFLICT (location_id, path_index, depth) DO UPDATE
                    SET place_name=EXCLUDED.place_name
            """, place_rows)
        print(f"  {len(place_rows)} 条归属层级")

        men_p = PROC / "location_mentions.json"
        if men_p.exists():
            men = json.loads(men_p.read_text(encoding="utf-8"))
            valid = {e["location_id"] for e in kept}
            men_rows = [(
                m["location_id"], m.get("chunk_id"), m.get("entity_kind"),
                m.get("entity_id"), m.get("direction", "entity_page"),
                m.get("match_kind"), m.get("matched_term"), m.get("snippet"),
            ) for m in (men.get("mentions") or [])
                if m.get("location_id") in valid]
            # 先删后插：关联表是**派生产物**，按最新语料整体重建
            cur.execute("DELETE FROM location_mentions")
            for i in range(0, len(men_rows), 1000):
                cur.executemany("""
                    INSERT INTO location_mentions
                        (location_id, chunk_id, entity_kind, entity_id,
                         direction, match_kind, matched_term, snippet)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """, men_rows[i:i + 1000])
            print(f"  {len(men_rows)} 条关联（强 "
                  f"{sum(1 for r in men_rows if r[4] == 'location_page')}）")
        conn.commit()
    else:
        print("  跳过（缺 locations.json）")

    # ---------- text_chunks ----------
    print("\n[7/7] text_chunks（分批）")
    # ★ B 方案：若有文章块语料（article_chunks.json）则并入。
    #   缺失时行为与以前完全一致（向后兼容）——
    #   该文件是抓取派生产物，已按第三方版权约定 gitignore。
    art: list[dict] = []
    if ARTICLES.exists():
        try:
            art = json.loads(ARTICLES.read_text(encoding="utf-8"))
            print(f"  + 文章块 {len(art)} 条（来自 {ARTICLES.name}）")
        except Exception as e:              # noqa: BLE001
            print(f"  [WARN] {ARTICLES.name} 解析失败，跳过：{e}")
            art = []
    all_chunks = list(chunks) + art

    BATCH = 500
    # ★ character_id：替身页块为 None，角色页块取自 extra.kind=='character'
    # ★★ M67 补：**地点块必须带 location_id** ★★
    #   约束 `chk_chunk_owner` 要求「替身/角色/地点 三选一」，
    #   而这里原先只写 stand_id / character_id → 地点块直接违反约束：
    #     CheckViolation: chk_chunk_owner
    #     Failing row contains (6762, null, 'Air Supplena Island', …)
    #   ★★ 为什么一直没暴露（值得记）：CI 用的是合成语料
    #      （make_min_fixture 只造替身/角色块），**没有地点块** →
    #      这条路径在 CI 里永远走不到；而本地库里 text_chunks 是 M65 之前
    #      灌的，之后只跑过 `--init`（只建表、不入库）→ 两边都看不见。
    #   这正是「**本地能跑 ≠ 全新部署能跑**」的又一形态：
    #      缺的不是代码，是"某条数据路径从来没被跑过"。
    chunk_rows = [(
        c["chunk_id"], c.get("stand_id"), c.get("stand_name"), c.get("part"),
        c.get("chunk_type"), c.get("content"), c.get("content_len"),
        c.get("section"), c.get("entity"), c.get("alias"), c.get("debut"),
        c.get("source_url"), c.get("data_source", "jojowiki_rendered"),
        c.get("character_id"), c.get("location_id"),
    ) for c in all_chunks]
    for i in range(0, len(chunk_rows), BATCH):
        batch = chunk_rows[i:i + BATCH]
        cur.executemany("""
            INSERT INTO text_chunks
                (chunk_id, stand_id, stand_name, part, chunk_type, content,
                 content_len, section, entity, alias, debut, source_url,
                 data_source, character_id, location_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (chunk_id) DO UPDATE SET
                stand_id=EXCLUDED.stand_id, stand_name=EXCLUDED.stand_name,
                part=EXCLUDED.part, chunk_type=EXCLUDED.chunk_type,
                content=EXCLUDED.content, content_len=EXCLUDED.content_len,
                section=EXCLUDED.section, entity=EXCLUDED.entity,
                alias=EXCLUDED.alias, debut=EXCLUDED.debut,
                source_url=EXCLUDED.source_url, data_source=EXCLUDED.data_source,
                character_id=EXCLUDED.character_id,
                location_id=EXCLUDED.location_id
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


def _loc_id(title: str) -> str:
    """★ M65：地点 id。

    ★★ 与替身不同：**必须保留括号** ★★
      jojowiki 上 `Morioh` 与 `Morioh (JoJolion)` 是**两个不同的地点**
      （第 4 部 vs 第 8 部的同名城镇），`Higashikata Residence (JoJolion)` 同理。
      替身的 `make_stand_id` 会剥括号 —— 那正是「跨部同名 id 冲突」
      这个已知待办的成因（`Killer Queen (JoJolion)` 撞第 4 部）。
      **地点不能重蹈**：直接走 `_char_id` 的规则（非字母数字一律变 `_`），
      `Morioh` → `morioh`、`Morioh (JoJolion)` → `morioh_jojolion`，天然区分。
    """
    return _char_id(title)


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
      1. **有显式分隔符**（逗号/分号/斜杠/and/&/+/|）→ 按它拆，**不再套用启发式保护**
         ★ M67 起这是主路径：owner 粘连修好后，多持有者字段本身就是 `, ` 分隔的
      2. 无分隔符但含 camelCase 边界 → 按边界拆（保护 Mc/Mac 前缀）
      3. 拆开后丢弃空段与"没有真名字"的碎片（如被 `/` 切开的 `N/A`）

    ★★ M67 踩到的坑：原来有两条启发式保护，其中「拆出 >6 段 → 回退整体」
      把 **Anubis 的 7 个持有者**当成了"过拆" → 整串被当成**一个角色**
      （DB 里出现 `caravan_serai_chaka_four_unnamed_mice__0152b175`）。
      保护逻辑本身没错，错的是**把它套用在源站已明确写出分隔符的串上**。
      → 现在只在启发式（策略 2）路径上保留保护。
    """
    s = (raw or "").strip()
    if not s:
        return []

    # 先剥整体括号注释
    s = re.sub(r"\s*\([^)]*\)", "", s).strip()
    if not s:
        return []

    def _real_name(p: str) -> bool:
        """是不是一个"真名字"：至少两个字母（挡掉 `N` / `A` / `-` 这类碎片）。"""
        return len(p) >= 2 and bool(re.search(r"[A-Za-z]{2}", p))

    # 策略 1：**显式分隔符**（源站写明的，直接信任；不做 >6 段回退）
    if _OWNER_SPLIT.search(s):
        parts = [p.strip(" ,;·/|+") for p in _OWNER_SPLIT.split(s)]
        out = [p for p in parts if _real_name(p)]
        if out:
            return out
        return [s]

    # 策略 2：无分隔符 → camelCase 启发式（保守，保留两条保护）
    parts = [p.strip() for p in _safe_camel_split(s) if p.strip()]
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
    # ★★ stands / stand_stats 的期望也改为**从磁盘推导**，不写死 156 ★★
    #   原值 156 = 154(stands.json) + 2(echoes/tusk 聚合父记录)。
    #   ★ 补第 8/9 部替身后 154 会变 → 写死必然不符。
    #     这与上方 text_chunks 的处理是同一原则
    #     （「断言绑死具体数字」在换数据形态后必然失败，实测踩过）。
    #   ★ agg_records 在 load_data() 里，此处是 verify() —— 不能直接引用，
    #     按**同一规则**重算：echoes/tusk 形态组各补 1 条聚合父记录。
    try:
        _stands_json = json.loads(
            (PROC / "stands.json").read_text(encoding="utf-8"))
        _agg_ids = set()
        _present = {_s.get("stand_id") for _s in _stands_json}
        for _s in _stands_json:
            _m = re.match(r"^(echoes|tusk)_(act\d+)$", _s.get("stand_id", ""))
            if _m:
                _agg_ids.add(_m.group(1))
        # ★ 仅补建**母体不在 stands.json** 的聚合记录（与 load_data 同规则）
        _n_stands = len(_stands_json) + len(_agg_ids - _present)
    except Exception:
        _n_stands = None

    # ★ text_chunks 的期望 = 原语料 + 文章块语料（load_data 里两者合并写入，
    #   见 `all_chunks = list(chunks) + art`）
    #   原实现只读 text_chunks.json → 入库含文章块后必然不符。
    try:
        _n_chunks = len(json.loads(
            (PROC / "text_chunks.json").read_text(encoding="utf-8")))
        if ARTICLES.exists():
            _n_chunks += len(json.loads(ARTICLES.read_text(encoding="utf-8")))
    except Exception:
        _n_chunks = None

    # ★ M45：stand_forms / stat_conflicts 也改从磁盘推导
    _n_forms = _n_conflicts = None
    try:
        _n_forms = len(json.loads(
            (PROC / "stand_forms.json").read_text(encoding="utf-8")))
    except Exception:
        pass
    try:
        _n_conflicts = len(json.loads(
            (PROC / "conflicts.json").read_text(encoding="utf-8")))
    except Exception:
        pass

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
        "characters": None,
        # ★ 动态（见上方推导）；原写死 156
        "stands": _n_stands, "stand_stats": _n_stands,
        # ★ M45：同样改为从磁盘推导 —— 补形态链后 stand_forms 146 → 153，
        #   写死必然报「不符」（与 stands / text_chunks 是同一类坑）。
        "stand_forms": _n_forms, "stat_conflicts": _n_conflicts,
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

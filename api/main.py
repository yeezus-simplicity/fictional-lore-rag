"""
fictional-lore-rag API 服务（M5）。

把 M1-M4 的四层串成可调用的系统：
    M4 路由 → M3 检索 / SQL 执行 → M4 消解信息 → 证据返回

★★ 核心立场：抽取式回答，零生成
  返回的每句话都是数据源原文，不做改写、不做推断。
  理由：M1-M4 都没有生成层，若这里用模板拼句子，
       等于引入一个未被评测的组件。**诚实比好看重要。**
  M8 接LLM 后，只需替换 answer 字段的生成方式，其余不动。

启动：
    python api/main.py                 # 默认 127.0.0.1:8000
    python api/main.py --reload         # 开发模式
    python api/main.py --port 9000
    python api/main.py --no-vector      # 纯 BM25（启动快，无 GPU 依赖）

接口文档：启动后访问 http://127.0.0.1:8000/docs
"""

from __future__ import annotations

import argparse
import json
import re# ★ M14：别名表要用它判断「含中文」
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services"))
sys.path.insert(0, str(ROOT / "database"))
sys.path.insert(0, str(ROOT / "retrieval"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402

from load_db import PG  # noqa: E402
# ★★ M17：雷达图 + 图片服务
#   ★★ M18：ensure_images 改异步（image_payload）——
#      原ensure_images 会阻塞等抓取完成，实测没缓存的替身要 87 秒，
#      用户反馈「等很久还在检索、再点一下就出答案」。
#      现在回答立即返回，图片走后台任务，前端轮询 /images/status/{id}。
from media import (image_payload, public_images, radar_payload,  # noqa: E402
                   status as image_status)
from radar_chart import radar_svg  # noqa: E402

IMG_DIR = ROOT / "images"

# ==================================================================
# 全局状态
# ==================================================================

STATE: dict[str, Any] = {}


def _init_state(use_vector: bool = True,
                chunk_merge_target: Optional[int] = None,
                images_enabled: bool = True) -> None:
    """启动时初始化：数据库 + 路由器 + 执行器。

    Args:
        use_vector: 是否启用向量检索
        chunk_merge_target:
            ★ M12：块合并目标（字符）。None/0 = 不合并（默认）。
            512 = M9 实测推荐（空洞率 0.50 → 0.30，代价延迟 +52%）。
            详见 retrieval/merging.py 的模块文档。
        images_enabled:
            ★ M17：是否返回替身图片（--no-images 关闭）。
            必须在启动时写进 STATE，_images_enabled() 只认 STATE。
    """
    t0 = time.time()
    STATE["images_enabled"] = bool(images_enabled)

    # --- 数据库（★ 失败时降级，不阻断启动）---
    #   理由：M3 的检索索引是 pickle 缓存，**不依赖数据库**。
    #   DB 挂了仍应能提供语义问答，只是结构化查询与数据浏览不可用。
    #   （实测踩坑：Docker Desktop 没启动时整个服务直接崩，
    #     连网页界面都打不开——但那时语义检索其实完全可用。）
    db_ok = True
    try:
        STATE["conn"] = psycopg2.connect(**PG)
        STATE["conn"].autocommit = True

        cur = STATE["conn"].cursor()
        # ★ stands 表的列是 owner_name_raw
        #   （owner_name 会与 characters.name_en 冲突）
        cur.execute("SELECT stand_id, name_en, part_name_en, owner_name_raw "
                    "FROM stands")
        rows = cur.fetchall()
        cur.close()

        stands = [{"stand_id": r[0], "name_en": r[1],
                   "part_name_en": r[2], "owner_name": r[3]} for r in rows]
    except Exception as e:
        db_ok = False
        STATE["conn"] = None
        print(f"[api] ⚠ 数据库不可用（{type(e).__name__}）")
        print("[api]   降级为「仅语义检索」模式：")
        print("[api]   ✓ 语义问答可用（走本地索引，不依赖 DB）")
        print("[api]   ✗ 结构化查询 / 数据浏览 / 冲突记录不可用")
        print("[api]   启动数据库：cd docker && docker compose up -d postgres")
        # 从 JSON 兜底拿替身名（路由与拒答检测仍需要实体表）
        sj = ROOT / "dataset" / "processed" / "stands.json"
        stands = []
        if sj.exists():
            data = json.loads(sj.read_text(encoding="utf-8"))
            for x in data:
                nm = x.get("name_en")
                if not nm:
                    continue
                stands.append({
                    "stand_id": x.get("stand_id"),
                    "name_en": nm,
                    "part_name_en": x.get("part_name_en"),
                    "owner_name": x.get("owner_name_raw"),
                })
        print(f"[api]   已从 stands.json 载入 {len(stands)} 个替身名（仅用于路由）")
    STATE["db_ok"] = db_ok

    # --- 已知实体（★ 三类：替身 / 部名 / 使用者名，M4 踩过的坑）---
    known_stands = {s["name_en"] for s in stands if s.get("name_en")}
    known_entities = set(known_stands)
    known_entities |= {s.get("part_name_en") for s in stands
                       if s.get("part_name_en")}
    known_entities |= {s.get("owner_name") for s in stands
                       if s.get("owner_name")}
    # ★ M14：把中文别名也纳入已知实体 ——
    #   否则用户问「黄金体验…」时，路由器会认为「黄金体验」是编造的
    #   → 触发拒答。
    try:
        from aliases import build_alias_table as _bat
        _a2i, _i2a = _bat()
        for _names in _i2a.values():
            for _n in _names:
                if _n and re.search(r"[\u4e00-\u9fff]", _n):
                    known_entities.add(_n)
    except Exception:
        pass
    known_entities.discard(None)
    known_entities.discard("")

    # ★★ M20：把**角色名**也纳入已知实体 ★★
    #   用户问「东方定助的替身是什么」，若路由器不认角色名
    #   → 判成编造实体 → 拒答；而「Jotaro Kujo 的替身」同样会被拒。
    #   实测 characters 表有 144 个角色，但**只有英文名**（中文名 0 个），
    #   而别名词典里只有替身名 —— 角色名两边都没有，必须从DB 补。
    try:
        _c = STATE["conn"].cursor()
        _c.execute("SELECT name_en FROM characters")
        for (_n,) in _c.fetchall():
            if _n:
                known_entities.add(_n)
        _c.close()
    except Exception:
        pass

    # ★★ M21：角色**中文名**也纳入已知实体 ★★
    #   否则「东方定助的替身是什么」会被判成编造实体而拒答
    #   （M20 只加了英文角色名，中文名这条路还是断的）。
    try:
        from aliases import load_char_zh_map as _lcz
        for _cid, _names in _lcz().items():
            for _n in _names:
                if _n:
                    known_entities.add(_n)
    except Exception:
        pass

    # --- 路由器（M4）---
    from conflict_resolver import Router
    # ★★ M65：地点名单独一份传给路由器 ★★
    #   为什么不并进 known_entities：地名**不能**被当成替身名。
    #   若混在一起，「安杰罗岩」会被判成「锚定替身」→ 走 entity_semantic
    #   → 拿一个不存在的 stand_id 去检索原文（返回无关内容）。
    known_locations: set = set()
    try:
        from locations import all_names as _loc_names
        known_locations = set(_loc_names())
        print(f"[api] 地点维度：{len(known_locations)} 个写法"
              f"（{__import__('locations').n_locations()} 个正传地点）")
    except Exception as e:  # noqa: BLE001
        print(f"[api] ⚠ 地点维度不可用（{type(e).__name__}）："
              f"地点类问题将按普通语义处理")
    STATE["known_locations"] = known_locations
    STATE["router"] = Router(known_stands=known_stands,
                             known_entities=known_entities,
                             known_locations=known_locations)
    STATE["known"] = known_stands
    # ---- name → id 映射（★ M14：全量别名，不只英文名）----
    #实测问题：用户问「黄金体验的能力」，而 name2id 只有英文名
    #   → 认不出实体 → 兜底返回篇章统计表（完全答非所问）。
    # → 这里用 aliases.build_alias_table()，含
    #   英文名 + 日文名 + 中文别名（含简繁通配）。
    #
    # ★ 降级：别名表构建失败时退回「仅英文名」，不阻断启动。
    name2id = {s["name_en"]: s["stand_id"]
               for s in stands if s.get("name_en")}
    try:
        from aliases import build_alias_table
        alias2id, id2alias = build_alias_table()
        if alias2id:
            # ★ 合并（别名表里也有英文名，这里以别名表为准更全）
            name2id = {**name2id, **alias2id}
            STATE["id2alias"] = id2alias
            # ★★ M16：留一份「stand_id → 中文名记录」，供 /stands/{id} 返回 name_zh
            #   （DB 的 stands 表没有中文名列，中文名只在词典里）
            try:
                from aliases import load_zh_map
                STATE["zh_map"] = load_zh_map()
            except Exception:
                STATE["zh_map"] = {}
            n_zh = sum(1 for v in id2alias.values()
                       if any(re.search(r"[\u4e00-\u9fff]", x) for x in v))
            print(f"[api] 别名表：{len(name2id)} 个名字 → "
                  f"{len(set(name2id.values()))} 个替身"
                  f"（其中 {n_zh} 个带中文名）")
    except Exception as e:
        print(f"[api] ⚠ 别名表构建失败（{type(e).__name__}），"
              f"退回仅英文名：中文名提问会识别不了")
    STATE["name2id"] = name2id

    # --- M25：会话存储（多轮追问）---
    #   ★ 进程内存 + TTL，不引入外部依赖（单机演示服务够用）
    try:
        from coref import SessionStore
        STATE["sessions"] = SessionStore(ttl_sec=1800, max_sessions=500)
        print("[api] 多轮追问：已启用（会话 TTL 30 分钟）")
    except Exception as e:  # noqa: BLE001
        STATE["sessions"] = None
        print(f"[api] ⚠ 多轮追问不可用：{type(e).__name__}")

    # --- M25：角色名索引（指代消解要判断「他」指哪个角色）---
    STATE["char2id"] = {}
    # ★ M33：同时保留**反向**映射（character_id → 中文名），
    #   中文摘要卡要拿它把使用者的英文名换成中文名。
    STATE["char_zh_map"] = {}
    if db_ok:
        try:
            from aliases import build_char_alias_table, load_char_zh_map
            STATE["char2id"] = dict(build_char_alias_table())
            STATE["char_zh_map"] = dict(load_char_zh_map())
            # ★ 再补英文名：build_char_alias_table 只给中文名，
            #   而用户也可能用英文名追问（"what about his other stands"）
            _cc = STATE["conn"].cursor()
            _cc.execute("SELECT character_id, name_en FROM characters")
            for _cid, _en in _cc.fetchall():
                if _en:
                    STATE["char2id"].setdefault(_en, _cid)
            _cc.close()
            print(f"[api] 角色名索引：{len(STATE['char2id'])} 个写法")
        except Exception:  # noqa: BLE001
            STATE["char2id"] = {}

    # --- 结构化执行器（★ 需要 DB）---
    STATE["structured"] = None
    if db_ok:
        from executor import StructuredExecutor
        STATE["structured"] = StructuredExecutor(STATE["conn"])

    # --- 语义执行器（M3 检索层）---
    from semantic_executor import SemanticExecutor
    sem = SemanticExecutor(use_vector=use_vector, verbose=True,
                           chunk_merge_target=chunk_merge_target)
    t_sem = time.time()
    vector_ok = sem.load()
    STATE["semantic"] = sem
    STATE["vector_ok"] = vector_ok
    # ★ M12：记录索引配置，health 与界面都要读它
    STATE["chunk_merge_target"] = chunk_merge_target
    # ★ M15：替身 → 别名列表（模糊候选提示用）
    #   正常路径已在上面设过；异常降级时兜底为空 dict（不崩）
    STATE.setdefault("id2alias", {})

    # --- 生成器（M6/M11）★ 延迟加载 ---
    #   不在这里 new：加载模型要 ~20 秒 + 3GB 显存，
    #   而默认走抽取式，根本不需要模型。
    #   用户传 mode=generate 时才加载（见 _get_generator）。
    STATE["generator"] = None
    STATE["generator_error"] = None

    # --- 冲突消解状态（M4）★ 需要 DB ---
    try:
        if not STATE.get("db_ok"):
            raise RuntimeError("数据库不可用")
        cur = STATE["conn"].cursor()
        cur.execute("""
            SELECT resolution, count(*),
                   count(*) FILTER (WHERE resolved_value IS NOT NULL)
            FROM stat_conflicts GROUP BY 1
        """)
        STATE["resolutions"] = {
            r[0]: {"n": r[1], "n_resolved": r[2]}
            for r in cur.fetchall()
        }
        cur.execute("SELECT count(*) FROM stat_conflicts "
                    "WHERE resolved_value IS NULL")
        STATE["n_unresolved"] = cur.fetchone()[0]
        cur.close()
    except Exception as e:
        STATE["resolutions"] = {}
        STATE["n_unresolved"] = -1
        print(f"[api] 冲突状态读取失败：{e}")

    print(f"[api] 初始化完成，用时 {time.time() - t0:.1f}s")
    print(f"[api]   替身 {len(stands)} 个 / 已知实体 {len(known_entities)} 个")
    print(f"[api]   语义检索：{'BM25 + bge-m3' if vector_ok else '仅 BM25'}"
          f"（{time.time() - t_sem:.1f}s）")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ★ 直接用 `uvicorn api:app` 启动时不会走 main()，
    #   app.state.use_vector 未设置→ 用 getattr 兜底
    use_vector = getattr(app.state, "use_vector", True)
    # ★ M12：块合并配置（None = 不合并，保持 M6–M11 的历史行为）
    chunk_merge = getattr(app.state, "chunk_merge_target", None)
    # ★★ M17：图片开关必须在这里传进 STATE ★★
    #   ★ 踩坑：只写 app.state.images_enabled 是不够的——
    #     _images_enabled() 优先读 STATE，而STATE 是 _init_state 建的，
    #     不显式传进去就永远是默认值 True → --no-images 静默失效
    #     （实测 /stands/{id} 在 --no-images 下仍返回 9 张图）。
    _init_state(use_vector=use_vector, chunk_merge_target=chunk_merge,
                images_enabled=getattr(app.state, "images_enabled", True))
    yield
    conn = STATE.get("conn")
    if conn:
        conn.close()
    print("[api] 已关闭")


# ==================================================================
# 响应模型
# ==================================================================

class Evidence(BaseModel):
    """证据片段。★ 所有回答都必须能追溯到证据。"""

    type: str = Field(..., description="证据类型：database/retrieval/conflict")
    table: Optional[str] = None
    tables: Optional[list[str]] = None
    field: Optional[str] = None
    stand_id: Optional[str] = None
    # ★ M65：地点证据要能指出是哪个地点（否则「提到了谁」缺少主语）
    location_id: Optional[str] = None
    chunk_id: Optional[int] = None
    stand_name: Optional[str] = None
    part: Optional[int] = None
    chunk_type: Optional[str] = None
    section: Optional[str] = None
    content: Optional[str] = None
    source_url: Optional[str] = None
    note: Optional[str] = None
    # ★ M12：合并块溯源信息
    #   （本模型是白名单模式，未声明的字段会被 FastAPI 过滤掉——
    #     实测踩过：字段加了但响应里没有，就是这个原因）
    n_base_blocks: int = Field(1, description="本证据由几个原始块合并而来")
    base_chunk_ids: list[int] = Field(
        default_factory=list,
        description="★ 可回溯的原始 chunk_id 列表（合并块的来源）")
    merged: bool = Field(False, description="是否为合并块")
    n_chars: int = Field(0, description="证据字符数")


class QueryResponse(BaseModel):
    """查询响应。"""

    question: str
    route: str = Field(..., description="structured/semantic/hybrid/abstain")
    route_reason: str
    answer: Optional[Any] = Field(
        None, description="结构化答案（对象）或原文片段（字符串列表）")
    answer_type: str = Field(
        ..., description="fact/extreme/count/forms/owner/snippet/composite/none")
    evidence: list[Evidence] = []
    # ★ 语义执行器返回的 meta 是 list[dict]，这里统一成 dict
    retrieval_meta: Optional[dict] = None
    confidence: Optional[float] = Field(
        None, description="★ 冲突消解的置信度（若有冲突）")
    warning: Optional[str] = Field(
        None, description="★ 可靠性警告（如未消解冲突、数据缺失）")
    elapsed_ms: float
    # ★ M11：生成式信息（mode=generate 时才有）
    mode: str = Field("extract", description="extract / generate")
    generation: Optional[dict] = Field(
        None, description="生成层详情：模型 / token / 延迟")
    faithfulness: Optional[dict] = Field(
        None, description="★ 机械核对的忠实度（不依赖 LLM 判分）")
    # ★ M15：模糊候选（未精确匹配到替身名时给出的提示，不代替答案）
    suggestions: Optional[list] = Field(
        None, description="模糊候选：「你是不是想问 X？」")
    # ★★ M17：可视化（缺失维度在 radar_svg 里已标注「无数据」，不填 0）
    radar_svg: Optional[str] = Field(
        None, description="六维雷达图 SVG（问属性/替身时返回）")
    # ★★ M25：多轮追问的指代消解信息 ★★
    #   发生了替换时才有值 —— 前端据此显示
    #   「「它」→ Star Platinum」，让用户看见系统怎么理解代词的。
    coref: Optional[dict] = Field(
        None, description="指代消解：{pronoun, resolved_to, from_question}")
    # ★★ M32：中文摘要卡 ★★
    #   从**结构化数据**拼出（中文名/日文原名/使用者/分部/六维中文等级），
    #   用于缓解「语料全是英文、答案看不动」的观感。
    #   ★ 不替换英文原文 —— 证据链必须保持可追溯。
    zh_card: Optional[dict] = Field(
        None, description=("中文摘要：{name_zh, name_zh_variants, name_zh_level, "
                           "name_ja, owner, part, stats_cn}"))
    stand_images: Optional[dict] = Field(
        None, description="替身图片：{stand,user,manga,anime} 分组 + URL")
    # ★★ M58：形态链（ACT1→ACT2…）—— 母体与形态成员都能看到"我有几个阶段"
    forms: Optional[list] = Field(
        None, description=("形态链：[{form_id, name, label, type, stand_id, current}]"
                            "；stand_id 为该形态对应的替身条目（可直接点进去）"))
    # ★★ M65：地点档案卡 ★★
    #   用户要的「安杰罗岩 / 勒索之路 / 恶魔掌心」这类地点。
    #   ★ 关联实体只给**强证据**（地点条目正文里写到过谁）；
    #     弱证据只给数量 —— 泛地名（Morioh 173 条）全列出来反而误导。
    #   ★ 字段缺就留 None，前端照实写「源站未提供」，**不填 0**。
    location_card: Optional[dict] = Field(
        None, description=("地点档案：{location_id, name_zh, name_en, name_ja, "
                           "types, part, place_paths, mangadebuts, "
                           "stands, characters, weak_count, missing}"))


class HealthResponse(BaseModel):
    status: str
    database: str
    n_stands: int
    vector_enabled: bool
    resolution_stats: dict
    n_unresolved_conflicts: int
    # ★ M12：把索引配置暴露出来 —— 用户要能确认「当前用的是哪套索引」
    chunk_merge_target: Optional[int] = None
    n_chunks: int = 0
    # ★ M56：网页构建号 = index.html 的修改时间。
    #   ★ 必须写进模型 —— `response_model` 会**静默丢掉**未声明的字段，
    #     实测踩坑：health() 里 return 了 web_build，但接口上没有，
    #     页面拿不到值就无法自检"我看到的是不是最新页面"。
    web_build: Optional[str] = None


# ==================================================================
# 应用
# ==================================================================

app = FastAPI(
    title="fictional-lore-rag 混合检索 API",
    version="0.6.0",
    description=(
        "基于 jojowiki 替身数据的混合检索系统。\n\n"
        "**核心特性**\n"
        "- **抽取式回答**：返回数据源原文，零生成零幻觉\n"
        "- **可解释路由**：每次查询都说明走了哪条路径及原因\n"
        "- **证据可追溯**：每条回答都能定位到具体表/字段或文本块\n"
        "- **冲突已消解**：数值已应用 M4 的消解策略，冲突处会标出警告"
    ),
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------------------------------------------------------
STATIC_DIR = Path(__file__).resolve().parent / "static"


def _forms_of(cur, sid: str, zh_name: Optional[str] = None) -> list:
    """★ M58：取替身的形态链（ACT1 → ACT2 → …）。

    实测问题：Tusk / Echoes 都有多个阶段，但**界面上只显示一种** ——
      · 母体（echoes/tusk）在 stands.json 里**缺失**（只有派生形态），
        是入库时按聚合规则补建的；
      · 形态链只存在于 stand_forms 表，而前端**从来没读过它**。
    → 于是用户看到「Tusk 只有一份数据」，ACT1~ACT4 全部不可见。

    ★ 形态成员（xxx_actN）也查得到：去掉后缀取母体，把整条链返回，
      并标出「当前处于哪一段」。
    ★ 形态 → 替身条目的对应靠命名约定 `{base}__f{k}` ↔ `{base}_act{k+1}`
      （建链时就是这个约定）；对不上就不给 stand_id（前端只显示名称）。
    """
    if not sid:
        return []
    base = re.sub(r"_act\d+$", "", sid)
    cur.execute("""
        SELECT form_id, form_name, form_type
        FROM stand_forms WHERE stand_id=%s
        ORDER BY COALESCE(raw_order, 0), form_id
    """, (base,))
    rows = cur.fetchall()
    if not rows:
        return []
    out = []
    for fid, fname, ftype in rows:
        m = re.search(r"__f(\d+)$", fid or "")
        member = f"{base}_act{int(m.group(1)) + 1}" if m else None
        out.append({
            "form_id": fid,
            "name": fname,
            "label": (f"{zh_name} {fname}" if zh_name else fname),
            "type": ftype,
            "stand_id": member,
            "current": member == sid,
        })
    return out


def _web_build() -> str:
    """网页构建标记 = index.html 的修改时间（MM-DD HH:MM）。

    ★ 注意：这个函数**不能带路由装饰器**。
      实测踩坑：插入它时把原来 `@app.get("/")` 那行留在了它头上，
      于是 `/` 被它接管 —— 页面变成 13 字节的 JSON 字符串
      （`"10-14 18:17"`），整个网页白屏。而 FastAPI 不会报错，
      因为 "/" 注册两次时**先注册的那个生效**，`root()` 被静默忽略。
      → 新增函数时，装饰器必须紧贴它自己的函数定义。

    ★★ M56：为什么要把构建号**注入页面** ★★
      用户反复说「网页还是没区别」，而我这边 `curl /` 明明是新版 ——
      双方都无法判断是「浏览器缓存了旧页面」还是「他开的是另一个服务」。
      → 页面上直接印出这个数字，并且 `/health` 也返回同一个值：
          · 页面上的数字 == 服务器上的数字 → 看到的就是最新的
          · 数字偏旧  → 浏览器缓存了旧页面（页面自己会提示强制刷新）
          · 显示占位符 `__WEB_BUILD__` → 服务进程是旧的（不会做替换）
            → 页面提示「请重启 start.bat」
    """
    html = STATIC_DIR / "index.html"
    if not html.exists():
        return "无"
    return datetime.fromtimestamp(
        html.stat().st_mtime).strftime("%m-%d %H:%M")


@app.get("/", include_in_schema=False)
def root():
    """★ 网页界面（单文件 HTML，零依赖）。

    ★★ M50：必须显式禁缓存 ★★
      实测：改了 `index.html` 后，服务端返回的确实是新版
      （`curl / | grep 'table class="tight"'` 命中），
      但**浏览器仍显示旧页面** —— 因为 `FileResponse` 默认不带
      `Cache-Control`，浏览器按「启发式缓存」把页面缓存了。
      → 页面本身很小（~70KB），每次重新验证的代价可忽略，
        而"改了看不到"的代价很大（会让人以为改动没生效）。
      ★ 对比：图片走 `/images/...` 那条路**故意**设了
        `max-age=604800`（内容不变，缓存划算）——
        两者策略不同是有意的。

    ★★ M56：同时在响应体里替换构建标记 ★★
      缓存头只能约束"从这一刻起"的浏览器；对于**在加这个头之前**
      就已经缓存过的旧页面，浏览器可能一直不回来验证。
      注入构建号的好处是：即使看到的是缓存页，用户也能一眼看出"这是旧的"。
    """
    html = STATIC_DIR / "index.html"
    if html.exists():
        # ★★ 占位符在整份 HTML 里只出现一次（那个 <b> 里），这里也只换第一次 ★★
        #   实测踩坑：原来是 `replace(占位符, 构建号)`（**全局**），
        #   结果把 JS 里的 `mine === '<占位符>'` 也一起改写了 ——
        #   于是页面把「正确的构建号」判成「没被替换」，
        #   自己误报"旧版本"。文本替换是**不看上下文**的，必须限制范围。
        txt = html.read_text(encoding="utf-8").replace(
            "__WEB_BUILD__", _web_build(), 1)
        #   ★ 这里**不做断言** —— 请求路径上断言会让首页 500，
        #     代价远大于"多出一个占位符"。由 test_m56 守住"只允许一处"。
        return Response(
            txt, media_type="text/html; charset=utf-8",
            headers={"Cache-Control": "no-cache, must-revalidate"})
    return {
        "service": "fictional-lore-rag 混合检索 API",
        "version": "0.6.0",
        "docs": "/docs",
        "health": "/health",
        "main_endpoint": "POST /query",
    }


def _get_generator():
    """★ 延迟加载生成器（首次调用时才加载模型）。

    实测踩坑（M6 → M11）：如果服务启动时就new Generator()，
    每个用户都得多等 20 秒 + 占 3GB 显存——而默认的抽取式根本不用模型。
    """
    gen = STATE.get("generator")
    if gen is not None:
        return gen
    if STATE.get("generator_error"):
        return None
    try:
        from generator import Generator
        gen = Generator(lazy=True)
        STATE["generator"] = gen
        return gen
    except Exception as e:
        STATE["generator_error"] = f"{type(e).__name__}: {e}"
        print(f"[api] 生成器不可用：{STATE['generator_error']}")
        return None


def _score_faithfulness(answer_text: str, evidence_texts: list[str]) -> dict | None:
    """★ 对生成结果做机械核对的忠实度自评。

    不依赖 LLM 判分（M6 的结论：1.5B 做评审员不可靠），
    只做机械核对：答案里的断言能否在证据里找到。
    """
    if not answer_text or not evidence_texts:
        return None
    try:
        sys.path.insert(0, str(ROOT / "evaluation"))
        from faithfulness import aggregate, score_one
        sc = score_one(answer_text, evidence_texts,
                       STATE.get("known", set()))
        agg = aggregate([sc])
        d = sc.to_dict()
        return {
            "trace_ratio": agg["trace_ratio"],
            "numeric_ratio": agg["numeric_ratio"],
            "contradiction_rate": agg["contradiction_rate"],
            "util_ratio": agg["util_ratio"],
            "hedging": d.get("hedging"),
            "n_unsupported": d.get("n_unsupported", 0),
        }
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _try_reconnect() -> bool:
    """★ 尝试重连数据库并重新初始化依赖它的组件。

    实测踩坑：服务在 Docker 未就绪时启动 → 降级，
    之后即使 DB 起来了也**永远不会恢复**，
    用户只能重启服务才知道「其实 DB 早就好了」。
    → health 每次调用都试一次重连，代价极小（一次 TCP 连接测试）。
    """
    if STATE.get("db_ok"):
        return True
    try:
        conn = psycopg2.connect(**PG)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SELECT stand_id, name_en, part_name_en, owner_name_raw "
                    "FROM stands")
        rows = cur.fetchall()
        cur.close()

        STATE["conn"] = conn
        from executor import StructuredExecutor
        STATE["structured"] = StructuredExecutor(conn)
        STATE["db_ok"] = True
        print("[api] ✓ 数据库已恢复，结构化查询重新启用")
        return True
    except Exception:
        return False


@app.get("/health", response_model=HealthResponse,
         summary="健康检查与系统状态")
def health():
    # ★ 先尝试重连（DB 可能已恢复但服务还在降级态）
    #   实测踩坑：插入位置错了，把 health 的签名覆盖掉，
    #   导致 _try_reconnect 的返回值 True 被当成响应体 → 500
    _try_reconnect()
    conn = STATE.get("conn")
    db_ok = False
    n = 0
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("SELECT count(*) FROM stands")
            n = cur.fetchone()[0]
            cur.close()
            db_ok = True
        except Exception:
            db_ok = False
    return {
        "status": "ok" if db_ok else "degraded",
        "database": "connected" if db_ok else "disconnected",
        "n_stands": n,
        "vector_enabled": STATE.get("vector_ok", False),
        "resolution_stats": STATE.get("resolutions", {}),
        "n_unresolved_conflicts": STATE.get("n_unresolved", -1),
        # ★ M12：把索引配置暴露出来（用户要能确认「当前用的是哪套索引」）
        "chunk_merge_target": STATE.get("chunk_merge_target"),
        "n_chunks": len(STATE.get("semantic")._chunks)
                     if STATE.get("semantic") else 0,
        # ★ M56：网页构建号（= index.html 的 mtime）。页面拿它自检
        #   「你看到的是不是最新页面」，见 _web_build() 的说明
        "web_build": _web_build(),
    }


@app.post("/query", response_model=QueryResponse,
          summary="★ 主接口：查询替身数据")
def query(
    q: str = Query(..., min_length=1, max_length=500,
                   description="自然语言问题，如「Star Platinum 的破坏力是几级？」"),
    top_k: int = Query(5, ge=1, le=20, description="语义检索返回条数"),
    mode: str = Query("extract", pattern="^(extract|generate)$",
                      description=("extract=抽取式（默认，零生成零幻觉）/ "
                                  "generate=生成式（Qwen2.5-1.5B，忠实度约束）")),
    session_id: Optional[str] = Query(
        None, max_length=64,
        description="★ M25：多轮会话标识。带上它才能解析「那它的速度呢」这类追问"),
):
    """统一查询入口。内部按路由分流。

    ★★ M25：多轮追问（指代消解）★★
      在路由**之前**把「它/这个替身/他」换成上一轮实体，
      得到 resolved_q 往下走 —— 对下游执行器完全透明。
      替换信息放在响应的 `coref` 字段里返回，让用户看得见。
    """
    t0 = time.time()
    # ★ DB 不可用时的降级标记（必须在函数体里，
    #   放进 Pydantic 模型会报「需要类型注解」——实测踩过）
    db_unavailable = False
    # ★ M11：生成式字段（mode=generate 时填充）
    generation: dict | None = None
    faithfulness: dict | None = None
    # ★★ M17：雷达图与图片（认得出替身时才填）
    radar_svg_out: Optional[str] = None
    stand_imgs: Optional[dict] = None
    # ★★ M32：中文摘要卡（认得出替身时填）—— 缓解「答案全是英文」的观感
    zh_card: Optional[dict] = None
    # ★★ M65：地点档案卡（route=location 时填）
    location_card: Optional[dict] = None

    # ---------- M25：指代消解（必须最早做）----------
    # ★ 时机很关键：必须在 router.route() 之前。
    #   放后面的话，路由已经按「它」这个含糊问句判过意图了。
    coref_info: Optional[dict] = None
    resolved_q = q
    _prev_ctx = None
    _store = STATE.get("sessions")
    if _store is not None:
        try:
            from coref import resolve as _resolve
            resolved_q, coref_info, _prev_ctx = _resolve(
                q, session_id, _store,
                guess_stand=lambda s: _guess_stand(s, STATE),
                guess_char=lambda s: _guess_char(s, STATE),
            )
        except Exception:  # noqa: BLE001
            resolved_q, coref_info = q, None

    router = STATE["router"]
    decision = router.route(resolved_q)
    route = decision.route

    evidence: list[dict] = []
    answer: Any = None
    answer_type = "none"
    retrieval_meta: dict = {}
    warning: Optional[str] = None
    confidence: Optional[float] = None

    # ★★ M15：模糊候选（只在**没有精确匹配**时给，且只作提示）
    #   评测结论（evaluation/eval_fuzzy_alias.py）：
    #     阈值 2 → 正例 100% 但负例误匹配 11.8%（不可用）
    #     阈值 3 → 负例 0% 但正例仅 44%
    #   → 任何阈值都做不到「既好用又安全」
    #   → 所以**不让它决定答案**，只提示「你是不是想问 X」
    fuzzy_suggestions: list[dict] = []
    if STATE.get("id2alias") and _guess_stand(resolved_q, STATE) is None:
        try:
            from aliases import suggest_stands
            fuzzy_suggestions = suggest_stands(resolved_q, STATE["id2alias"])
        except Exception:
            fuzzy_suggestions = []

    # ---------- abstain ----------
    if route == "abstain":
        answer = {
            "abstained": True,
            "reason": decision.reason,
            "hint": "该问题涉及的数据集中不存在。数据来源为 jojowiki 替身条目，"
                    "不含未公布设定或衍生作品内容。",
        }
        answer_type = "abstain"
        evidence.append({"type": "note",
                         "note": f"路由判定：{decision.reason}"})

    # ---------- location（M65）----------
    #   ★ 读完 JSON 就能答，**不依赖数据库** —— 与语义检索一致，
    #     所以降级模式下地点问题仍然可用。
    elif route == "location":
        try:
            from locations import answer as _loc_answer
            _ans, _ev, location_card = _loc_answer(resolved_q, top_k=top_k)
            answer = _ans
            answer_type = _ans.get("type", "location_fact")
            evidence.extend(_ev)
            evidence.append({
                "type": "note",
                "note": "地点数据来自 jojowiki 的 {{Location Infobox}}"
                        "（113 个正传地点；归属层级取 place 字段原文）",
            })
        except Exception as e:  # noqa: BLE001
            answer = {"note": f"地点模块出错：{type(e).__name__}: {e}"}
            answer_type = "none"
            location_card = None

    # ---------- structured ----------
    elif route in ("structured", "hybrid"):
        if STATE["structured"] is None:
            # ★ DB 不可用：明确告知，而不是抛 500
            #   实测踩坑：原先把 else 块写歪了，result/ev 作用域错乱 → 500
            answer = {
                "note": "结构化查询需要数据库，当前数据库不可用。",
                "hint": "启动方式：cd docker && docker compose up -d postgres",
            }
            answer_type = "none"
            # ★ 注意：route_reason 不是局部变量！
            #   它来自 decision.reason，只在返回 dict 时用（第 396 行）。
            #   之前写成 `route_reason += ...` → UnboundLocalError（实测踩过）：
            #   Python 在编译期把它判定为局部变量，读时却从未赋值。
            #   正确做法：加一个单独的降级说明字段。
            db_unavailable = True
        else:
            result, ev = STATE["structured"].execute(resolved_q, STATE["name2id"])
            evidence.extend(ev)
            if result is None:
                # 路由说是结构化但解析不出意图 → 降级到语义
                # ★★★ M16：同样必须按实体锚定。
                #   实测：「小面孔的能力」「紫烟破音有什么效果」
                #   这两句既不命中 STRUCT_FACT 也不命中 SEMANTIC（「能力」「效果」
                #   两个词两个正则都没收录）→ 落到默认 structured →
                #   execute() 解析不出意图 → 降级到这里的语义检索。
                #   ★ 旧代码只传 boost_stand，BM25 主体仍是全库 →
                #     又返回Strength/Hermit Purple（完全无关的替身）。
                _sid = _guess_stand(resolved_q, STATE)
                _pinned = (STATE["semantic"].stand_chunks(_sid, top_k=top_k)
                           if _sid else [])
                if _pinned:
                    snippets, meta = _pinned, {"mode": "entity_pinned",
                                               "pinned_stand": _sid}
                else:
                    snippets, meta = STATE["semantic"].search(
                        resolved_q, top_k=top_k, boost_stand=_sid)
                if snippets:
                    answer = [s["content"] for s in snippets]
                    answer_type = "snippet"
                    evidence.extend(_to_evidence(snippets))
                    retrieval_meta = (_meta_to_dict(meta)
                                      if not isinstance(meta, dict) or
                                      "mode" not in meta
                                      else meta)
                else:
                    answer = {"note": "未能解析该问题"}
                    answer_type = "none"
            else:
                answer = result
                answer_type = result.get("type", "structured")
                # 冲突/可靠性信息
                warning, confidence = _extract_warning(result)

                # hybrid：补语义描述
                if route == "hybrid":
                    sid = result.get("stand_id") or _guess_stand(resolved_q, STATE)
                    extra = STATE["semantic"].keyword_snippets(resolved_q, sid) \
                        if sid else []
                    if extra:
                        answer = {**result, "description_snippets": extra}
                        evidence.append({
                            "type": "retrieval",
                            "stand_id": sid,
                            "note": "混合路由补充的原文描述",
                        })

    # ---------- semantic ----------
    else:
        sid = _guess_stand(resolved_q, STATE)
        # ★★★ M16：`entity_semantic` —— 问句已锚定实体时**必须按实体取原文**，
        #   不能只用 BM25 boost。
        #   实测：走boost 语义检索时，「骇游天外的能力是什么」
        #        → BM25 主体仍命中Strength（语料里只有 name_en、无中文）
        #        → 返回完全无关的替身。
        #   → 锚定实体的描述类问题，一律先keyword_snippets(sid) 硬过滤；
        #     不足 top_k 时再补一次全库检索。
        if route == "entity_semantic" and sid \
                and STATE["semantic"] is not None:
            snippets = STATE["semantic"].stand_chunks(sid, top_k=top_k)
            if snippets:
                retrieval_meta = {"mode": "entity_pinned",
                                  "pinned_stand": sid}
            else:
                snippets, meta = STATE["semantic"].search(
                    resolved_q, top_k=top_k, boost_stand=sid)
                retrieval_meta = _meta_to_dict(meta)
        else:
            snippets, meta = STATE["semantic"].search(
                resolved_q, top_k=top_k, boost_stand=sid)
            retrieval_meta = _meta_to_dict(meta)
        if snippets:
            ev_texts = [s["content"] for s in snippets]
            # ★ M11：生成式分支。抽取式保持默认，行为完全不变。
            if mode == "generate":
                gen = _get_generator()
                if gen is None:
                    answer = {
                        "note": "生成模型不可用，已回退到抽取式。",
                        "error": STATE.get("generator_error"),
                        "hint": "先跑 fetch_model.py 下载 Qwen2.5-1.5B",
                    }
                    answer_type = "snippet"
                    warning = (warning or "") + "；生成模型不可用，已回退抽取式"
                else:
                    t_gen = time.time()
                    gen.ensure_loaded()
                    if gen.model is None:
                        answer = {
                            "note": "生成模型加载失败，已回退到抽取式。",
                            "error": gen.info().get("error"),
                        }
                        answer_type = "snippet"
                        warning = ((warning or "")
                                   + "；生成模型加载失败，已回退抽取式")
                    else:
                        res = gen.generate(resolved_q, ev_texts)
                        answer = res.text
                        answer_type = "generated"
                        generation = {
                            "model": gen.info().get("model", ""),
                            "device": gen.info().get("device"),
                            "temperature": gen.temperature,
                            "n_prompt_tokens": res.n_prompt_tokens,
                            "n_gen_tokens": res.n_gen_tokens,
                            "elapsed_ms": round(res.elapsed_ms, 1),
                            "load_ms": round((time.time() - t_gen) * 1000, 1),
                        }
                        # ★ 机械核对的忠实度 —— 让用户看到这次回答有多可信
                        faithfulness = _score_faithfulness(
                            res.text, ev_texts)
            else:
                answer = [s["content"] for s in snippets]
                answer_type = "snippet"
            evidence.extend(_to_evidence(snippets))
        else:
            answer = {"note": "未检索到相关内容"}
            answer_type = "none"

    # ★★★ M17：附上雷达图与图片 ★★★
    # 触发条件：路由认出了替身（answer 里有 stand_id，或能猜出来），
    #   且**没有拒答**。abstain 时不该配图 —— 那是无关问题。
    # ★ 图片可能走网络抓取（首次 5-15 秒），所以：
    #   - 只在认得出实体时做
    #   - 严格限时，失败就返回空列表，不影响主答案
    # ★★ M58b：sid 必须在这里就初始化 ★★
    #   它原来只在 `if route != "abstain" and _images_enabled():` 里面赋值
    #   （见下方媒体块），而**形态链那段代码在 if 之外**读它 ——
    #   实测：问句被判 abstain（或禁用图片）时，那段 if 整块跳过 →
    #   `if sid and ...` 直接 UnboundLocalError → 整个请求 **500**。
    #   前端只显示「无法连接到服务」，把用户引向了错误方向（服务其实活着）。
    sid = None
    if route != "abstain" and _images_enabled():
        if isinstance(answer, dict):
            sid = answer.get("stand_id")
            # ★★ 修复：stand_of / stands_of_owner 的替身名在 **stands 列表**里，
            #   顶层没有 stand_id。
            #   实测症状：问「东方常秀的替身」→ 答出 Nut King Call，
            #   但**没有图片、没有雷达、没有替身档案卡**（用户反馈
            #   「这个替身咋没图片呢」）。
            #   根因：这里只取顶层 stand_id → None → 回退到 _guess_stand(问句)，
            #   而「东方常秀的替身」句子里根本没有替身名 → 猜不到 → 整块媒体跳过。
            #   ★ 讽刺的是，本文件下方的**指代消解**早就做了这个兜底
            #     （注释原话：「必须**先看答案**：stand_of 的替身名在
            #     answer["stands"] 里」），媒体分支漏了同一件事。
            if not sid:
                _st = answer.get("stands")
                if isinstance(_st, list) and _st and isinstance(_st[0], dict):
                    sid = _st[0].get("stand_id")
        if not sid:
            sid = _guess_stand(resolved_q, STATE)
        if sid and STATE.get("conn"):
            try:
                c2 = STATE["conn"].cursor()
                c2.execute("""SELECT s.name_en, s.owner_name_raw,
                                  st.pwr,st.spd,st.rng,st.sta,st.prc,st.dev,
                                  st.missing_count, s.part, s.stand_type,
                                  s.owner_id,
                                  -- ★ M53：雷达图要区分「无限」与「无数据」，
                                  --   必须带上 raw（∞）与 cat（INFINITE）
                                  st.pwr_raw,st.spd_raw,st.rng_raw,
                                  st.sta_raw,st.prc_raw,st.dev_raw,
                                  st.pwr_cat,st.spd_cat,st.rng_cat,
                                  st.sta_cat,st.prc_cat,st.dev_cat
                           FROM stands s
                           LEFT JOIN stand_stats st ON st.stand_id=s.stand_id
                           WHERE s.stand_id=%s""", (sid,))
                row = c2.fetchone()
                c2.close()
                if row:
                    _dk = ("pwr", "spd", "rng", "sta", "prc", "dev")
                    _srow = {k: row[2 + i] for i, k in enumerate(_dk)}
                    _srow.update({f"{k}_raw": row[12 + i]
                                  for i, k in enumerate(_dk)})
                    _srow.update({f"{k}_cat": row[18 + i]
                                  for i, k in enumerate(_dk)})
                    rp = radar_payload(_srow, missing_count=row[8] or 0)
                    zh_rec = (STATE.get("zh_map") or {}).get(sid) or {}
                    radar_svg_out = radar_svg(
                        rp["stats"], missing=rp["missing"],
                        specials=rp["specials"],
                        title=f"{zh_rec.get('name_zh') or row[0]} 六维能力")

                    # ★★ M32：中文摘要卡 ★★
                    #   用户反馈「答案返回一堆英文介绍看不懂」——
                    #   根因是语料全部来自 jojowiki 英文站（2407/2407 块英文）。
                    #   做法不是机翻（会破坏「证据可追溯」），而是
                    #   **用本来就有的中文数据**拼一张摘要卡：
                    #     中文名 / 日文原名 / 使用者 / 分部 / 六维中文等级
                    #   英文原文仍保留（可折叠），保证证据链不被污染。
                    try:
                        from executor import DIM_CN, LEVEL_CN
                        # ★ DIM_CN 的键是**大写**（"PWR"），而 row 里按
                        #   小写顺序排 —— 必须 .lower() 再 index，
                        #   否则 ValueError（被下面的 except 吞掉，
                        #   表现成 zh_card 静默为 None）。
                        _order = ["pwr", "spd", "rng", "sta", "prc", "dev"]
                        _sp = rp.get("specials") or {}
                        _lv = {}
                        for _k, _cn in DIM_CN.items():
                            _key = _k.lower()
                            if _key in _sp:
                                # ★ M53：无限维度 → 显示原始值（∞）。
                                #   以前走下面那个分支得到 None，前端一律
                                #   渲染成「无数据」——把"无限"说成"没有数据"。
                                _lv[_cn] = _sp[_key]
                                continue
                            _v = rp["stats"].get(_key)
                            _lv[_cn] = (LEVEL_CN.get(int(_v))
                                        if isinstance(_v, (int, float))
                                        else None)
                        _alias = (STATE.get("id2alias") or {}).get(sid) or []
                        _ja = next((a for a in _alias
                                    if re.search(r"[\u3040-\u30ff]", a)), None)
                        # ★ M33：使用者也要中文名 —— 原来只转了替身名，
                        #   卡片上还挂着 "Jotaro Kujo"（用户当场指出）。
                        #   角色中文名来自 load_char_zh_map（130 个角色）。
                        _own_en = (row[1] or "").split(",")[0].strip()
                        _own_list = (STATE.get("char_zh_map") or {}).get(row[11])
                        _own_zh = (_own_list or [None])[0] if _own_list else None
                        # ★★ M39：异译别名要**显示出来** ★★
                        #   实测用户反馈：输入「娃娃脸」能查到 Baby Face，
                        #   但卡片第一行写「婴儿面孔」—— 用户输入与屏幕对不上，
                        #   于是怀疑「是不是机翻」。
                        #   根因不是数据缺（别名早就在 name_zh_variants 里），
                        #   而是**从没被展示过** → 这里透传给前端。
                        _zh_main = zh_rec.get("name_zh")
                        _zh_vars = [v for v in (zh_rec.get("name_zh_variants") or [])
                                    if v and v != _zh_main]
                        zh_card = {
                            "stand_id": sid,
                            "name_zh": _zh_main,
                            "name_zh_variants": _zh_vars,
                            # ★ 可信级别：A=官方 / B=双源一致 / C=单源 / D=反推
                            #   让用户能分辨「哪条译名是有据可查的」
                            "name_zh_level": zh_rec.get("level"),
                            "name_zh_source": zh_rec.get("source"),
                            "name_en": row[0],
                            "name_ja": _ja,
                            "owner": _own_zh or _own_en or None,
                            "owner_en": _own_en if _own_zh else None,
                            "part": row[9],
                            "stats_cn": _lv,
                            "missing": rp["missing"],
                            "type": _clean_stand_type(row[10]),
                        }
                    except Exception:  # noqa: BLE001
                        zh_card = None

                    # ★ M18：异步取图 —— 没缓存就起后台任务、立即返回 pending
                    stand_imgs = image_payload(
                        row[0], sid, (row[1] or "").split(",")[0].strip())
            except Exception:
                # ★ 图片/雷达图是增强项，出错绝不能影响主答案
                radar_svg_out = radar_svg_out or None

    # ---------- M25：把本轮的实体记进会话（供下一轮指代消解）----------
    # ★ 必须放在所有 exit 路径之前，否则「回答失败的那一轮」不留上下文，
    #   用户紧接着追问就断链了。
    #
    # ★★ 实体来源的优先级（M25 实测踩坑）★★
    #   第 1 轮「空条承太郎的替身是什么」的问句里**没有替身名**
    #   （只有角色名），所以只从问句抽取会得到 stand_name=None →
    #   下一轮「那它的速度呢」的「它」只能退到角色名，答成
    #   「空条承太郎的速度」→ 检索到无关内容（实测就是这个 bug）。
    #   → 必须**先看答案**：stand_of 的替身名在 answer["stands"] 里。
    if _store is not None and session_id:
        try:
            from coref import build_context
            _sid_new = _sname_new = None
            _cid_new = _cname_new = None
            if isinstance(answer, dict):
                _sid_new = answer.get("stand_id")
                _sname_new = answer.get("stand_name")
                # stand_of / stands_of_owner：替身在 stands 列表里
                _st = answer.get("stands")
                if isinstance(_st, list) and _st and isinstance(_st[0], dict):
                    _f = _st[0]
                    _sid_new = _sid_new or _f.get("stand_id")
                    # ★ 优先中文名（用户问的是中文，回填中文更连贯）
                    _sname_new = (_sname_new or _f.get("zh")
                                  or _f.get("name"))
                _sname_new = _sname_new or answer.get("zh")
                # 角色：stand_of 的 owner 字段
                if answer.get("type") in ("stand_of", "stands_of_owner"):
                    _cname_new = answer.get("owner")
            # 问句兜底（答案给不出时）
            if not _sid_new:
                _sid_new = _guess_stand(resolved_q, STATE)
            if not _sname_new:
                _sname_new = _display_name_of(resolved_q, STATE) or _sid_new
            if not _cid_new:
                _cid_new = _guess_char(resolved_q, STATE)
            if not _cname_new:
                _cname_new = _display_char_name(resolved_q, STATE) or _cid_new
            _store.put(session_id, build_context(
                # ★ 存**用户原话**（不是 resolved_q）—— 它要回显给用户看
                question=q,
                stand_id=_sid_new, stand_name=_sname_new,
                char_id=_cid_new, char_name=_cname_new,
                prev=_prev_ctx,
            ))
        except Exception:  # noqa: BLE001
            pass

    # ★★ M58：形态链 —— 让「Tusk 有 ACT1~ACT4、Echoes 有 ACT1~ACT3」在界面上可见
    _forms_out = None
    if sid and STATE.get("conn"):
        try:
            _c = STATE["conn"].cursor()
            # ★★ M58b：标签用**母体**的名字，不是当前替身的 ★★
            #   否则点进某个形态后，标签会变成「獠牙ACT1 ACT2」，越点越长，
            #   最后拼出「獠牙ACT1 ACT4」这种查不到的句子（实测会 500）。
            _base = re.sub(r"_act\d+$", "", sid)
            _zhm = (STATE.get("zh_map") or {}).get(_base) or {}
            _forms_out = _forms_of(_c, sid, _zhm.get("name_zh")) or None
            _c.close()
        except Exception:  # noqa: BLE001
            _forms_out = None

    return {
        "question": q,
        "route": route,
        "route_reason": decision.reason
                      + ("（但数据库不可用，无法执行 SQL）"
                         if db_unavailable else ""),
        "answer": answer,
        "answer_type": answer_type,
        "evidence": [Evidence(**e) for e in evidence if _valid_evidence(e)],
        "retrieval_meta": retrieval_meta or None,
        "confidence": confidence,
        "warning": warning,
        "elapsed_ms": round((time.time() - t0) * 1000, 1),
        "mode": mode,
        "generation": generation,
        "faithfulness": faithfulness,
        # ★ M15：模糊候选（★ 只提示，不代替答案）
        "suggestions": fuzzy_suggestions or None,
        # ★★ M17：六维雷达图 + 替身图片
        "radar_svg": radar_svg_out,
        "stand_images": stand_imgs,
        # ★ M58：形态链（母体与形态成员都能看到自己的阶段）
        "forms": _forms_out,
        # ★★ M32：中文摘要卡（名称/使用者/分部/六维中文等级）
        #   —— 与英文原文并列，不替换它（证据链保持可追溯）
        "zh_card": zh_card,
        # ★★ M65：地点档案卡（route=location 时才有）
        "location_card": location_card,
        # ★★ M25：指代消解信息（发生替换时才有）
        "coref": coref_info,
    }


# ------------------------------------------------------------------
@app.get("/similar/{stand_id}", summary="找六维最接近的替身（M32）")
def get_similar(stand_id: str,
                k: int = Query(5, ge=1, le=10, description="返回条数")):
    """按**六维数值**找相似替身。

    ★ 为什么不用向量索引：它需要 7 GB 模型，而且编码的是
      「描述文本」的语义，不是「能力数值」—— 描述风格相近但
      能力迥异的替身可能被判为相似。六维距离更贴题且可解释。

    ★ 缺失维度**不按 0 计**：只比较两边都有值的维度，
      共同维度少于 3 个时不参与比较，并在 note 里说明排除了多少个。
      不足时明确返回「数据不足」，**不硬给排名**。
    """
    if not STATE.get("db_ok"):
        return {"error": "数据库不可用",
                "hint": "cd docker && docker compose up -d postgres"}
    try:
        from similarity import similar_stands
        return similar_stands(STATE["conn"], stand_id, k=k)
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


# ===================================================================
# M37：出场记录 / 必杀技 / 替身来源
# ===================================================================
# ★ 这三块数据本来就在磁盘上，只是没建表。现在落库了。
# ★ 每个接口都在返回值里**明写覆盖率** ——
#   必杀技只有 10 个替身有数据，不说清楚会被当成全库都有。


@app.get("/appearances/{stand_id}", summary="查替身出场记录（M37）")
def get_appearances(
        stand_id: str,
        kind: str = Query("manga",
                          pattern="^(manga|anime|cover|all)$"),
        limit: int = Query(60, ge=1, le=500)):
    """出场章节 / 动画集数。

    ★ **kind 必须分清**，这直接决定「第一次出场是第几话」是否可信：
        manga = 漫画里实际有戏份的章节
        anime = 动画集数
        cover = 出现在封面 / 仅被提及 —— **不等于有戏份**
      混在一起的话，白金之星的「首次出场」会答成第 1 话（那是封面）。

    ★ chapter_no 解析不出的行是 NULL（按「无数据」处理，不是 0）。
    """
    if not STATE.get("db_ok"):
        return {"error": "数据库不可用"}
    cur = STATE["conn"].cursor()
    try:
        cur.execute("SELECT name_en FROM stands WHERE stand_id=%s", (stand_id,))
        row = cur.fetchone()
        if not row:
            return {"error": f"库里没有替身 {stand_id}"}

        sql = """
            SELECT kind, chapter_no, episode_no, chapter_title, raw_text
            FROM stand_appearances WHERE stand_id=%s
        """
        params: list = [stand_id]
        if kind != "all":
            sql += " AND kind=%s"
            params.append(kind)
        # 排序：先按编号，NULL 排最后（而不是当 0 排最前）
        sql += (" ORDER BY (chapter_no IS NULL), chapter_no NULLS LAST,"
                "         episode_no NULLS LAST, raw_text LIMIT %s")
        params.append(limit)
        cur.execute(sql, params)
        rows = cur.fetchall()

        # 各 kind 的计数（即使 kind 过滤了也给全量计数，便于前端提示）
        cur.execute("""
            SELECT kind, count(*), min(chapter_no)
            FROM stand_appearances WHERE stand_id=%s GROUP BY kind
        """, (stand_id,))
        summary = {k: {"n": n, "first_chapter": first}
                   for k, n, first in cur.fetchall()}

        # ★ 首次出场**只认 manga**：封面不是出场
        first_manga = summary.get("manga", {}).get("first_chapter")
        cover_ch = summary.get("cover", {}).get("first_chapter")
        note = None
        if cover_ch and first_manga and cover_ch < first_manga:
            note = (f"注意：该替身在第 {cover_ch} 话的封面/提及中出现过，"
                    f"但那是 cover 类，不代表有戏份。"
                    f"实际首次出场是第 {first_manga} 话")

        return {
            "stand_id": stand_id, "name": row[0],
            "kind_filtered": kind,
            "count": len(rows),
            "summary_by_kind": summary,
            "first_real_appearance": (
                {"chapter": first_manga, "source": "manga"}
                if first_manga is not None else None),
            "note": note,
            "appearances": [
                {"kind": k, "chapter": ch, "episode": ep,
                 "title": ti, "raw": raw}
                for k, ch, ep, ti, raw in rows],
        }
    finally:
        cur.close()


@app.get("/moves/{stand_id}", summary="查替身必杀技（M37）")
def get_moves(stand_id: str):
    """必杀技列表。

    ★★ 覆盖极低：全库 19 条、只覆盖 10 个替身。★★
      返回里带 `coverage_note`，前端要显示出来 ——
      否则用户会以为「只有白金之星有必杀技」。
    """
    if not STATE.get("db_ok"):
        return {"error": "数据库不可用"}
    cur = STATE["conn"].cursor()
    try:
        cur.execute("SELECT name_en FROM stands WHERE stand_id=%s", (stand_id,))
        row = cur.fetchone()
        if not row:
            return {"error": f"库里没有替身 {stand_id}"}
        cur.execute("""
            SELECT name, phonetic, alias, debut_chapter, debut_raw
            FROM stand_moves WHERE stand_id=%s
            ORDER BY (debut_chapter IS NULL), debut_chapter, name
        """, (stand_id,))
        rows = cur.fetchall()
        cur.execute("SELECT count(DISTINCT stand_id) FROM stand_moves")
        n_with = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM stands")
        n_all = cur.fetchone()[0]
        return {
            "stand_id": stand_id, "name": row[0], "count": len(rows),
            "moves": [
                {"name": n, "phonetic": ph, "alias": al,
                 "debut_chapter": dc, "debut_raw": dr}
                for n, ph, al, dc, dr in rows],
            "coverage_note": (
                f"★ 数据覆盖有限：全库 {n_all} 个替身里只有 {n_with} 个"
                f"有必杀技记录。没有列出不代表该替身没有必杀技。"),
        }
    finally:
        cur.close()


@app.get("/origins", summary="查替身来源 / 血缘（M37）")
def get_origins(
        kind: str = Query("all",
                          pattern="^(all|arrow|exposure|natural|bloodline"
                                  "|saint_corpse|merge|technique|unknown)$"),
        has_note: bool = Query(False,
                               description="只看带来源人名的（如「箭的来源: DIO」）")):
    """替身来源。

    ★ 填充 44%（65/148）。`kind=unknown` 的行是
      **「有原始串但没归类出来」，不是「无来源」** —— 二者不可混同。
    """
    if not STATE.get("db_ok"):
        return {"error": "数据库不可用"}
    cur = STATE["conn"].cursor()
    try:
        sql = """
            SELECT o.stand_id, s.name_en, s.name_ja, o.origin_kind,
                   o.origin_raw, o.origin_note
            FROM stand_origins o JOIN stands s USING(stand_id)
        """
        where: list[str] = []
        params: list = []
        if kind != "all":
            where.append("o.origin_kind=%s")
            params.append(kind)
        if has_note:
            where.append("o.origin_note IS NOT NULL")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY o.origin_kind, s.name_en"
        cur.execute(sql, params)
        rows = cur.fetchall()
        cur.execute("SELECT origin_kind, n_stands FROM v_origin_summary")
        dist = {k: n for k, n in cur.fetchall()}
        cur.execute("SELECT count(*) FROM stands")
        n_all = cur.fetchone()[0]
        return {
            "kind_filtered": kind,
            "count": len(rows),
            "distribution": dist,
            "coverage_note": (
                f"★ 仅 {sum(dist.values())}/{n_all} 个替身有来源数据。"
                f"未出现在这里的是「源数据没有 origin 字段」，不是「无来源」。"),
            "origins": [
                {"stand_id": sid, "name": nm, "name_ja": ja,
                 "kind": k, "raw": raw, "note": note}
                for sid, nm, ja, k, raw, note in rows],
        }
    finally:
        cur.close()


@app.get("/chapters/{n}", summary="查某话出现了哪些替身（M37）")
def get_chapter(n: int, limit: int = Query(40, ge=1, le=200)):
    """反向查询：第 N 话里出现了哪些替身。

    ★ 只统计 kind='manga'（实际出场），cover/提及不算。
    """
    if not STATE.get("db_ok"):
        return {"error": "数据库不可用"}
    cur = STATE["conn"].cursor()
    try:
        cur.execute("""
            SELECT s.stand_id, s.name_en, s.name_ja, s.part, a.chapter_title
            FROM stand_appearances a JOIN stands s USING(stand_id)
            WHERE a.chapter_no=%s AND a.kind='manga'
            ORDER BY s.name_en LIMIT %s
        """, (n, limit))
        rows = cur.fetchall()
        cur.execute("""
            SELECT count(*) FROM stand_appearances
            WHERE chapter_no=%s AND kind='manga'
        """, (n,))
        total = cur.fetchone()[0]
        return {
            "chapter": n, "count": total,
            "returned": len(rows),
            "note": None if total <= limit
                    else f"共 {total} 个，此处只返回前 {limit} 个",
            "stands": [
                {"stand_id": r[0], "name": r[1], "name_ja": r[2],
                 "part": r[3], "title": r[4]} for r in rows],
        }
    finally:
        cur.close()


@app.get("/stands/{stand_id}", summary="查单个替身的完整信息")
def get_stand(stand_id: str):
    """返回替身的全部结构化信息 + 冲突状态 + 文本块数。"""
    if not STATE.get("db_ok"):
        return {"error": "数据库不可用",
                "hint": "cd docker && docker compose up -d postgres"}
    conn = STATE["conn"]
    cur = conn.cursor()
    cur.execute("""
        SELECT s.stand_id, s.name_en, s.name_ja, s.part, s.part_name_en,
               s.owner_name_raw, s.stand_type, s.reference,
               s.form_count, st.pwr, st.spd, st.rng, st.sta, st.prc, st.dev,
               st.composite, st.missing_count,
               -- ★ M49：原始字面量与类别。前端「六维能力值」表有
               --   「原始」「类别」两列，但接口此前**没带这两个字段** →
               --   前端读 st['PWR_raw'] / st['PWR_cat'] 恒为 undefined，
               --   于是「原始」永远显示「—」、「类别」永远空白。
               --   （DB 里本来就有这些列，只是没 SELECT 出来。）
               st.pwr_raw, st.spd_raw, st.rng_raw,
               st.sta_raw, st.prc_raw, st.dev_raw,
               st.pwr_cat, st.spd_cat, st.rng_cat,
               st.sta_cat, st.prc_cat, st.dev_cat
        FROM stands s LEFT JOIN stand_stats st ON st.stand_id = s.stand_id
        WHERE s.stand_id = %s
    """, (stand_id,))
    r = cur.fetchone()
    if not r:
        raise HTTPException(404, f"{stand_id} 不存在")

    dims = ["PWR", "SPD", "RNG", "STA", "PRC", "DEV"]
    level_cn = {0: "无", 1: "E", 2: "D", 3: "C", 4: "B", 5: "A"}
    cur.execute("""
        SELECT stat_dim, value_a, source_a, value_b, source_b,
               conflict_type, resolution, resolved_value, note
        FROM stat_conflicts WHERE stand_id = %s
    """, (stand_id,))
    conflicts = [dict(zip(["dim", "value_a", "source_a", "value_b", "source_b",
                           "type", "resolution", "resolved_value", "note"],
                          row)) for row in cur.fetchall()]
    cur.execute("SELECT count(*) FROM text_chunks WHERE stand_id = %s",
                (stand_id,))
    n_chunks = cur.fetchone()[0]
    cur.close()

    # ★★ M16：把中文名挂到接口上
    #   DB 的 stands 表没有中文名列（中文名在 aliases 的词典里），
    #   → 在这里查一次词典补上，GUI 才能显示「骇游天外 / Sky High」。
    #   同时带出异译别名（软又湿 / 柔软且湿润），便于核对。
    zh_rec = (STATE.get("zh_map") or {}).get(stand_id) or {}
    name_zh = zh_rec.get("name_zh")
    zh_variants = [v for v in (zh_rec.get("name_zh_variants") or [])
                   if v and v != name_zh]

    # ★★★ M17：六维雷达图（SVG 内联，免图片请求）
    #   缺失维度留空 + 标注「无数据」，**不填 0**（填 0 会被误读成"能力为零"）
    _dk = ("pwr", "spd", "rng", "sta", "prc", "dev")
    stat_row = {k: r[9 + i] for i, k in enumerate(_dk)}
    # ★ M53：raw 在 r[17..22]、cat 在 r[23..28]（列顺序见上面 SELECT 的注释）
    stat_row.update({f"{k}_raw": r[17 + i] for i, k in enumerate(_dk)})
    stat_row.update({f"{k}_cat": r[23 + i] for i, k in enumerate(_dk)})
    rp = radar_payload(stat_row, missing_count=r[16] or 0)
    radar = radar_svg(
        rp["stats"], missing=rp["missing"], specials=rp["specials"],
        title=f"{name_zh or r[1]} 六维能力")

    # ★★★ M17/M18：图片（**异步**，不阻塞响应）
    #   没缓存时只起后台任务就返回 pending，前端轮询 /images/status/{id}。
    #   绝不阻塞等抓取完成 —— 实测阻塞会让请求变成 87 秒。
    imgs: dict[str, Any] = {}
    if _images_enabled():
        owner = (r[5] or "").split(",")[0].strip()
        imgs = image_payload(r[1], stand_id, owner)

    return {
        "stand_id": r[0], "name_en": r[1], "name_ja": r[2],
        # ---- M16 新增：中文名与异译 ----
        "name_zh": name_zh,
        "name_zh_variants": zh_variants,
        # ---- M17 新增：雷达图与图片 ----
        "radar_svg": radar,
        "stats_detail": rp,
        "images": imgs,
        "part": r[3], "part_name": r[4],
        "owner_raw": r[5], "stand_type": r[6], "reference": r[7],
        "form_count": r[8],
        # ★ M49：每维补 raw（原始字面量，如 'A'/'∞'/'?'）与 category。
        #   索引：r[0..8] 固定字段 → r[9..14] pwr..dev → r[15] composite
        #        → r[16] missing → r[17..22] *_raw → r[23..28] *_cat
        "stats": {d: {"value": r[9 + i],
                      # ★ M53：INFINITE 的 value 是 NULL，但**它是有值的**（∞）。
                      #   返回 None 会让「值」列显示「—」，而同一行
                      #   「原始」是 ∞、「类别」是无限 —— 自相矛盾。
                      "label": ("∞" if r[23 + i] == "INFINITE"
                                else level_cn.get(r[9 + i])),
                      "raw": r[17 + i],
                      "category": r[23 + i]}
                  for i, d in enumerate(dims)},
        "composite": r[15], "missing_count": r[16],
        "n_text_chunks": n_chunks,
        "conflicts": conflicts,
    }


# ==================================================================
# M17：图片服务
# ==================================================================
def _images_enabled() -> bool:
    """是否启用图片（可用 --no-images 关闭）。

    ★ 图片抓取要走代理、有网络延迟（首次约 5-15秒/替身），
      所以提供开关：离线演示或只看文字时关掉更快。
    ★ 从 app.state 读（命令行 --no-images 写在那里），
      读不到时默认开启 —— 图片是增强功能，不该因为没配就消失。
    """
    if "images_enabled" in STATE:
        return bool(STATE["images_enabled"])
    return bool(getattr(app.state, "images_enabled", True))


@app.get("/images/status/{stand_id}",
         summary="M18：查图片抓取状态（前端轮询用）")
def images_status(stand_id: str):
    """返回图片抓取状态：idle / pending / running / done / failed。

    ★ 为什么需要这个端点 ★★
    M18 之前图片抓取是同步阻塞在 /query 里，实测没缓存的替身要 **87 秒**，
    用户看到的是「一直转圈，再点一下就出答案」。
    现在 /query 立即返回，前端拿 answer，再轮询本端点，
    图就绪后渲染 —— 回答不再被图片拖住。

    ★★★ 路由顺序坑（M18 实测踩到）★★★
      这个端点**必须定义在 `/images/{stand_id}/{filename}` 之前**。
      FastAPI 按**定义顺序**匹配、先定义者先赢：
      状态端点写在后面时，`/images/status/c_moon` 会命中图片路由，
      被解析成 stand_id="status"、filename="c_moon" → 400，
      前端轮询永远拿不到状态（实测 state 一直返回 None）。
    """
    st = image_status(stand_id)
    if st.get("state") == "done":
        out = public_images(st["manifest"])
        out["state"] = "done"
        return out
    return {"stand_id": stand_id, "state": st.get("state", "idle"),
            "total": 0,
            "error": st.get("error"),
            "elapsed": st.get("elapsed")}


@app.get("/images/{stand_id}/{filename}",
         summary="本地图片（M17：fetch_stand_images.py 抓取后存此）")
def serve_image(stand_id: str, filename: str):
    """返回本地缓存的图片文件。

    ★ 安全：必须挡掉路径穿越（../）。
      stand_id 与 filename 都只允许字母数字下划线连字符，
      校验不过直接 400 —— 不要用 (IMG_DIR / stand_id / filename).resolve()
      那种写法，否则 ../ 就能读到项目外的文件。
    """
    safe = re.compile(r"^[A-Za-z0-9_\-.]+$")
    if not safe.match(stand_id) or not safe.match(filename):
        raise HTTPException(400, "非法路径")
    p = IMG_DIR / stand_id / filename
    if not p.exists() or not p.is_file():
        raise HTTPException(404, "图片不存在")
    if filename == "_images.json":
        raise HTTPException(400, "清单文件不作为图片提供")
    mime = {".png": "image/png", ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg", ".gif": "image/gif",
            ".webp": "image/webp"}.get(Path(filename).suffix.lower())
    if not mime:
        raise HTTPException(400, "不支持的图片格式")
    # ★ 缓存：图片内容不可变（文件名带序号，抓一次就不再变）
    return FileResponse(p, media_type=mime,
                        headers={"Cache-Control": "public, max-age=604800"})


@app.get("/conflicts", summary="查冲突（支持按状态过滤）")
def get_conflicts(
    status: str = Query("all", pattern="^(all|pending|resolved)$"),
    limit: int = Query(50, ge=1, le=500),
):
    """返回冲突记录。pending = 未定值（含刻意的 keep_unknown）。

    ★★ M22：补上「消解理由 / 置信度 / 敏感性」★★
      rationale / confidence / sensitivity 来自 resolution_log，
      之前只在库里躺着、界面上完全没出现 —— 而这三样正是
      「多源交叉验证」最有说服力的证据。

    ★★ JOIN 必须按 (stand_id, stat_dim) 取**最新一条**日志 ★★
      实测 resolution_log 有 44 条，而 stat_conflicts 只有 28 条：
      同一个 (替身, 维度) 可能有多条日志（消解过程的变更记录，
      如 boy_ii_man/RNG 有 2 条）。
      直接 JOIN 会让冲突行**重复**、理由**串到别的维度**。
      → 用 DISTINCT ON 先取每个组合的最新一条。

    ★ 字段名保持向后兼容（仍返回 dim / value_a / value_b / type），
      新增字段一律追加，避免破坏既有调用方。
      另补 dim_cn（维度中文名），省得前端再维护一份映射。
    """
    if not STATE.get("db_ok"):
        return {"count": 0, "conflicts": [],
                "error": "数据库不可用",
                "hint": "cd docker && docker compose up -d postgres"}
    conn = STATE["conn"]
    cur = conn.cursor()
    sql = """
        SELECT c.stand_id, s.name_en, s.part, c.stat_dim,
               c.value_a, c.source_a, c.value_b, c.source_b,
               c.conflict_type, c.resolution, c.resolved_value, c.note,
               r.rationale, r.confidence, r.sensitivity, r.strategy
        FROM stat_conflicts c
        JOIN stands s ON s.stand_id = c.stand_id
        LEFT JOIN (
            SELECT DISTINCT ON (stand_id, stat_dim)
                   stand_id, stat_dim, rationale, confidence,
                   sensitivity, strategy, logged_at
            FROM resolution_log
            ORDER BY stand_id, stat_dim, logged_at DESC NULLS LAST
        ) r ON r.stand_id = c.stand_id AND r.stat_dim = c.stat_dim
        WHERE 1=1
    """
    params: list = []
    if status == "pending":
        sql += "AND c.resolved_value IS NULL "
    elif status == "resolved":
        sql += "AND c.resolved_value IS NOT NULL "
    sql += "ORDER BY c.resolution NULLS FIRST, s.part NULLS LAST, c.stand_id LIMIT %s"
    params.append(limit)
    cur.execute(sql, params)
    cols = ["stand_id", "stand_name", "part", "dim", "value_a", "source_a",
            "value_b", "source_b", "type", "resolution", "resolved_value",
            "note",
            # M22
            "rationale", "confidence", "sensitivity", "logged_strategy"]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    cur.close()
    # 维度中文名（复用执行器的映射，避免前端再维护一份）
    try:
        from executor import DIM_CN, LEVEL_CN
    except Exception:  # noqa: BLE001
        DIM_CN, LEVEL_CN = {}, {}
    for r in rows:
        r["dim_cn"] = DIM_CN.get(r.get("dim") or "", r.get("dim") or "")
        # 有理由 = 该条在 resolution_log 里有记录
        r["has_rationale"] = bool(r.get("rationale"))
        # ★ M22：resolved_value 是**数字等级**（1..5），而 value_a/value_b 是
        #   **字母**（E/D/C/B/A）→ 同一张卡左右不一致，用户看不懂「采纳 3」。
        #   → 补一个字母形态给前端用（非 1..5 的值原样返回，不硬转）。
        rv = r.get("resolved_value")
        if isinstance(rv, int) and rv in LEVEL_CN:
            r["resolved_label"] = LEVEL_CN[rv]
        else:
            r["resolved_label"] = rv
    return {"count": len(rows), "status": status, "conflicts": rows}


@app.get("/stats", summary="系统统计")
def get_stats():
    """数据完整性与分布概览。"""
    out: dict[str, Any] = {}
    if not STATE.get("db_ok"):
        return {"error": "数据库不可用", "degraded_mode": True,
                "hint": "cd docker && docker compose up -d postgres"}
    conn = STATE["conn"]
    cur = conn.cursor()

    cur.execute("SELECT * FROM v_data_quality")
    out["data_quality"] = {r[0]: {"n_rows": r[1], "detail": r[2]}
                           for r in cur.fetchall()}
    cur.execute("""
        SELECT pwr_cat, count(*) FROM stand_stats
        GROUP BY 1 ORDER BY 2 DESC
    """)
    out["pwr_category_dist"] = {r[0]: r[1] for r in cur.fetchall()}
    cur.execute("SELECT part, count(*) FROM stands "
                "WHERE part IS NOT NULL GROUP BY 1 ORDER BY 1")
    out["part_dist"] = {f"第{r[0]}部": r[1] for r in cur.fetchall()}
    cur.execute("SELECT chunk_type, count(*) FROM text_chunks "
                "GROUP BY 1 ORDER BY 2 DESC")
    out["chunk_type_dist"] = {r[0]: r[1] for r in cur.fetchall()}
    cur.execute("SELECT strategy, count(*) FROM resolution_log "
                "GROUP BY 1")
    out["resolution_log"] = {r[0]: r[1] for r in cur.fetchall()}
    cur.close()
    out["retrieval"] = {
        "vector_enabled": STATE.get("vector_ok", False),
        "mode": "BM25 + bge-m3 + RRF(k=30)" if STATE.get("vector_ok")
                else "仅 BM25（k1=1.2, b=0.5）",
    }
    return out


# ------------------------------------------------------------------
# 辅助
# ------------------------------------------------------------------

# ★★ M32：替身类型清洗 ★★
#   stands.stand_type 是从 infobox 多项**直接拼接**来的，实测值形如：
#     "Close-RangeRange Irrelevant (Star Platinum: The World)ReconnaissanceNatural Humanoid"
#   （没有分隔符、还夹着括号注记）—— 直接显示没法看。
#   → 按已知类型词提取、译成中文、用「·」连接；识别不出就返回 None。
_STAND_TYPE_CN: tuple[tuple[str, str], ...] = (
    ("Close-Range", "近距离型"), ("Long-Range", "远距离型"),
    ("Range Irrelevant", "射程无关"), ("Reconnaissance", "侦查型"),
    ("Natural Humanoid", "自然人型"), ("Artificial Humanoid", "人造人型"),
    ("Automatic", "自动操纵型"), ("Colony", "群体型"), ("Bound", "依附型"),
    ("Integrated", "一体型"), ("Wearable", "穿戴型"), ("Tool", "道具型"),
    ("Materialized", "物质化型"), ("Phenomenon", "现象型"),
    ("Spirit", "精神型"), ("Psychic", "超能力型"),
)
_TYPE_NOTE_RE = re.compile(r"\([^)]*\)")


def _clean_stand_type(raw: Optional[str]) -> Optional[str]:
    """把拼接的类型串清成「近距离型 · 自然人型」；识别不出返回 None。"""
    if not raw:
        return None
    t = _TYPE_NOTE_RE.sub(" ", str(raw))     # 去掉括号里的注记
    hits: list[str] = []
    for en, cn in _STAND_TYPE_CN:
        if en.lower() in t.lower() and cn not in hits:
            hits.append(cn)
    return " · ".join(hits) if hits else None


def _display_name_of(q: str, state: dict) -> Optional[str]:
    """返回问句里出现的**替身写法本身**（不是 id）。

    ★ 为什么要它：回填时代词应换成用户原本的写法 ——
      用户打「白金之星」，下一轮显示「白金之星的速度」比
      「Star Platinum 的速度」自然得多。
    """
    name2id = state.get("name2id", {})
    for name in sorted(name2id, key=len, reverse=True):
        if name and name.lower() in q.lower():
            return name
    return None


def _display_char_name(q: str, state: dict) -> Optional[str]:
    """返回问句里出现的**角色写法本身**。"""
    c2i = state.get("char2id") or {}
    ql = q.lower()
    for name in sorted(c2i, key=len, reverse=True):
        if name and name.lower() in ql:
            return name
    return None


def _guess_char(q: str, state: dict) -> Optional[str]:
    """猜问句里的**角色** id（M25，用于指代消解）。

    ★ 与 _guess_stand 同理：长名优先，否则「常秀」会抢在「东方常秀」前面命中。
    ★ 中文名与英文名都在同一张表里（初始化时已合并）。
    """
    c2i = state.get("char2id") or {}
    if not c2i:
        return None
    ql = q.lower()
    for name in sorted(c2i, key=len, reverse=True):
        if name and name.lower() in ql:
            return c2i[name]
    return None


def _guess_stand(q: str, state: dict) -> Optional[str]:
    """猜问句里的替身 id（★ M14：走全量别名表）。

    ★ 长名优先：避免 "Tusk" 抢走 "Tusk ACT1"。
    ★ 中文名也认：见 STATE["name2id"] 的构建（aliases.build_alias_table）。
    """
    name2id = state.get("name2id", {})
    for name in sorted(name2id, key=len, reverse=True):
        if name and name.lower() in q.lower():
            return name2id[name]
    # ★ 兜底：用别名的简繁通配再试一次
    #   （用户可能混用简繁，如「黄金体験」/「黄金体验」）
    try:
        from aliases import simplify_zh
        q2 = simplify_zh(q)
        if q2 != q:
            for name in sorted(name2id, key=len, reverse=True):
                if name and simplify_zh(name).lower() in q2.lower():
                    return name2id[name]
    except Exception:
        pass
    return None


def _meta_to_dict(meta) -> Optional[dict]:
    """语义执行器返回 list[dict]，统一成 dict。

    ★ 踩坑：Pydantic 声明是 Optional[dict]，直接塞 list 会
      ValidationError → HTTP 500。本地调用正常但 HTTP 层失败，
      根因是「本地没做模型校验」。
    """
    if not meta:
        return None
    if isinstance(meta, dict):
        return meta
    if isinstance(meta, list) and meta:
        # 多个元素时合并成一个 dict
        out: dict = {}
        for m in meta:
            if isinstance(m, dict):
                out.update(m)
        return out or None
    return None


# Evidence 模型的字段类型（用于入参校验）
_EVIDENCE_STR_FIELDS = ("type", "table", "field", "stand_id", "location_id",
                        "chunk_type", "section", "content", "source_url", "note")


def _valid_evidence(e: dict) -> bool:
    """★ 过滤掉不合法的 evidence。

    实测踩坑（本项目第三次在Evidence 白名单模型上栽）：
      1. M13：`n_base_blocks` 没在模型里声明 → 响应里看不到
      2. M14a：`note` 塞了 dict → Pydantic ValidationError → **500**
      3. M14b：`_valid_evidence` 只查 `type` 存在，
                类型不匹配的一路漏到 Pydantic 才炸

    → 这里**在建Evidence 之前**就把类型不对的挡掉，
      最多丢一条证据，不会让整个请求 500。
    """
    if not isinstance(e, dict) or not e.get("type"):
        return False
    # ★ 类型预检：字符串字段里塞了非字符串 → 直接拒
    for k in _EVIDENCE_STR_FIELDS:
        v = e.get(k)
        if v is not None and not isinstance(v, str):
            return False
    # 数值字段
    for k in ("part", "chunk_id", "n_base_blocks", "n_chars"):
        v = e.get(k)
        if v is not None and not isinstance(v, int):
            return False
    # 列表字段
    for k in ("tables", "base_chunk_ids"):
        v = e.get(k)
        if v is not None and not isinstance(v, list):
            return False
    return True


def _to_evidence(snippets: list[dict]) -> list[dict]:
    return [{"type": "retrieval", **{k: v for k, v in s.items()
                                     if k != "retrieval_score"}}
            for s in snippets]


def _extract_warning(result: dict) -> tuple[Optional[str], Optional[float]]:
    """从结构化结果里提取可靠性警告。"""
    warnings: list[str] = []
    # composite 缺失
    if result.get("type") in ("extreme", "count_by_level"):
        pass
    if result.get("missing_count", 0) and result.get("type") == "forms":
        warnings.append("该替身有维度缺失，composite 不可用")
    # 异常类别
    if result.get("category") in ("UNKNOWN", "EMPTY_SLOT", "UNPARSED"):
        warnings.append(
            f"该维度数据异常（{result.get('category_cn')}），"
            "数值为不可用状态，请参考 raw 原文")
    if not warnings:
        return None, None
    return "；".join(warnings), None


# ------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="fictional-lore-rag API 服务")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--reload", action="store_true", help="开发模式热重载")
    ap.add_argument("--no-vector", action="store_true",
                    help="不加载向量模型（纯 BM25，启动快）")
    # ★ M17
    ap.add_argument("--no-images", action="store_true",
                    help="不返回替身图片（图片首次抓取需联网，默认开启）")
    ap.add_argument("--log-level", default="info")
    ap.add_argument("--chunk-merge", type=int, default=None,
                    metavar="N",
                    help=("★ M12：把语义块合并到约 N 字符。"
                          "512=M9 实测推荐（空洞率 0.50→0.30，"
                          "代价是延迟 +52%%）。默认不合并。"))
    args = ap.parse_args()

    import uvicorn
    app.state.use_vector = not args.no_vector
    app.state.chunk_merge_target = args.chunk_merge
    # ★ M17：--no-images 关闭图片（抓图要走代理，首次有网络延迟）
    app.state.images_enabled = not args.no_images

    print("=" * 62)
    print("fictional-lore-rag 混合检索 API")
    print("=" * 62)
    print(f"  模式: {'BM25 + bge-m3' if not args.no_vector else '仅 BM25'}")
    print(f"  索引: {f'语义块合并到 {args.chunk_merge} 字符' if args.chunk_merge else '原始语义块（2407）'}")
    print(f"  地址: http://{args.host}:{args.port}")
    print(f"  文档: http://{args.host}:{args.port}/docs")
    print("=" * 62)

    uvicorn.run(
        "api.main:app" if args.reload else app,
        host=args.host, port=args.port,
        reload=args.reload, log_level=args.log_level,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

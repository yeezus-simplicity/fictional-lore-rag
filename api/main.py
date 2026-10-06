"""
rag-kb API 服务（M5）。

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
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
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

# ==================================================================
# 全局状态
# ==================================================================

STATE: dict[str, Any] = {}


def _init_state(use_vector: bool = True) -> None:
    """启动时初始化：数据库 + 路由器 + 执行器。"""
    t0 = time.time()

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
    known_entities.discard(None)
    known_entities.discard("")

    # --- 路由器（M4）---
    from conflict_resolver import Router
    STATE["router"] = Router(known_stands=known_stands,
                             known_entities=known_entities)
    STATE["known"] = known_stands
    # name → id 映射（_guess_stand 用）
    STATE["name2id"] = {s["name_en"]: s["stand_id"]
                        for s in stands if s.get("name_en")}

    # --- 结构化执行器（★ 需要 DB）---
    STATE["structured"] = None
    if db_ok:
        from executor import StructuredExecutor
        STATE["structured"] = StructuredExecutor(STATE["conn"])

    # --- 语义执行器（M3 检索层）---
    from semantic_executor import SemanticExecutor
    sem = SemanticExecutor(use_vector=use_vector, verbose=True)
    t_sem = time.time()
    vector_ok = sem.load()
    STATE["semantic"] = sem
    STATE["vector_ok"] = vector_ok

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
    #   app.state.use_vector 未设置 → 用 getattr 兜底
    use_vector = getattr(app.state, "use_vector", True)
    _init_state(use_vector=use_vector)
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
    chunk_id: Optional[int] = None
    stand_name: Optional[str] = None
    part: Optional[int] = None
    chunk_type: Optional[str] = None
    section: Optional[str] = None
    content: Optional[str] = None
    source_url: Optional[str] = None
    note: Optional[str] = None


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


class HealthResponse(BaseModel):
    status: str
    database: str
    n_stands: int
    vector_enabled: bool
    resolution_stats: dict
    n_unresolved_conflicts: int


# ==================================================================
# 应用
# ==================================================================

app = FastAPI(
    title="rag-kb 混合检索 API",
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


@app.get("/", include_in_schema=False)
def root():
    """★ 网页界面（单文件 HTML，零依赖）。"""
    html = STATIC_DIR / "index.html"
    if html.exists():
        return FileResponse(html)
    return {
        "service": "rag-kb 混合检索 API",
        "version": "0.6.0",
        "docs": "/docs",
        "health": "/health",
        "main_endpoint": "POST /query",
    }


@app.get("/health", response_model=HealthResponse,
         summary="健康检查与系统状态")
def health():
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
    }


@app.post("/query", response_model=QueryResponse,
          summary="★ 主接口：查询替身数据")
def query(
    q: str = Query(..., min_length=1, max_length=500,
                   description="自然语言问题，如「Star Platinum 的破坏力是几级？」"),
    top_k: int = Query(5, ge=1, le=20, description="语义检索返回条数"),
):
    """统一查询入口。内部按路由分流。"""
    t0 = time.time()
    # ★ DB 不可用时的降级标记（必须在函数体里，
    #   放进 Pydantic 模型会报「需要类型注解」——实测踩过）
    db_unavailable = False
    router = STATE["router"]
    decision = router.route(q)
    route = decision.route

    evidence: list[dict] = []
    answer: Any = None
    answer_type = "none"
    retrieval_meta: dict = {}
    warning: Optional[str] = None
    confidence: Optional[float] = None

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
            result, ev = STATE["structured"].execute(q, STATE["name2id"])
            evidence.extend(ev)
            if result is None:
                # 路由说是结构化但解析不出意图 → 降级到语义
                snippets, meta = STATE["semantic"].search(
                    q, top_k=top_k, boost_stand=_guess_stand(q, STATE))
                if snippets:
                    answer = [s["content"] for s in snippets]
                    answer_type = "snippet"
                    evidence.extend(_to_evidence(snippets))
                    retrieval_meta = _meta_to_dict(meta)
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
                    sid = result.get("stand_id") or _guess_stand(q, STATE)
                    extra = STATE["semantic"].keyword_snippets(q, sid) \
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
        sid = _guess_stand(q, STATE)
        snippets, meta = STATE["semantic"].search(
            q, top_k=top_k, boost_stand=sid)
        retrieval_meta = _meta_to_dict(meta)
        if snippets:
            answer = [s["content"] for s in snippets]
            answer_type = "snippet"
            evidence.extend(_to_evidence(snippets))
        else:
            answer = {"note": "未检索到相关内容"}
            answer_type = "none"

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
    }


# ------------------------------------------------------------------
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
               st.composite, st.missing_count
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

    return {
        "stand_id": r[0], "name_en": r[1], "name_ja": r[2],
        "part": r[3], "part_name": r[4],
        "owner_raw": r[5], "stand_type": r[6], "reference": r[7],
        "form_count": r[8],
        "stats": {d: {"value": r[9 + i],
                      "label": level_cn.get(r[9 + i])}
                  for i, d in enumerate(dims)},
        "composite": r[15], "missing_count": r[16],
        "n_text_chunks": n_chunks,
        "conflicts": conflicts,
    }


@app.get("/conflicts", summary="查冲突（支持按状态过滤）")
def get_conflicts(
    status: str = Query("all", pattern="^(all|pending|resolved)$"),
    limit: int = Query(50, ge=1, le=500),
):
    """返回冲突记录。pending = 未定值（含刻意的 keep_unknown）。"""
    if not STATE.get("db_ok"):
        return {"count": 0, "conflicts": [],
                "error": "数据库不可用",
                "hint": "cd docker && docker compose up -d postgres"}
    conn = STATE["conn"]
    cur = conn.cursor()
    sql = """
        SELECT c.stand_id, s.name_en, s.part, c.stat_dim,
               c.value_a, c.source_a, c.value_b, c.source_b,
               c.conflict_type, c.resolution, c.resolved_value, c.note
        FROM stat_conflicts c JOIN stands s ON s.stand_id = c.stand_id
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
            "note"]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    cur.close()
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

def _guess_stand(q: str, state: dict) -> Optional[str]:
    """猜问句里的替身 id。

    ★ 长名优先：避免 "Tusk" 抢走 "Tusk ACT1"。
    """
    name2id = state.get("name2id", {})
    for name in sorted(name2id, key=len, reverse=True):
        if name and name.lower() in q.lower():
            return name2id[name]
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


def _valid_evidence(e: dict) -> bool:
    """过滤掉缺必填字段的 evidence。"""
    return isinstance(e, dict) and bool(e.get("type"))


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
    ap = argparse.ArgumentParser(description="rag-kb API 服务")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--reload", action="store_true", help="开发模式热重载")
    ap.add_argument("--no-vector", action="store_true",
                    help="不加载向量模型（纯 BM25，启动快）")
    ap.add_argument("--log-level", default="info")
    args = ap.parse_args()

    import uvicorn
    app.state.use_vector = not args.no_vector

    print("=" * 62)
    print("rag-kb 混合检索 API")
    print("=" * 62)
    print(f"  模式: {'BM25 + bge-m3' if not args.no_vector else '仅 BM25'}")
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

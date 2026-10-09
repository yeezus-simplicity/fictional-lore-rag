"""替身图片的后台抓取任务（M18，替代 M17 的同步方案）。

==★ 为什么必须改成后台异步 ★★

M17 把图片抓取做成了**同步阻塞**在 /query 里，结果实测：
    已缓存的替身      0.06 秒  ✓
    没缓存的替身    87.48 秒  ✗← 前端一直转圈
用户反馈「等很长一段时间还在检索，重新点一下就出答案了」——
原因就在这里：第一次请求花 87 秒把图抓完并写进本地，
第二次走缓存就秒回。体验上就是「卡死 → 再点一下好了」。

★ 更糟的是：图片是**增强项**，却成了回答的最大延迟来源。
  → 正确架构（业界通用做法）：
    回答**立即返回**，图片走**后台任务**，
    前端拿到答案后单独轮询图片状态，图就绪后渲染出来。

本模块提供：
  start_task(stand_en, stand_id, owner)  —— 后台起抓取（不阻塞）
  status(stand_id)                       —— 查状态 idle/pending/running/done/failed
  ensure_async(...)                     —— 有缓存给清单，没有就起后台任务
  image_payload(...)                     —— 给 API 的便捷入口

==★ M18 顺带修的并行化 ★★
实测单张 400px thumb 经代理要 2.8 秒 → 串行 10 张 = 28 秒，
叠加角色页与失败重试后整体 87 秒。
→ fetch_stand_images.save 已改为 4 线程并行，同批约 7-8 秒。
"""
from __future__ import annotations

import concurrent.futures
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
IMG_DIR = ROOT / "images"
SRC_DIR = ROOT / "dataset" / "sources" / "images"

# ★ images/ 在 .gitignore 里（体积大 + 有版权），但前端会请求 /images/...
#   → 兜底创建，避免"必须先手动跑一次抓图"才能看页面。
try:
    IMG_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    pass

# 同一个替身只允许一个抓取任务在跑（避免重复点击起多个）
_TASKS: dict[str, dict] = {}
_LOCK = threading.Lock()

# 兜底池：8 个线程够用（并发抓多个替身时不会把代理压垮）
_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=8)


def manifest_path(stand_id: str) -> Path:
    return IMG_DIR / stand_id / "_images.json"


def has_cache(stand_id: str) -> bool:
    return manifest_path(stand_id).exists()


def _ensure_src_on_path() -> None:
    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))


def read_manifest(stand_id: str) -> Optional[dict]:
    if not has_cache(stand_id):
        return None
    try:
        return json.loads(manifest_path(stand_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _run(stand_en: str, stand_id: str, owner: str) -> None:
    """真正执行抓取（跑在后台线程里，不阻塞任何 HTTP 请求）。"""
    _ensure_src_on_path()
    with _LOCK:
        # ★ 用 setdefault 而不是「if in」+ 赋值：
        #   之前是 `if stand_id in _TASKS: _TASKS[...]["state"]=...`，
        #   任务若没预置（直接调 _run）会在后面的 .update() 抛
        #   KeyError → 整段异常。setdefault 天然兜住。
        _TASKS.setdefault(stand_id, {
            "state": "pending", "stand_en": stand_en, "owner": owner,
            "started": time.time(),
        })["state"] = "running"
    t0 = time.time()
    try:
        import fetch_stand_images as F
        #★★ 双来源要各自兜底，不能一失败就全丢 ★★
        #   实测踩到：c_moon 的使者是 "F.F."，角色页 /F.F. 抓取会超时
        #   （外部网络偶发），旧写法 page_images_by_kind 直接抛异常 →
        #   连**替身本体的图也一起丢了**，整个 state=failed。
        #   → 改成：替身页失败才放弃；角色页失败只缺使者图，不影响本体。
        stand_pairs: list = []
        user_pairs: list = []
        err_parts: list[str] = []
        try:
            stand_pairs = F.page_images(stand_en, owner)
        except Exception as e:  # noqa: BLE001
            err_parts.append(f"替身页失败 {type(e).__name__}")
            stand_pairs = []
        try:
            # 只补角色页的使者图（复用上面已抓的替身页结果，不重复请求）
            user_pairs = F.owner_page_images(stand_en, owner)
        except Exception as e:  # noqa: BLE001
            err_parts.append(f"角色页失败 {type(e).__name__}")
            user_pairs = []

        items = [(F.classify(f, stand_en, owner), f, u)
                 for f, u in stand_pairs]
        items += [("user", f, u) for f, u in user_pairs]
        items = [x for x in items if x[0] != "noise"]
        if not items:
            with _LOCK:
                _TASKS[stand_id].update(
                    state="failed",
                    error="无可用图片" + ("（" + "；".join(err_parts) + "）"
                                          if err_parts else ""),
                    elapsed=round(time.time() - t0, 1))
            return
        F.save(stand_id, items)
        with _LOCK:
            _TASKS[stand_id].update(
                state="done", elapsed=round(time.time() - t0, 1))
    except Exception as e:  # noqa: BLE001
        with _LOCK:
            _TASKS[stand_id].update(
                state="failed", error=f"{type(e).__name__}: {e}",
                elapsed=round(time.time() - t0, 1))


def start_task(stand_en: str, stand_id: str, owner: str = "") -> dict:
    """起一个后台抓取任务，**立即返回**（不阻塞调用方）。"""
    with _LOCK:
        cur = _TASKS.get(stand_id)
        if cur and cur["state"] in ("pending", "running"):
            return {"state": cur["state"], "started": False}
        _TASKS[stand_id] = {
            "state": "pending", "stand_en": stand_en, "owner": owner,
            "started": time.time(),
        }
    _POOL.submit(_run, stand_en, stand_id, owner)
    return {"state": "pending", "started": True}


def status(stand_id: str) -> dict:
    """查抓取状态。完成时带清单。"""
    mf = read_manifest(stand_id)
    if mf is not None:
        return {"state": "done", "manifest": mf}
    with _LOCK:
        task = dict(_TASKS.get(stand_id) or {})
    if not task:
        return {"state": "idle"}
    return task


def ensure_async(stand_en: str, stand_id: str, owner: str = "") -> dict:
    """有缓存直接给清单；没有就起后台任务并立刻返回 pending。"""
    mf = read_manifest(stand_id)
    if mf is not None:
        return {"state": "done", "manifest": mf}
    start_task(stand_en, stand_id, owner)
    return {"state": "pending"}


def public_images(manifest: dict) -> dict:
    """把内部清单转成前端结构（含 URL）。"""
    sid = manifest.get("stand_id", "")
    out: dict[str, list[dict]] = {}
    for item in manifest.get("images", []):
        f = item.get("file")
        if not f:
            continue
        out.setdefault(item.get("kind", "misc"), []).append({
            "file": f,
            "url": f"/images/{sid}/{f}",
            "bytes": item.get("bytes"),
            "caption": item.get("source_name", ""),
        })
    res: dict[str, Any] = {
        "stand_id": sid, "total": sum(len(v) for v in out.values())}
    res.update(out)
    if manifest.get("error"):
        res["error"] = manifest["error"]
    return res


def image_payload(stand_en: str, stand_id: str, owner: str = "") -> dict:
    """给 API 的便捷入口：能立刻给图就给图，否则返回 pending 让前端轮询。"""
    st = ensure_async(stand_en, stand_id, owner)
    if st["state"] == "done":
        out = public_images(st["manifest"])
        out["state"] = "done"
        return out
    return {"stand_id": stand_id, "total": 0, "state": "pending",
            "note": "图片正在后台抓取（约 10-30 秒），稍后自动出现"}


# ------------------------------------------------------------
# 雷达图：把 DB 的行组装成 radar_svg 需要的输入
# ------------------------------------------------------------
DIM_KEYS = ("pwr", "spd", "rng", "sta", "prc", "dev")


def radar_payload(row: dict, missing_count: int = 0) -> dict:
    """从 stand_stats 行算出 {stats, missing, values}。

    ★★★ 实测结论（勿再臆测）★★★
      stand_stats 里**没有任何 0 值**：pwr/spd/rng/sta/prc/dev 六列
      为 0 的行数都是 0，非空行有 135~149 条。
      有效等级区间是 **1..5**（1=E 2=D 3=C 4=B 5=A），
      缺失以**SQL NULL** 表达（对应 jojowiki 原文的 "?"/Unknown）。

      → 所以缺失判定**只看 IS NULL**，
        绝不能把 0 当成「无数据」或「能力为 0」——
        0 在这个数据集里根本不存在，凭空判它是缺维会错杀真实数据。
    """
    stats: dict[str, Optional[int]] = {}
    missing: list[str] = []
    vals: dict[str, Optional[int]] = {}
    for k in DIM_KEYS:
        v = row.get(k)
        if v is None:
            stats[k] = None
            vals[k] = None
            missing.append(k)
        else:
            iv = int(v)
            stats[k] = iv
            vals[k] = iv
    return {"stats": stats, "missing": missing, "values": vals,
            "missing_count": missing_count}
"""替身图片与雷达图服务（M17）。

==★ 设计要点 ==

1. **懒加载**：首次问某替身时才抓图（约 5-15 秒），之后走本地缓存。
   实测每个替身约 10 张、每张 3-5 秒 → 全量预抓 154 个要 1小时以上，
   而用户通常只关心少数几个 → 按需抓取是对的。

2. **抓取进程隔离**：抓图要走代理且可能卡 20 秒，
   ★ 绝不能阻塞 FastAPI 的事件循环 → 放线程池（run_in_executor）。

3. **降级链**：
   有本地缓存 → 用缓存
   没缓存 → 尝试抓取（限时）→ 成功则用，失败/超时 → 返回空列表 + 说明
   ★ 绝不因为图片抓不到就让整个查询失败（图片是增强，不是主功能）。

4. **雷达图缺失维度不填 0**：见 radar_chart.py 的说明。
"""
from __future__ import annotations

import concurrent.futures as _futures
import json
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
IMG_DIR = ROOT / "images"
SRC_DIR = ROOT / "dataset" / "sources" / "images"

# ★ images/ 在 .gitignore 里（体积大 + 有版权，靠 fetch_stand_images.py 重抓），
#   但服务启动后前端会请求 /images/... → 目录不存在会404。
#   → 这里兜底创建，避免"必须先手动跑一次抓图"才能看页面。
try:
    IMG_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    pass

# 抓取超时（秒）。实测单张3-5 秒、单个替身 10 张 → 给 90 秒够用
FETCH_TIMEOUT = 90


def _ensure_src_on_path() -> None:
    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))


def manifest_path(stand_id: str) -> Path:
    return IMG_DIR / stand_id / "_images.json"


def has_cache(stand_id: str) -> bool:
    return manifest_path(stand_id).exists()


def _fetch_sync(stand_en: str, stand_id: str, owner: str) -> bool:
    """同步抓取（跑在线程池里）。返回是否成功。

    ★ 走 page_images_by_kind：替身页取本体图+ **角色页取使者立绘**
      （实测替身条目页里通常没有使者图 —— Star_Platinum 页 52 张图
      含 Jotaro 的是 0 张，立绘都在角色页 /Jotaro_Kujo）。
    """
    _ensure_src_on_path()
    try:
        import fetch_stand_images as F
    except Exception:
        return False
    try:
        stand_pairs, user_pairs = F.page_images_by_kind(stand_en, owner)
        items = [(F.classify(f, stand_en, owner), f, u)
                 for f, u in stand_pairs]
        items += [("user", f, u) for f, u in user_pairs]
        items = [x for x in items if x[0] != "noise"]
        if not items:
            return False
        F.save(stand_id, items)
        return True
    except Exception:
        return False


def ensure_images(stand_en: str, stand_id: str, owner: str = "",
                  timeout: int = FETCH_TIMEOUT) -> dict:
    """确保某替身有图，返回清单 dict。

    ★ 这是给 API 调的入口：内部处理缓存命中 / 懒抓取 / 降级。
    """
    if has_cache(stand_id):
        try:
            return json.loads(manifest_path(stand_id).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass

    # 懒抓取：线程池 + 限时
    try:
        with _futures.ThreadPoolExecutor(max_workers=1) as ex:
            ex.submit(_fetch_sync, stand_en, stand_id, owner).result(timeout)
    except _futures.TimeoutError:
        return {"stand_id": stand_id, "images": [], "error": "抓取超时"}
    except Exception as e:  # noqa: BLE001
        return {"stand_id": stand_id, "images": [],
                "error": f"{type(e).__name__}: {e}"}

    if not has_cache(stand_id):
        return {"stand_id": stand_id, "images": [], "error": "无可用图片"}
    try:
        return json.loads(manifest_path(stand_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"stand_id": stand_id, "images": [], "error": "清单读取失败"}


def image_url(stand_id: str, file: str) -> str:
    return f"/images/{stand_id}/{file}"


def public_images(manifest: dict) -> dict:
    """把内部清单转成可直接给前端的结构（含 URL）。"""
    sid = manifest.get("stand_id", "")
    out: dict[str, list[dict]] = {}
    for item in manifest.get("images", []):
        f = item.get("file")
        if not f:
            continue
        out.setdefault(item.get("kind", "misc"), []).append({
            "file": f,
            "url": image_url(sid, f),
            "bytes": item.get("bytes"),
            "caption": item.get("source_name", ""),
        })
    res: dict = {"stand_id": sid, "total": sum(len(v) for v in out.values())}
    res.update(out)
    if manifest.get("error"):
        res["error"] = manifest["error"]
    return res


# ------------------------------------------------------------
# 雷达图：把 DB 的行组装成 radar_svg 需要的输入
# ------------------------------------------------------------
DIM_KEYS = ("pwr", "spd", "rng", "sta", "prc", "dev")


def radar_payload(row: dict, missing_count: int = 0) -> dict:
    """从 stand_stats 行算出 {stats, missing, values}。

    ★★★实测结论（evaluation/_probe_zero.py，勿再臆测）★★★
      stand_stats 里**没有任何0 值**：pwr/spd/rng/sta/prc/dev 六列
      为 0 的行数都是 0，非空行有 135~149 条。
      有效等级区间是 **1..5**（1=E2=D 3=C 4=B 5=A），
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
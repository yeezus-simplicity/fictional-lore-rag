"""相似替身推荐（M32）。

为什么用**六维数值**而不是向量索引
--------------------------------
两条路都可行，选了前者：

  向量索引   需要 7 GB 模型；且它编码的是**描述文本**的语义，
             不是「能力数值」——两个描述风格相近但能力迥异的替身
             可能被判为相似。
  六维数值   数据现成（stand_stats）、零依赖、可解释
             （能说清楚「哪里像」），且「能力相似的替身」
             本来就该看六维。

★ 缺失维度的处理（本项目的硬规则）
  当前六维完整度 79.87% —— 约 1/5 的替身有维度缺失。
  如果缺失按「不参与比较」处理，会出现两种错误：
    ① 只比了 1 个维度就说「很像」
    ② 把缺失当成 0（等于断言「该项能力为零」，事实是「没数据」）
  → 做法：**只比较两边都有值的维度**，且要求共同维度
     ≥ min_common（默认 3）。不足时**明确返回「数据不足」**，
     而不是硬给一个看似合理的排名。
"""
from __future__ import annotations

from typing import Optional

# 六维权重：破坏力/速度更能代表「像不像」；
# 成长性波动大（同一替身的形态差异）故权重低。
DIM_WEIGHT = {
    "pwr": 1.2, "spd": 1.2, "rng": 1.0,
    "sta": 1.0, "prc": 1.0, "dev": 0.6,
}
DIMS = tuple(DIM_WEIGHT)

DIM_CN = {"pwr": "破坏力", "spd": "速度", "rng": "射程",
          "sta": "持续力", "prc": "精密性", "dev": "成长性"}


def _stats_of(rows: list[dict]) -> dict[str, dict]:
    """{stand_id: {dim: value}}，只保留有值的维度。"""
    out: dict[str, dict] = {}
    for r in rows:
        sid = r.get("stand_id")
        if not sid:
            continue
        vals = {}
        for d in DIMS:
            v = r.get(d)
            if isinstance(v, (int, float)):
                vals[d] = int(v)
        out[sid] = vals
    return out


def _distance(a: dict[str, int], b: dict[str, int]
              ) -> Optional[tuple[float, list[str]]]:
    """加权欧氏距离。只比共有维度。

    Returns:
        (distance, common_dims)；无共有维度时 None
    """
    common = [d for d in DIMS if d in a and d in b]
    if not common:
        return None
    tot = 0.0
    for d in common:
        tot += DIM_WEIGHT[d] * (a[d] - b[d]) ** 2
    return (tot ** 0.5), common


def similar_stands(conn, stand_id: str, k: int = 5,
                   min_common: int = 3) -> dict:
    """找与 stand_id 六维最接近的替身。

    Args:
        conn: psycopg2 连接
        stand_id: 目标替身
        k: 返回条数
        min_common: 至少要有几个**共同有值的维度**才算有效比较

    Returns:
        {
          "stand_id": ..., "name": ...,
          "stats": {dim: value},
          "similar": [{"stand_id","name","distance","common_dims",
                       "diff": {dim: 差值}} ...],
          "note": 说明（数据不足时给出原因）
        }
        ★ 找不到目标或数据不足时 `similar` 为空 + `note` 说明原因，
          **不编造推荐**。
    """
    cur = conn.cursor()
    try:
        # 目标替身的基本信息 + 六维
        cur.execute("""
            SELECT s.stand_id, s.name_en, st.pwr, st.spd, st.rng,
                   st.sta, st.prc, st.dev
            FROM stands s LEFT JOIN stand_stats st ON st.stand_id = s.stand_id
            WHERE s.stand_id = %s
        """, (stand_id,))
        row = cur.fetchone()
        if not row:
            return {"stand_id": stand_id, "similar": [],
                    "note": f"库里没有替身 {stand_id}"}

        cols = ["stand_id", "name_en", "pwr", "spd", "rng",
                "sta", "prc", "dev"]
        target_row = dict(zip(cols, row))
        target_vals = {d: int(target_row[d]) for d in DIMS
                       if isinstance(target_row[d], (int, float))}

        if len(target_vals) < min_common:
            return {
                "stand_id": stand_id, "name": target_row["name_en"],
                "stats": target_vals, "similar": [],
                "note": f"该替身只有 {len(target_vals)} 个维度有数据"
                        f"（少于 {min_common} 个），无法可靠比较",
            }

        # 全量六维（156 行，直接全取）
        cur.execute("""
            SELECT s.stand_id, s.name_en, st.pwr, st.spd, st.rng,
                   st.sta, st.prc, st.dev
            FROM stands s LEFT JOIN stand_stats st ON st.stand_id = s.stand_id
        """)
        all_rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        cur.close()

    table = _stats_of(all_rows)
    names = {r["stand_id"]: r["name_en"] for r in all_rows}

    scored = []
    for sid, vals in table.items():
        if sid == stand_id:
            continue
        d = _distance(target_vals, vals)
        if d is None:
            continue
        dist, common = d
        if len(common) < min_common:
            continue
        scored.append({
            "stand_id": sid,
            "name": names.get(sid) or sid,
            # 距离按共同维度数归一，避免「比得少的反而分低」
            "distance": round(dist, 2),
            "normalized": round(dist / (len(common) ** 0.5), 2),
            "common_dims": len(common),
            "diff": {DIM_CN[c]: target_vals[c] - vals[c] for c in common},
            "stats": {DIM_CN[c]: vals[c] for c in common},
        })

    scored.sort(key=lambda x: (x["normalized"], -x["common_dims"]))
    skipped = len(table) - 1 - len(scored)

    note = None
    if skipped > 0:
        # ★ 明说排除了多少 —— 而不是假装它们不存在
        note = (f"另有 {skipped} 个替身因可用维度少于 {min_common} 个"
                f"未参与比较（六维完整度 79.87%，缺失不按 0 计）")

    return {
        "stand_id": stand_id,
        "name": target_row["name_en"],
        "stats": {DIM_CN[d]: v for d, v in target_vals.items()},
        "similar": scored[:k],
        "considered": len(scored),
        # ★ 明说这是**数值**相似，不是设定相似 ——
        #   实测 Star Platinum 最近邻里有 D4C（六维完全相同），
        #   但一个能时间停止、一个是平行世界，设定毫不相干。
        #   不写清楚用户会误以为「系统觉得这俩是一个路子」。
        "scope": "按六维能力数值的接近程度排序（不代表能力设定相似）",
        "note": note,
    }

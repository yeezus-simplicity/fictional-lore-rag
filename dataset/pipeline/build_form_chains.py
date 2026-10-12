# -*- coding: utf-8 -*-
"""把断裂的形态链补进 stand_forms（M45，解决 M38 遗留）。

== 问题（实测）==
M38 记的「形态链断裂」，实测后定位到具体形态：

  · `stand_forms.json` 146 条，**`parent_id` 一个都没有** → 链根本没连
  · `echoes` / `tusk` 的 **7 个形态成员（ACT1~ACT3 / ACT1~ACT4）
    在 stand_forms 里完全不存在**
  · 而它们在 `stands.json` 里 `form_count=0, form_chain=[]`
    —— 既没连到母体，自己也没被登记为形态
  · 但**六维数据是齐的**（`stand_stats.json` 里 pwr/spd/... + `*_raw`）

→ 即：数据都在，只是**从没建进形态表**。

== 做法 ==
为这两组生成形态链（母体 stand_id + 递增 order + parent 指针）：

    echoes__f0 = ACT1 (base)      ┐
    echoes__f1 = ACT2 (evolved)   ├─ parent 依次指向前一个
    echoes__f2 = ACT3 (evolved)   ┘

    tusk__f0 = ACT1 (base)  … tusk__f3 = ACT4 (evolved)

★ 命名沿用既有规则 `{stand_id}__f{order}`（见 stand_forms.json 现有条目）。
★ values 用 `*_raw` **原始字面量**（'E'/'B'/'C'…），
  与 stand_forms 现有条目的 values 形态一致（不是编码后的数字）。
   —— 与项目一贯原则一致：原始值必须保留以便追溯。

用法：
    python dataset/pipeline/build_form_chains.py            # 预览
    python dataset/pipeline/build_form_chains.py --apply    # 写入
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROC = ROOT / "dataset" / "processed"
FORMS = PROC / "stand_forms.json"
STATS = PROC / "stand_stats.json"

# 形态组：母体 stand_id → [(成员 stand_id, 形态名)]
GROUPS: dict[str, list[tuple[str, str]]] = {
    "echoes": [("echoes_act1", "ACT1"),
               ("echoes_act2", "ACT2"),
               ("echoes_act3", "ACT3")],
    "tusk": [("tusk_act1", "ACT1"),
             ("tusk_act2", "ACT2"),
             ("tusk_act3", "ACT3"),
             ("tusk_act4", "ACT4")],
}

DIMS = ("pwr", "spd", "rng", "sta", "prc", "dev")


def build_record(mother: str, order: int, member: str, form_name: str,
                 stats: dict) -> dict:
    """生成一条 stand_forms 记录（含 parent_id 链）。"""
    values = {}
    for d in DIMS:
        raw = stats.get(f"{d}_raw")
        if raw:
            values[d.upper()] = raw
    return {
        "form_id": f"{mother}__f{order}",
        "stand_id": mother,
        "form_name": form_name,
        "form_type": "base" if order == 0 else "evolved",
        "raw_order": order,
        # ★ 链：第一条无父，其余指向前一个
        "parent_id": None if order == 0 else f"{mother}__f{order - 1}",
        # 追溯用：这条 form 对应 stands 里的哪个（未消解）独立条目
        "_source_stand_id": member,
        "values": values,
    }


def main() -> int:
    apply = "--apply" in sys.argv
    forms = json.loads(FORMS.read_text(encoding="utf-8"))
    stats = {r["stand_id"]: r for r in
             json.loads(STATS.read_text(encoding="utf-8"))}
    existing = {f["form_id"] for f in forms}

    print("=" * 68)
    print("补形态链：echoes / tusk 的 7 个形态")
    print("=" * 68)
    print(f"stand_forms 现有 {len(forms)} 条"
          f"（其中带 parent_id 的 {sum(1 for f in forms if f.get('parent_id'))} 条）")

    added: list[dict] = []
    for mother, members in GROUPS.items():
        print(f"\n--- {mother} ---")
        for order, (member, form_name) in enumerate(members):
            rec = build_record(mother, order, member, form_name,
                               stats.get(member, {}))
            if not rec["values"]:
                print(f"    ✗ {member}：无六维原始值，跳过")
                continue
            flag = "（已存在，更新）" if rec["form_id"] in existing else "（新增）"
            print(f"    {rec['form_id']:14s} ← {member:14s} "
                  f"{rec['form_type']:8s} parent={rec['parent_id']} "
                  f"{rec['values']}{flag}")
            added.append(rec)

    print(f"\n将写入 {len(added)} 条 → stand_forms {len(forms)} → "
          f"{len(forms) + len([r for r in added if r['form_id'] not in existing])} 条")
    if not apply:
        print("\n（预览模式，未写入。加 --apply 生效）")
        return 0

    by_id = {f["form_id"]: f for f in forms}
    for r in added:
        by_id[r["form_id"]] = r
    shutil.copy(FORMS, FORMS.with_suffix(".json.bak"))
    FORMS.write_text(json.dumps(list(by_id.values()), ensure_ascii=False,
                                indent=1), encoding="utf-8")
    print(f"  备份 → {FORMS.name}.bak")
    print("✓ 已写入")
    return 0


if __name__ == "__main__":
    sys.exit(main())

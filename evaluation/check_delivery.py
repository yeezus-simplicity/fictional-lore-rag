"""离线交付包的**功能**自检：不连库、不起服务，只验证 index.html 里的逻辑。

做法：把交付包的 <script> 抠出来，配一个假的 DOM，在 Node 里跑 search()，
看「白金之星 / Ora Ora / 不存在的名字」三种输入分别得到什么。
→ 能过就说明：**把 deliverable/ 整个目录拷到任何机器，双击 index.html 就能用**。
"""
import re
import subprocess
import sys
from pathlib import Path

HTML = Path(__file__).resolve().parents[1] / "deliverable" / "index.html"
NODE = r"C://Users//28188//.workbuddy//binaries//node//versions//22.22.2-6//node.exe"

# 在 Node 里用最小 DOM 打桩（只实现脚本用到的 API）
SHIM = r"""
const _els = {};
function mkEl(){ return {
  _html:'', innerHTML:'', textContent:'', value:'', dataset:{},
  addEventListener(){}, closest(){ return null; },
  querySelectorAll(){ return []; },
};}
for (const id of ['#stats','#chips','#q','#res','#nall','#list']) _els[id]=mkEl();
global.document = { querySelector: s => _els[s] || mkEl(),
                    querySelectorAll: () => [] };
"""

RUN = r"""
// 触发搜索并返回渲染结果
function ask(q){
  _els['#q'].value = q;
  search();
  return _els['#res'].innerHTML;
}
const out = {
  star: ask('白金之星'),
  ora: ask('Ora Ora'),
  none: ask('不存在的替身XYZ'),
  owner: ask('Giorno'),
};
console.log(JSON.stringify(out));
"""


def main() -> int:
    if not HTML.exists():
        print("✗ deliverable/index.html 不存在，先跑 tools/build_delivery.py")
        return 1
    html = HTML.read_text(encoding="utf-8")
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)
    code = "\n".join(scripts)
    # 打桩 + 原脚本 + 探针
    probe = SHIM + code + RUN
    # ★ 代码很长（整个 script 块），命令行传参会超 Windows 32K 限制
    #   （WinError 206「文件名或扩展名太长」）→ 写成临时文件再跑
    import os
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as f:
        f.write(probe)
        tmp = f.name
    try:
        p = subprocess.run([NODE, tmp], capture_output=True, text=True,
                           encoding="utf-8", timeout=60)
    finally:
        os.unlink(tmp)
    if p.returncode != 0:
        print("✗ 交付包脚本执行失败：")
        print(p.stderr[-800:])
        return 1
    import json
    out = json.loads(p.stdout.strip().split("\n")[-1])
    # 交付包里内联的数据（一致性断言要用）
    import re as _re
    m = _re.search(r"const DATA = (\{.*?\});\n", html, _re.S)
    d_all = json.loads(m.group(1))
    d_stands = d_all.get("stands") or []
    fails = []
    print("=" * 62)
    print("离线交付包功能自检（不连库、不起服务）")
    print("=" * 62)

    def check(name, cond, detail=""):
        print(f"  {'PASS' if cond else 'FAIL'}  {name}"
              + (f"   {detail}" if detail and not cond else ""))
        if not cond:
            fails.append(name)

    check("查「白金之星」→ 命中替身档案",
          "Star Platinum" in out["star"] and "空条" not in out["star"],
          out["star"][:120])
    check("查「白金之星」→ 使用者有值",
          "Jotaro" in out["star"], out["star"][:200])
    check("查「Ora Ora」→ 命中招式",
          "Ora Ora" in out["ora"] and "招式" in out["ora"], out["ora"][:120])
    check("查不存在的名字 → 明确说没有，不硬凑",
          "没有匹配" in out["none"], out["none"][:120])
    check("查「Giorno」→ 按使用者反查到替身",
          "Gold Experience" in out["owner"], out["owner"][:160])
    check("缺数据显示「源站未提供」（不是空白/0）",
          "源站未提供" in out["star"], out["star"][:200])

    # ---- 与主站的一致性（离线包最容易悄悄漏的一块）----
    #   实测第一版打包漏了中文名与中文类型（它们不在 stands.json 里，
    #   而在别名表/DB）→ 离线包搜「白金之星」什么都搜不到，
    #   而主站是好的。**「主站有、离线包没有」最难发现**，
    #   因为离线包自己跑得毫无异常。
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
        from aliases import load_zh_map
        zh = load_zh_map()
    except Exception:  # noqa: BLE001
        zh = {}
    by_id = {x.get("id"): x for x in d_stands}
    zh_in_pack = sum(1 for x in d_stands if x.get("zh"))
    zh_in_alias = sum(1 for v in zh.values() if v.get("name_zh"))
    # 别名表里有历史 id（如 echoes 已拆成 ACT1/2/3）→ 只比两边都存在的
    both = [sid for sid, v in zh.items()
            if v.get("name_zh") and sid in by_id]
    missing = [sid for sid in both if not by_id[sid].get("zh")]
    check("中文名：主站有中文名的，离线包也都有",
          not missing, f"缺 {missing[:5]}")
    check(f"中文名覆盖率 {zh_in_pack}/{len(d_stands)}（别名表 {zh_in_alias}）",
          zh_in_pack >= 185, f"实得 {zh_in_pack}")
    check("中文类型已并入（168 个替身有 stand_type_cn）",
          sum(1 for x in d_stands if x.get("type_cn")) >= 160,
          f"实得 {sum(1 for x in d_stands if x.get('type_cn'))}")
    check("异译别名已并入（可按「娃娃脸」这类译名搜）",
          sum(len(x.get("zh_variants") or []) for x in d_stands) > 200,
          f"实得 {sum(len(x.get('zh_variants') or []) for x in d_stands)}")

    print("\n" + "=" * 62)
    if fails:
        print(f"✗ {len(fails)} 项未通过：{fails}")
        return 1
    print("✓ 全部通过 —— 拷走整个目录、双击 index.html 即可使用")
    return 0


if __name__ == "__main__":
    sys.exit(main())

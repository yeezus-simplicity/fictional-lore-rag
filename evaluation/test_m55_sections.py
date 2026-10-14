"""M55 回归测试：搜索结果要按**板块**组织。

用户原话：
  「我不是这个意思啊 是搜索出来如果是替身有关的 就应该有好几个板块
    如答案 替身信息 和图片 你先改回去 然后再按照我的改」

== 我上一轮（M54）做错在哪 ==
  我把「图片 + 证据」塞进了**同一张没有标题的卡**里：
      <div class="card">${media}${evidence}</div>
  两个后果：
    1. 那张卡没有 h3 —— 它不是一个"板块"，只是一坨内容
    2. 证据（折叠 30px）被压在 **1500px 的图片墙**下面 → 等于看不见
  用户要的是：**答案 / 替身信息 / 图片** 各成一个板块。

== 现在的结构（改回来 + 按用户描述重做）==
  板块① 答案      —— 答案内容 + **证据（检索过程）**（证据是这条答案的出处）
  板块② 替身信息  —— 档案键值 + 六维明细 ｜ 六维雷达
  板块③ 图片      —— 按来源分组（本体/使者/漫画/动画/其它）
  板块④ 相似替身  —— 原有，自带标题
  ★ 四个板块**各自成卡、各有 h3 标题**。

== 顺带清掉的重复 ==
  · 「替身档案」（卡内 .zc-title）→ 提到卡片 h3「替身信息」，
    原说明挪到 h3 后面的小字（.h3n）。同一句话不再出现两次。
  · 图片块内的「相关图片 N」→ 卡片标题已是「图片」，
    内部改成「N 张 · 按来源分组」（只说数量与分类口径）。

== 本测试守什么 ==
  1. 四个板块的标题都存在，且由各自的渲染函数产出
  2. 图片的三种状态（就绪 / 抓取中 / 失败）都装在**同一张卡**里
     —— 异步抓完替换内容时标题不会消失
  3. 证据在答案卡内、图片在答案卡之外
  4. 没有「无标题卡片」（M54 的那个写法不能再出现）
  5. 重复标题已清理（.zc-title 死样式删除、图片块不再写「相关图片」）
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "api" / "static" / "index.html"


def main() -> int:
    fails: list[str] = []
    n = 0

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal n
        n += 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}"
              f"{('   ' + detail) if detail and not ok else ''}")
        if not ok:
            fails.append(name)

    src = HTML.read_text(encoding="utf-8")
    css = re.sub(r"<script[^>]*>.*?</script>", "", src, flags=re.S)

    def fn(name: str) -> str:
        m = re.search(rf"function {name}\([^)]*\)\{{[\s\S]*?\n\}}", src)
        return m.group(0) if m else ""

    print("=" * 74)
    print("M55：搜索结果按板块组织（答案 / 替身信息 / 图片 / 相似替身）")
    print("=" * 74)

    # ---- ① 五个板块都有标题，且都带 data-sec（供左侧索引跳转） ----
    print("\n[1] 板块的标题与锚点")
    ra, rp, rm = fn("renderAnswer"), fn("renderStandPanel"), fn("renderMedia")
    check("找到三个渲染函数", bool(ra and rp and rm))
    check("板块「答案」有 h3", "<h3>答案" in ra)
    # ★ M57：按用户用词改名「替身信息」→「替身资料」
    check("板块「替身资料」有 h3", "<h3>替身资料" in rp)
    check("板块「检索过程」有自己的卡片与折叠标题（M57 从页顶移进来）",
          'data-sec="sec-proc"' in ra and "<summary>检索过程" in ra)
    check("板块「图片」有 h3", '<h3>图片' in rm)
    check("板块「相似替身」有 h3", "<h3>相似替身" in src)
    check("★ 不再有「无标题卡片」（M54 的 media+evidence 合卡已删除）",
          not re.search(r"\$\{_media\}\$\{_ev\}", src)
          and "const _media = renderMedia(d);" not in ra)
    # 五个板块的 data-sec / data-secname
    for key, nm in [("sec-answer", "答案"), ("sec-stand", "替身资料"),
                    ("sec-proc", "检索过程"), ("sec-images", "图片"),
                    ("sec-similar", "相似替身")]:
        check(f"「{nm}」带 data-sec={key} + data-secname={nm}",
              f'data-sec="{key}" data-secname="{nm}"' in src)

    # ---- ② 板块顺序 = 用户列的顺序；证据归「检索过程」 ----
    print("\n[2] 板块顺序与证据归属")
    i_ans = ra.find("sec-answer")
    i_stand = ra.find("renderStandPanel(d)")
    i_proc = ra.find("sec-proc")
    i_ev = ra.find("renderEvidence(")
    i_media = ra.find("renderMedia(")
    check("顺序：答案 → 替身资料 → 检索过程 → 图片",
          0 <= i_ans < i_stand < i_proc < i_media,
          f"答案 {i_ans} / 资料 {i_stand} / 检索 {i_proc} / 图片 {i_media}")
    check("★ 证据归「检索过程」板块（它是这条答案的出处）",
          i_proc < i_ev < i_media, f"检索 {i_proc} / 证据 {i_ev} / 图片 {i_media}")
    check("★ 先渲染答案再填检索过程（否则 #proc 还不存在，写入静默失效）",
          bool(re.search(r"renderAnswer\(d\);\s*\n\s*renderProcess\(d\);",
                         src)))
    check("#procCard 访问都判空（它在 #ans 里，可能已被清空）",
          src.count("const _pc0 = $('#procCard');") == 1
          and "if(_pc){ _pc.open = false;" in src)

    # ---- ③ 图片板块的三种状态同卡 ----
    print("\n[3] 图片板块（三种状态都不能丢标题）")
    check("卡片头只定义一次、且带锚点",
          'const head = `<div class="card" data-sec="sec-images"' in rm)
    check("已就绪分支用 head 拼",
          "return head + imgSectionHTML(im) + '</div>'" in rm)
    check("抓取中分支用 head 拼",
          bool(re.search(r"return head \+ `<div class=\"media-block\" id=\"imgPending\"", rm)))
    check("失败分支用 head 拼",
          bool(re.search(r"return head \+ `<div class=\"media-block\">", rm)))
    check("无图片信息时返回空串（不出空板块）", bool(re.search(r"return '';\s*\n\}", rm)))
    check("抓取中仍会启动轮询", "pollImages(im.stand_id)" in rm)

    # ---- ④ 重复标题已清理 ----
    print("\n[4] 标题不再重复")
    #   ★ 只查**实际用法**：注释里提到 `.zc-title` 是说明为什么删掉它，不算残留
    check("卡内 .zc-title 的用法已删除（只剩注释提及）",
          'class="zc-title"' not in src,
          f"仍有 {src.count('class=\"zc-title\"')} 处用法")
    check("  └ 死样式 .zc-title 也已删除", ".zc-title {" not in css)
    check("说明文字挪到 h3 的小字里（.h3n）",
          'class="h3n"' in src and ".card h3 .h3n" in css)
    check("图片块内不再写「相关图片」（与卡标题重复）",
          "相关图片 <span" not in src and ">相关图片<" not in src)
    check("  └ 改成数量与分类口径", "张 · 按来源分组" in src)

    # ---- ⑤ 布局约束没被破坏（M52） ----
    print("\n[5] M52 的左右对齐约束仍成立")
    check(".stand-panel 仍是 flex 横向",
          bool(re.search(r"\.stand-panel\s*\{[^}]*display:\s*flex", css)))
    check(".sp-info 仍是纵向 flex", bool(re.search(r"\.sp-info\s*\{[^}]*flex-direction:\s*column", css)))
    check(".zc-dimgrid 仍 flex:1（吃掉剩余高度）",
          bool(re.search(r"\.zc-dimgrid\s*\{[^}]*flex:\s*1 1 auto", css)))
    check(".stand-panel 不再自带 margin-bottom（改由卡片负责）",
          bool(re.search(r"\.stand-panel\s*\{[^}]*\}", css))
          and "margin-bottom: 14px" not in re.search(
              r"\.stand-panel\s*\{([^}]*)\}", css).group(1))

    print()
    if fails:
        print(f"✗ {len(fails)}/{n} 项失败：" + "、".join(fails))
        return 1
    print(f"✓ 全部通过（{n} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

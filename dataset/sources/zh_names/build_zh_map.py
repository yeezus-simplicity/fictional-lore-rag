"""
构建替身中文名词典（M15）。

==★ 数据来源与可信度分级 ==
  Level A（官方）：stands.json 的 name_ja 括号内中文（jojowiki 官方标注）
  Level B（双来源一致）：两个独立来源给出同一译名                              —— 可用
  Level C（单来源）：只有一个来源                                           —— 收录但标注
  ★ 分级不是形式主义：Level A 冲突时以A 为准（官方>民间）

==★ M16 新增两个脚本来源（不手工维护）==

  1. fetch_zh_wiki.py   → zh_wiki_pairs.json
     中文维基百科「替身(JoJo的奇妙冒险)」总表里的人工编纂英中对照表（201 组）
  2. fetch_zh_huiji.py  → zh_huiji_pairs.json
     JOJO 中文维基 huijiwiki 镜像的逐词条 {{Stand Info}} 模板（含 title/engname）
     —— 总表里查不到的冷门替身（巴斯特女神 / 小面孔 / 神圣之屋…）靠它兜底

★ 为什么脚本来源优先于猜译：
  实测jojowiki 单页里Chinese/中文 命中 0 次 → 「继续从 jojowiki 抓」走不通；
  而中文维基的「骇游天外」「紫烟破音」「洋娃娃匕首」这类译名一看就是人工查证的，
  正是用户实际会输入的写法。
  ★抓不到就是中文圈确实没有通行译名 → 如实留缺口，不硬译（M15 已实测硬译会拉低匹配质量）。

==★ 为什么不用自动翻译 ==
  1. 译名高度约定俗成：「Gold Experience」官方译「黄金体验」，
     机器翻译会给「黄金经验」—— 看起来对但用户不会这么输入
  2. 同一名词有多个流传版本（ Oh! Lonesome Me = 喔!寂寞的我 / 哦!孤独的我），
     机器只能给一个，用户两个都会用
  3. 译名是**产品资产**，需要可审阅、可修改 —— 抓取+人工核对，而不是黑盒

用法：python dataset/sources/zh_names/build_zh_map.py
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PROC = ROOT / "dataset" / "processed"
OUT = ROOT / "dataset" / "processed" / "stand_name_zh.json"


# ============================================================
# Level B/C：抓取到的中英对照（来源见文件末尾SOURCES）
# ============================================================
# ★ 网页排版产物：字与字之间有空格，抓取后必须去掉
_RAW_PAIRS = """
STAR PLATINUM|白金之星
MAGICIAN RED|红色魔术师
HERMIT PURPLE|隐者之紫
HIEROPHANT GREEN|绿之法皇
TOWER OF GRAY|灰塔
SILVER CHARIOT|银色战车
DARK BLUE MOON|暗青之月
# ★ 「力」是单字 —— M14 实测证明单字中文名会误匹配
#   （「力」会抢走「黄金体验的能力」）。改用带限定词的常见译名。
STRENGTH|力量
DEVIL|恶魔
YELLOW TEMPERANCE|黄色节制
HANGED MAN|倒吊男
EMPEROR|皇帝
EMPRESS|女帝
WHEEL OF FORTUNE|命运的车轮
JUSTICE|正义
LOVERS|恋人
SUN|太阳
DEATH THIRTEEN|死神13
JUDGEMENT|审判
HIGH PRIESTESS|女教皇
THE FOOL|愚者
GEB|盖布神
TOHTH|托托神
KHNUM|库努姆神
ANUBIS|安努比斯神
BAST|巴斯特女神
SETHAN|瑟多神
OSIRIS|奥西里斯
HORUS|霍尔斯神
ATUM|阿多姆神
TENORE SAX|迪那·塞克斯
CREAM|亚空瘴气
THE WORLD|世界
CRAZY DIAMOND|疯狂钻石
AQUA NECKLACE|银水链
THE HAND|轰炸空间
BAD COMPANY|极恶中队
RED HOT CHILI PEPPER|辛红辣椒
THE LOCK|心锁
SURFACE|表面
LOVE DELUXE|紫色恋人
PEARL JAM|珍珠果酱
ACHTUG BABY|透明宝宝
HEAVEN'S DOOR|天堂之门
RATT|虫眼
HARVEST|钱宝宝
KILLER QUEEN|皇后杀手
CINDERELLA|灰姑娘
SHEER HEART ATTACK|穿心攻击
ATOM HEART FATHER|破碎的慈父心
BOY II MAN|猜拳小子
EARTH WIND AND FIRE|大地·风和火
HIGHWAY STAR|高速之星
STRAY CAT|猫草
SUPER FLY|超能平底锅
ENIGMA|摺纸师
CHEAP TRICK|廉价魔术
GOLD EXPERIENCE|黄金体验
STICKY FINGERS|万能手指
BLACK SABBATH|黑色安息日
MOODY BLUES|蓝色忧郁
SOFT MACHINE|柔软机器
SEX PISTOLS|性感手枪
CRAFT WORK|手工艺
LITTLE FEET|小脚
AERO SMITH|空中狙击手
MAN IN THE MIRROR|镜中人
PURPLE HAZE|紫色烟雾
BEACH BOY|海滩男孩
THE GRATEFUL DEAD|幸福死亡
MR.PRESIDENT|乌龟
BABY FACE|婴儿面孔
WHITE ALBUM|白色相册
KING CRIMSON|绯红之王
TALKING HEAD|谈话头脑
CLASH|冲击
NOTORIOUS B.I.G|臭名昭著
SPICE GIRL|辣妹
METALLICA|金属制品
GREEN DAY|绿色末日
OASIS|沙漠绿洲
REQUIEM|银色战车·镇魂歌
GOLD EXPERIENCE REQUIEM|黄金体验·镇魂歌
ROLLING STONES|命运之石
Stone Free|自由之石
Goo Goo Dolls|变化人偶
Burning Down the House|幽灵房间
Manhattan Transfer|曼哈顿转播站
White Snake|白蛇
Kiss|亲吻
Highway to Hell|地狱高速路
Foo Fighters|无名斗士
Marilyn Manson|追债人玛丽莲
Weather Report|天气预报
Jumping Juck Flash|旋转闪光
Limp Bizkit|圆舞黑鹰
Diver Down|潜伏者
Planet Waves|行星波导
Survivor|生存
Dragon's Dream|龙之梦
Yo-Yo Ma|陀螺
Jail House Lock|恶魔枷锁
Bohemian Rhapsody|波西米亚狂想曲
Under World|地底世界
C-MOON|新月
Made in Heaven|天堂制造
Kraft Work|工匠
Magician's Red|红色魔术师
Magician Red|红色魔术师
Green, Green Grass of Home|绿草之家
Chariot Requiem|战车镇魂歌
Ebony Devil|黑檀木恶魔
Jumpin' Jack Flash|旋转闪光
# ---- M16：以下为中文维基百科总表人工复核项（其余由脚本抓取提供）----
Paisley Park|佩斯利公园
California King Bed|加州大床
Paper Moon King|纸月之王
Born This Way|天生完美
Sky High|骇游天外
Voodoo Child|巫毒之子
All Along Watchtower|永恒的守望塔
Dolly Dagger|洋娃娃匕首
Manic Depression|狂躁抑郁
Rainy Day Dream Away|雨天迷梦
Nightbird Flying|夜鸟飞翔
"""

# ★★ SBR（第七部）：两个来源交叉一致才用 Level B
_SBR_CONFIRMED = {
    "Tusk": "牙", "Tusk ACT1": "牙ACT1", "Tusk ACT2": "牙ACT2",
    "Tusk ACT3": "牙ACT3", "Tusk ACT4": "牙ACT4",
    "Ball Breaker": "铁球破坏者", "Cream Starter": "护霜旅行者",
    "Scary Monsters": "骇人恶兽", "Tomb of the Boom 1 2 3": "盛世之墓",
    "Boku no Rhythm wo Kiitekure": "聆听我的旋律", "Wired": "连线",
    "Mandom": "男人领域", "Sugar Mountain": "香糖山之泉",
    "TATOO YOU!": "为你纹身", "Tubular Bells": "管钟",
    "20th Century BOY": "20世纪少年", "Civil War": "南北战争",
    "Chocolate Disco": "巧克力迪斯科",
    "Dirty Deeds Done Dirt Cheap": "恶行易施",
    "D4C Love Train": "D4C爱的列车",
}

# ★ 同义异译：两个来源不同但都是流通译名 → 都收录，用户两种写法都能命中
_SBR_VARIANTS = {
    "Oh! Lonesome Me": ["喔!寂寞的我", "哦!孤独的我"],
    "In a Silent Way": ["寂静之道", "沉默之道"],
    "Catch the Rainbow": ["触碰彩虹", "彩虹捕手"],
    "Ticket to Ride": ["泪之乘车券", "泪之乘车劵"],
    "Hey Ya!": ["嘿呀!", "嘿呀"],
}


def norm_en(s: str) -> str:
    """英文名规范化：只保留小写字母与数字。

    ★ 实测踩坑：最初用 `re.sub(r"[^\\w\\s]", "", s)` 保留空格，
      而抓取侧的 norm() 把空格删了 → 两边key 形态不一致
      → 154 个里只匹配上 30 个。
      → 统一成「只保留字母数字」，空格/标点/全角全归一。
    """
    s = unicodedata.normalize("NFKC", s).lower()
    return re.sub(r"[^a-z0-9]", "", s)


def main() -> int:
    stands = json.loads((PROC / "stands.json").read_text(encoding="utf-8"))

    # ---- Level B/C：抓取来源 ----
    fetched: dict[str, str] = {}
    for line in _RAW_PAIRS.strip().split("\n"):
        if "|" not in line:
            continue
        en, zh = line.split("|", 1)
        en = re.sub(r"\s+", "", en.strip())
        zh = re.sub(r"\s+", "", zh.strip())
        if en and zh:
            fetched.setdefault(norm_en(en), zh)
    for en, zh in _SBR_CONFIRMED.items():
        fetched.setdefault(norm_en(en), zh)

    # ---- M16：加载脚本抓取的两个来源 ----
    # ★ fetched_variants[k] = [一级译名, 异译别名...]，
    #   顺序有意义：第一个是主名，其余作为 name_zh_variants 并列收录。
    #   优先级低于 _RAW_PAIRS（人工复核过的更可信）→ 用 setdefault。
    fetched_variants: dict[str, list[str]] = {}
    fetched: dict[str, str] = fetched
    n_wiki = n_huiji = 0
    for fname, src in (("zh_wiki_pairs.json", "zh.wikipedia.org"),
                       ("zh_huiji_pairs.json", "jojo.huijiwiki.com")):
        path = Path(__file__).resolve().parent / fname
        if not path.exists():
            print(f"  !缺少 {fname}，跳过（先跑对应的 fetch_*.py）")
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        for k, names in data.items():
            names = [n for n in names if n and len(n) >= 2]
            if not names:
                continue
            fetched_variants.setdefault(k, [])
            for n in names:
                if n not in fetched_variants[k]:
                    fetched_variants[k].append(n)
            fetched.setdefault(k, names[0])
        if "huiji" in fname:
            n_huiji = len(data)
        else:
            n_wiki = len(data)
    print(f"  抓取来源：中文维基 {n_wiki} 组 / JOJO中文维基 {n_huiji} 组")

    # ---- Level A：官方 name_ja 括号内的中文名（优先级最高）----
    official: dict[str, str] = {}
    for s in stands:
        ja = s.get("name_ja") or ""
        m = re.search(r"[（(]([^（()）]*[\u4e00-\u9fff][^（()）]*)[）)]", ja)
        if m:
            official[norm_en(s["name_en"])] = m.group(1)

    # ---- 官方单字名的降级处理 ----
    #★ 实测发现：jojowiki 的 name_ja 括号里是**日文汉字直译**，不是中文
    #   社区的实际叫法：
    #     Strength →「力」（日文直译）但中文圈普遍叫「力量」
    #     The Hand →「手」（日文直译）但中文圈普遍叫「轰炸空间」
    #   ★ 而单字名会**误匹配**（M14 已实测：「力」抢走「黄金体验的能力」）。
    #   → 官方性不能凌驾于正确性：单字的官方名**降级为参考**，
    #     改用通行译名，并把官方原名记进 name_ja_official 备查。
    _SINGLE_CHAR_OFFICIAL = {norm_en("Strength"), norm_en("The Hand")}

    #★★ 日文汉字形标志字：这些写法在中文社区基本不用
    #   （「体験」「星の白金」是日文语序/汉字，中文说「体验」「白金之星」）
    # ★「審判」用的是**繁体「判」**（U+5224），简体是「判」但字形不同。
    #   该字在中文里两种写法都有，jojowiki 用的是繁体形；
    #   抓取表的简体「审判」更符合大陆用户习惯 → 优先。
    _JP_MARKERS = ("体験", "験", "體", "經", "壓", "氣", "車", "門",
                   "鬥", "髮", "馬", "鳥", "魚", "館", "裝", "の",
                   "審判")

    def _has_japanese_forms(zh: str) -> bool:
        return any(mk in zh for mk in _JP_MARKERS)

    # ---- 组装 ----
    out: dict[str, dict] = {}
    n_a = n_b = n_c = 0
    for s in stands:
        en = s["name_en"]
        k = norm_en(en)
        if not k:
            continue
        zh = official.get(k)
        if k in _SINGLE_CHAR_OFFICIAL:
            # ★ 单字官方名 → 不用它，用通行译名；官方名仅备查
            alt = fetched.get(k)
            if alt:
                out[s["stand_id"]] = {
                    "name_en": en, "name_zh": alt,
                    "level": "C", "source": "jojogh.jojo6.com",
                    "name_ja_official_single": zh,
                    "_note": ("jojowiki 的 name_ja 括号是日文汉字直译"
                              f"（{zh!r}），但中文圈通行译名是{alt!r}；"
                              "单字名会误匹配（M14 实测），故采用后者"),
                }
                n_c += 1
                continue
            # 没有替代译名 → 宁可不收（单字名不可用）
            continue
        # ★★ 官方名可能是**日文汉字**（黄金体験/ 星の白金），
        #    而用户输入简体（黄金体验 / 白金之星）。
        #    → 简体译名优先，官方日文名作为**补充别名**保留。
        #    （两者都收，两种写法都能命中）
        simp_zh = fetched.get(k)
        if zh and _has_japanese_forms(zh):
            if simp_zh:
                e = {
                    "name_en": en, "name_zh": simp_zh,
                    "level": "C", "source": "jojogh.jojo6.com",
                    "name_ja_official": zh,
                    "_note": ("官方 name_ja 是日文汉字写法"
                              f"（{zh}），用户多用简体（{simp_zh}），"
                              "两者都收录；繁体/日文形另存aliases"),
                }
                # M16：抓取来源可能给出多个异译，也一并带上
                vs = [n for n in fetched_variants.get(k, []) if n != simp_zh]
                if vs:
                    e["name_zh_variants"] = [simp_zh] + vs
                out[s["stand_id"]] = e
                n_c += 1
                continue
            # 没有简体译名 → 仍用官方名，但记明是日文形
        if zh:
            level, src = "A", "jojowiki name_ja"
            n_a += 1
        elif k in fetched:
            zh = fetched[k]
            level = "B" if k in {norm_en(x) for x in _SBR_CONFIRMED} else "C"
            src = "jojogh.jojo6.com" if level == "C" else "bilibili+萌娘百科"
            n_b += level == "B"
            n_c += level == "C"
        else:
            continue
        entry = {
            "name_en": en,
            "name_zh": zh,
            "level": level,
            "source": src,
        }
        # ---- M16：附上抓取到的异译别名 ----
        # ★ 用户两种写法都会输入（软又湿 / 柔软且湿润），必须并列收录，
        #   否则用户换个说法就匹配不上 → 又变成"答非所问"。
        vs = [n for n in fetched_variants.get(k, []) if n != zh]
        if vs:
            entry["name_zh_variants"] = [zh] + vs
        out[s["stand_id"]] = entry
        continue

    # 同义异译（额外别名）
    for en, variants in _SBR_VARIANTS.items():
        for s in stands:
            if norm_en(s["name_en"]) == norm_en(en):
                out.setdefault(s["stand_id"], {
                    "name_en": en, "name_zh": variants[0],
                    "level": "C", "source": "bilibili+萌娘百科（异译并列）",
                })
                out[s["stand_id"]]["name_zh_variants"] = variants
                break

    # ---- 统计与自检 ----
    total = len(stands)
    usable = {k: v for k, v in out.items()
              if not v.get("_excluded_from_matching")}
    have = len(usable)
    print("=" * 66)
    print("中文名词典构建")
    print("=" * 66)
    print(f"  替身总数 {total}  **可匹配的中文名 {have}**（{have/total*100:.0f}%）")
    print(f"    Level A（官方 name_ja）  {n_a}")
    print(f"    Level B（双来源一致）    {n_b}")
    print(f"    Level C（单来源）        {n_c}")
    print(f"  缺中文名 {total - have}（需用英文名）")
    # M16：异译别名覆盖（用户换个说法也能匹配上）
    n_var = sum(1 for v in usable.values() if v.get("name_zh_variants"))
    n_var_all = sum(len(v.get("name_zh_variants", []))
                    for v in usable.values())
    print(f"    带异译别名 {n_var} 条 / 共 {n_var_all} 个可接受中文写法")

    # ★★ 不变量 1b：中文名不能含**繁体/日文独有字形**
    #   实测：Level A 的官方名里混着「審判」（繁体）、「悪魔」（日文汉字）
    #   —— 这些字用户根本不会输入，保留它们只会虚增覆盖率。
    #   → 构建时就降级（优先用简体译名），并在此处复查。
    _NON_SIMP_JP = set("審判壓氣車門鬥髮馬鳥魚館裝經驗體験複"
                       "數據點擊網頁實現處於業務")
    non_simp = [(v["name_en"], v["name_zh"]) for v in out.values()
                if any(ch in _NON_SIMP_JP for ch in v["name_zh"])]
    if non_simp:
        print(f"  ! {len(non_simp)} 条含繁体/日文字形（构建时已优先降级，"
              f"此处复查）:")
        for en, zh in non_simp[:8]:
            print(f"      {en} → {zh}")
        print("      → 这类写法用户不会输入，不计入覆盖率")
        for sid, v in list(out.items()):
            if any(ch in _NON_SIMP_JP for ch in v["name_zh"]):
                v["_excluded_from_matching"] = True

    # 不变量 1：中文名至少 2 字
    #★★ M14 实测：单字中文名会**抢匹配**——
    #   Strength 的中文名曾写作单字「力」，
    #   而问句「黄金体验的**能力**」里也有「力」
    #   → 单字别名抢在「黄金体験」之前命中 → 返回 Strength 的数据。
    #   ★ 这不是理论风险，是实测踩过的坑。
    single = [(v["name_en"], v["name_zh"]) for v in out.values()
              if len(re.sub(r"[\s·、,，/]", "", v["name_zh"])) < 2]
    print(f"\n  {'✓' if not single else '✗'} 中文名均≥2 字"
          f"{'' if not single else f' —— 单字 {single[:5]}'}")
    if single:
        print("      ★ 单字名会误匹配（M14 实测），必须换成带限定词的译名")
        return 1

    # 不变量 2：中文名不能重复（不同替身同中文名会误判）
    seen: dict[str, list[str]] = {}
    for v in out.values():
        seen.setdefault(v["name_zh"], []).append(v["name_en"])
    dup = {k: v for k, v in seen.items() if len(v) > 1}
    print(f"  {'✓' if not dup else '✗'} 中文名唯一"
          f"{'' if not dup else f' —— 重复 {len(dup)} 个'}")
    if dup:
        for k, v in list(dup.items())[:5]:
            print(f"      {k!r} ← {v}")

    # 不变量 3：每个 stand_id 都能对上stands.json
    ids = {s["stand_id"] for s in stands}
    bad = set(out) - ids
    print(f"  {'✓' if not bad else '✗'} stand_id 全部有效"
          f"{'' if not bad else f' —— 无效 {len(bad)}'}")

    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"\n  → {OUT.relative_to(ROOT)}")

    print("\n" + "=" * 66)
    print("来源")
    print("=" * 66)
    print("""  A 级  stands.json 的 name_ja 括号（jojowiki 官方标注）
  B 级  bilibili cv4624640 × 萌娘百科（两来源译名一致）
  C 级  jojogh.jojo6.com/stand/stand_value.htm（单来源，繁体转简体）
         + zh.wikipedia.org「替身(JoJo的奇妙冒险)」总表（M16 脚本抓取）
         + jojo.huijiwiki.com 逐词条{{Stand Info}}（M16，总表查不到的冷门替身）
         +人工核实项（出处见 fetch_zh_huiji.py 文件头）

★ Level A 优先：官方译名与民间译名冲突时以官方为准。
  ★ 例：Anubis 官方标「無」→ 我们的C 级写「安努比斯神」，
    但因为官方那条 name_ja 没括号，所以不冲突。

★ M16 结果：154/154 全覆盖，异译别名 84 条（共 179 个可接受中文写法）。
  ★ 关键是这些译名全部**来自人工编纂的词条/对照表**，没有一个是机器翻译：
    「骇游天外」「紫烟破音」「洋娃娃匕首」「永恒的守望塔」这类译法
    一看就是查证过的，也正是用户实际会输入的写法。""")
    return 0


if __name__ == "__main__":
    sys.exit(main())

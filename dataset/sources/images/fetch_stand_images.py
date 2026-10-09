"""
抓取替身图片（M17）。

==★ 网络前提：必须走代理 ★★

实测（本机环境）：
  - jojowiki.com / static.jojowiki.com 的 DNS 都解析到 **198.18.0.x**
    —— 这是代理沙箱地址，不是真实IP。
  - urllib **默认不读** http_proxy/https_proxy 环境变量
    → 直连 static.jojowiki.com 会 SSL handshake timeout（试了 20s+）
  - 显式给 ProxyHandler({'http':proxy,'https':proxy}) 后立刻成功
  → 所以本模块**强制**从环境变量取代理并显式构造 opener。

==★ 为什么用 400px thumb 而不是原图★

实测Star Platinum 的原图 **6.4MB** → 读到 109KB 就 IncompleteRead（中断）。
  400px thumb = 300KB / 4.8s   ✓
  800px thumb = 1.1MB / 33s     ✗ 太慢，GUI 展示也用不上
→ 统一用 400px。

==★ 图片怎么分类 ★

jojowiki 的文件名是有规律的（实测 Achtung Baby 页58 张图）：
    Achtung_Baby_Infobox_Manga.png   → 替身本体（漫画）
    Achtung_Baby_Infobox_Anime.png   → 替身本体（动画）
    ..._User_...                     → 替身使者
    ..._Cover_.../..._Gallery_...    → 漫画截图
    SDCSymbolLarge.png               → 部徽/图标，**必须过滤**
→ 按关键词分类，命中不了归misc。

用法：
    python dataset/sources/images/fetch_stand_images.pyStar_Platinum Gold_Experience
    python dataset/sources/images/fetch_stand_images.py --from-stands   # 按 stands.json 全量
输出：
    images/<stand_id>/stand_1.png   等，以及 _images.json 清单
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
IMG_ROOT = ROOT / "images"

# ★ 抓取尺寸：实测 400px 是速度/清晰度平衡点
THUMB_PX = 400

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def build_opener() -> urllib.request.OpenerDirector:
    """构造走代理的 opener。

    ★ 不设ProxyHandler({}) 会导致 urllib 直连 → 198.18.0.x 握手超时。
      所以优先读环境变量；没有代理时才用 ProxyHandler({}) 明确置空
      （明确置空可避免 urllib 在有代理环境里走直连）。
    """
    proxy = (os.environ.get("https_proxy")
             or os.environ.get("HTTPS_PROXY")
             or os.environ.get("http_proxy")
             or os.environ.get("HTTP_PROXY") or "").strip()
    handler = urllib.request.ProxyHandler(
        {"http": proxy, "https": proxy} if proxy else {})
    opener = urllib.request.build_opener(handler)
    opener.addheaders = [
        ("User-Agent", _UA),
        ("Referer", "https://jojowiki.com/"),
        ("Accept", "image/avif,image/webp,image/png,image/*,*/*;q=0.8"),
    ]
    return opener


_OPENER = None


def fetch(url: str, tries: int = 2, timeout: int = 25) -> bytes:
    global _OPENER
    if _OPENER is None:
        _OPENER = build_opener()
    last: Exception | None = None
    for i in range(tries):
        try:
            return _OPENER.open(url, timeout=timeout).read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"下载失败 {url[-70:]}: {last}")


# ------------------------------------------------------------
# 分类规则
# ------------------------------------------------------------
# ★ 必须过滤的噪声：符号图标、周边、模型摆件
#   实测 Achtung Baby 页58 张里，真正有用的只有 10 张左右，
#   混着 SDCSymbolLarge（部徽）、Stand Users Shirt（周边T恤）、
#   Acrylic_Diorama（模型摆件）—— 全是噪声，混进 GUI 很难看。
_NOISE = re.compile(
    r"Symbol|Icon|Logo|Flag|Wiki|Wikia|Commons|PDF|Folder|Edit|"
    # 周边/ 模型 / 道具
    #★★ 边界不能省！★★
    #   原先写 `Box` →re.I 下把 "Achtung_Baby"里的 "bBa" 也匹配上了
    #   （.search 只需找到子串）→ 本体图 Infobox_Manga 被整体误杀成 noise，
    #   实测 stand 类候选直接变成 0。
    #   → 所有单词型噪声必须加分隔符边界。
    r"(?:^|[_ .-])Shirt(?:[_ .-]|$)|(?:^|[_ .-])Hat(?:[_ .-]|$)|"
    r"(?:^|[_ .-])(?:Mug|Poster|Figure|Diorama|Merch|Product|Box|"
    r"Sticker|Card|Badge|Pendant|Keychain)(?:[_ .-]|$)",
    re.I)

# ★ 分类顺序敏感：user 必须先判
#   实测踩坑：
#     Shizukababy.jpg  → 是使者静津芽芽本体图，却因不含 User 关键字落到 misc
#     Jojo_×_Graniph_Stand_Users_Shirt → 含 "Users" 被误判成使者图（实为周边T恤）
#   → 所以：
#     先把周边排除；使者图靠「条目名/User/角色名」双通道识别。
_KIND_PATTERNS = [
    # ---- 替身使者 ----
    # 通道a：文件名里含 User / Portrait / 角色特征词
    ("user", re.compile(
        r"[_.]User[_.]|User_(?:Portrait|Avatar|Image)|"
        r"Stand_Users?_(?:19|20)\d\d|"          # Jojo_×_Graniph_Stand_Users_...
        r"Portrait|Fullbody|Character", re.I)),
    # 通道b：条目主角色图（jojowiki 惯用命名：<角色名>.jpg，
    #        如 Shizukababy.jpg 对应静津芽芽）
    #        —— 由调用方用 stand 的 owner 名字补充判断
    # ---- 替身本体 ----
    ("stand", re.compile(r"Infobox|Sweep|Stand_Image|[_.]Stand[_.]|"
                         r"Appearance|Closeup", re.I)),
    # ---- 漫画截图 ----
    ("manga", re.compile(r"Cover|Gallery|Manga|Screenshot|Panel|"
                         r"Chapter|Volume|Page", re.I)),
    # ---- 动画截图 ----
    ("anime", re.compile(r"Anime|ASB|EOH|ASBR", re.I)),
]


def classify(fname: str, stand_en: str = "", owner_hint: str = ""
             ) -> str:
    """按文件名判类别；命中不了返回 misc。

    ★ owner_hint：替身使者的名字片段（如 "Shizuka"、"Jotaro"）。
      jojowiki 的使者图常直接用角色名命名（Shizukababy.jpg），
      光看 User 关键字抓不到 → 用使者名兜底。

    ★ GIF 一律排除：实测 GE_MorningGlories.gif 有 **3 MB**
      （万帧动画序列），当静态图展示会拖垮页面首屏。
      这类文件在 jojowiki 里用 GIF 存「多帧立绘序列」，信息价值低。
    """
    if fname.lower().endswith(".gif"):
        return "noise"
    if _NOISE.search(fname):
        return "noise"
    for kind, pat in _KIND_PATTERNS:
        if pat.search(fname):
            return kind
    # 兜底：文件名里含使者名片段→ user
    #★踩坑：owner_name 可能是 "Shizuka Joestar"（多词），
    #     整串扁平化成 "shizukajoestar" 再去匹配 "shizukababy.jpg"
    #     → 永远匹配不上（实测38 张里Shizukababy.jpg 落到 misc）。
    #   → 改成「逐个名字片段，任一命中即可」，
    #      且片段要够长（>=4 字母）以免Jotaro 之类短名乱命中。
    if owner_hint:
        flat = re.sub(r"[^a-z]", "", fname.lower())
        for token in re.findall(r"[A-Za-z]{4,}", owner_hint):
            key = token.lower()
            if key in flat:
                return "user"
    return "misc"


def owner_page_candidates(owner_raw: str) -> list[str]:
    """把 owner_name 转成 jojowiki 角色页候选名（按可能性排序）。

    ★ 为什么需要这个 ★★
    实测：**替身条目页里通常没有使者立绘**
      Star_Platinum 页52 张图里含 Jotaro 的0 张；
      替身使者立绘在**角色页**（Jotaro_Kujo / Giorno_Giovanna /
      Shizuka_Joestar，各有 35~82 张图且含Infobox 立绘）。
    → 用户明确要「替身使者的图片」，必须去角色页抓。

    ★ owner_name 形态很杂（实测 138 个唯一值）★★
      "Caravan Serai (Originally)Chaka (Possessed)Four Unnamed mice"
      "Anjuro \"Angelo\" Katagiri"   "Benjamin Boomboom (1)Andre Boomboom (2)"
      "Bug-EatenNot Bug-Eaten"      "Akira Otoishi"（正常形态）
    → 逐级降级尝试：取第一段 → 去括号 → 去引号 → 空格换下划线
    """
    if not owner_raw:
        return []
    s = owner_raw.split(",")[0].strip()
    # 先截掉括号段（"Caravan Serai (Originally)..." → "Caravan Serai"）
    s = re.split(r"[(\[]", s)[0].strip()
    cands: list[str] = []
    for variant in (s, s.replace('"', "").replace("'", "")):
        v = variant.strip()
        if not v:
            continue
        # 空格 → 下划线（jojowiki 用 Jotaro_Kujo）
        page = re.sub(r"\s+", "_", v)
        if page and page not in cands:
            cands.append(page)
    # 再试"只取最后一个词"（应对 "Bug-EatenNot Bug-Eaten" 这类粘连）
    parts = re.split(r"[_\s]+", cands[0]) if cands else []
    if len(parts) >= 2 and parts[-1] not in cands:
        cands.append(parts[-1])
    return cands[:3]


# ★ 角色页里哪些文件算「使者立绘」
_USER_PAT = re.compile(
    r"Infobox|Portrait|Face|Fullbody|Character[_.]|"
    # 反例：替身页里 user 类常混进场次图，角色页里同样有噪声
    r"(?<!powa)(?<!Star)",re.I)


def page_images_by_kind(stand: str, owner_hint: str = ""
                        ) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """抓替身页，返回 (stand_or_manga 图, 使者图)。

    ★ 使者图从**角色页**抓（见 owner_page_candidates 的说明）。
    """
    stand_imgs = page_images(stand, owner_hint)

    user_imgs: list[tuple[str, str]] = []
    for cand in owner_page_candidates(owner_hint):
        try:
            for f, u in page_images(cand, owner_hint):
                # 只取立绘类，且排除明显噪声
                if _NOISE.search(f):
                    continue
                if not re.search(r"Infobox|Portrait|Face|Fullbody", f, re.I):
                    continue
                # ★ 排除"某替身的图"（角色页也会贴替身立绘）——
                #   判据：文件名以角色名开头
                flat = re.sub(r"[^a-z]", "", cand.split("_")[0].lower())
                if flat and flat not in re.sub(r"[^a-z]", "", f.lower()):
                    continue
                if (f, u) not in user_imgs:
                    user_imgs.append((f, u))
            if user_imgs:      # 命中就停，不继续试下一个候选
                break
        except Exception:
            continue
    return stand_imgs, user_imgs


def page_images(stand: str, owner_hint: str = "") -> list[tuple[str, str]]:
    """抓条目页，返回 [(文件名, thumbURL)]（已去重、已滤噪声）。

    ★ URL 结构（实测）：
        https://static.jojowiki.com/images/thumb/b/b8/latest/20260218064129/
            Achtung_Baby_Infobox_Manga.png/400px-Achtung_Baby_Infobox_Manga.png
                    ^^^^^^thumb 路径第1段  ^^^^第2段  ^^^^^^版本/时间戳 ^^^^^^文件名
      原图（无 thumb）则是：
        https://static.jojowiki.com/images/b/b8/latest/.../Achtung_Baby_....png
    ★★ 踩坑（匹配率 0/58 → 58/58）★★
      MediaWiki 图片路径的哈希前缀是 **1 + 2** 字符，不是 2 + 2：
          /images/thumb/b/b8/latest/...← 第一段 'b' 只有 1 个字符
      且`latest/<时间戳>/` 是**两段**，漏掉任一段都匹配不上。
      → 正确：/images/thumb/[0-9a-f]/[0-9a-f]{2}/[^/]+/[0-9]+/(fname)/(px)px-
    """
    url = f"https://jojowiki.com/{urllib.parse.quote(stand.replace(' ', '_'))}"
    html = fetch(url).decode("utf-8", "ignore")
    pat = re.compile(r"/images/(?:thumb/)?[0-9a-f]/[0-9a-f]{2}/"
                     r"[^/]+/[0-9]+/([^/]+)/([0-9]+)px-")
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for src in re.findall(r'<img[^>]+src="([^"]+)"', html):
        if "/images/" not in src:
            continue
        m = pat.search(src)
        if not m:
            continue
        fname = m.group(1)
        if fname in seen or classify(fname, stand, owner_hint) == "noise":
            continue
        seen.add(fname)
        # 原图（>1000px）统一降级到 400px thumb —— 原图实测 6.4MB 会下崩
        if m.group(2) != str(THUMB_PX):
            base = src.split(f"/{m.group(2)}px-")[0]
            src = f"{base}/{THUMB_PX}px-{fname.replace(' ', '_')}"
        out.append((fname, src))
    return out


def save(stand_id: str, items: list[tuple[str, str, str]]) -> dict:
    """下载并落盘，返回清单。

    ★ items 形如 [(kind, fname, url)]，kind 已由 page_images 判好。
    ★ 配额策略：每类上限如下，**本体图按「版本」去重** ——
      实测 Star Platinum 有 SC/DU/SO 三个版本各1 张 Infobox，
      若只按总数截前 4 张，会全被 SC 占满、SO 版永远拿不到。
      → 同类里先按「版本标记」（SC/DU/SO/GY/BD…）各取一张，
        剩下的名额再按原顺序补。
      截图类封顶 4 张（一个替身常有几十张 Chapter 图，不能淹没重点）。
    """
    d = IMG_ROOT / stand_id
    d.mkdir(parents=True, exist_ok=True)
    quota = {"stand": 4, "user": 4, "manga": 4, "anime": 3, "misc": 2}
    used: dict[str, int] = {}
    kept: list[tuple[str, str, str]] = []
    order = {"stand": 0, "user": 1, "anime": 2, "manga": 3, "misc": 4}

    # ★ 同类里按版本标记各留一张
    VERSION_RE = re.compile(
        r"_(SC|DU|SO|GY|BD|EH|PP|AC|DC|GZA|DCC)(?:_|$)", re.I)

    by_kind: dict[str, list] = {}
    for kind, fname, url in items:
        if kind == "noise":
            continue
        by_kind.setdefault(kind, []).append((kind, fname, url))

    for kind in sorted(by_kind, key=lambda k: order.get(k, 9)):
        lst = by_kind[kind]
        cap = quota.get(kind, 2)
        if len(lst) <= cap:
            kept.extend(lst)
            continue
        # 先每个版本各取一张
        picked: list = []
        seen_ver: set[str] = set()
        for it in lst:
            vm = VERSION_RE.search(it[1])
            ver = (vm.group(1).upper() if vm
                   else re.sub(r"[^a-z]", "", it[1].lower())[:12])
            if ver not in seen_ver:
                seen_ver.add(ver)
                picked.append(it)
            if len(picked) >= cap:
                break
        # 剩余名额按原顺序补
        for it in lst:
            if len(picked) >= cap:
                break
            if it not in picked:
                picked.append(it)
        kept.extend(picked)
        used[kind] = len(picked)

    manifest = {
        "stand_id": stand_id,
        "images": [],
        "source": "jojowiki.com",
        "note": "图片版权归荒木飞吕彦 / 集英社所有，此处仅作技术演示",
    }
    counters: dict[str, int] = {}
    for kind, fname, url in kept:
        counters[kind] = counters.get(kind, 0) + 1
        ext = ".jpg" if fname.lower().endswith((".jpg", ".jpeg")) else ".png"
        local = f"{kind}_{counters[kind]}{ext}"
        try:
            data = fetch(url)
        except Exception as e:  # noqa: BLE001
            print(f"      跳过 {fname[:40]}：{str(e)[:44]}")
            continue
        # ★ 体积保险：超过 1.5 MB 一律不存。
        #   400px thumb 正常是 100-600 KB；
        #   实测偶有 3 MB 的（多帧 GIF / 未缩放大图），
        #   塞进 GUI 会明显拖慢首屏。
        if len(data) > 1_500_000:
            print(f"      跳过 {fname[:40]}：{len(data)//1024} KB 过大")
            continue
        (d / local).write_bytes(data)
        manifest["images"].append({
            "kind": kind, "file": local, "bytes": len(data),
            "source_name": fname,
            "source_page": f"https://jojowiki.com/{stand_id.replace('_', ' ')}",
        })
        print(f"      [{kind:5s}] {local:12s} {len(data)//1024:>5d} KB  "
              f"{fname[:46]}")
    (d / "_images.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def slug(stand_en: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", stand_en.lower()).strip("_")


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    print("=" * 66)
    print("抓取替身图片（M17）")
    print("=" * 66)

    owner_map: dict[str, str] = {}
    if "--from-stands" in sys.argv or not args:
        stands = json.loads(
            (ROOT / "dataset" / "processed" / "stands.json")
            .read_text(encoding="utf-8"))
        if args:      # 显式给了名字 →只处理这些，但owner 映射仍取全量
            want = {slug(a.replace("_", " ")) for a in args}
            targets = [s["name_en"] for s in stands
                       if slug(s["name_en"]) in want] or args
        else:
            targets = [s["name_en"] for s in stands]
            print(f"  按 stands.json 全量抓取：{len(targets)} 个替身")
            print("  ★ 每个约 10 张、每张约 5 秒 → 全量很久，建议分批")
        # ★ 替身使者名（用于识别「Shizukababy.jpg」这类以角色名命名的图）
        for s in stands:
            o = (s.get("owner_name") or "").split(",")[0].strip()
            owner_map[slug(s["name_en"])] = o
    else:
        targets = args
        # 从 stands.json 补owner 映射
        try:
            stands = json.loads(
                (ROOT / "dataset" / "processed" / "stands.json")
                .read_text(encoding="utf-8"))
            for s in stands:
                o = (s.get("owner_name") or "").split(",")[0].strip()
                owner_map[slug(s["name_en"])] = o
        except Exception:
            pass

    ok = 0
    for i, en in enumerate(targets, 1):
        sid = slug(en)
        owner = owner_map.get(sid, "")
        print(f"\n[{i}/{len(targets)}] {en}  -> images/{sid}/"
              f"{f'  (使者 {owner})' if owner else ''}")
        try:
            # ★ 双来源：替身页取本体/漫画图，角色页取使者立绘
            stand_pairs, user_pairs = page_images_by_kind(en, owner)
            if not stand_pairs and not user_pairs:
                print("      未发现可用图片")
                continue
            items = [(classify(f, en, owner), f, u)
                     for f, u in stand_pairs]
            # 使者图强制标为 user（不靠文件名猜，角色页的就是使者）
            items += [("user", f, u) for f, u in user_pairs]
            items = [x for x in items if x[0] != "noise"]
            print(f"      替身页 {len(stand_pairs)} 张 + 角色页使者 "
                  f"{len(user_pairs)} 张（可用 {len(items)}）")
            m = save(sid, items)
            if m["images"]:
                ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"      ERROR {str(e)[:90]}")
        time.sleep(1.5)

    print(f"\n完成：{ok}/{len(targets)} 个替身已存图 -> {IMG_ROOT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
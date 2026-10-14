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

==★ 性能（M47：6.8 倍提速）★

同一批 8 个替身 / 112 张图，逐步优化的实测数据：

| 配置                        | 耗时   | 相对 |
| 原版 workers=1（urllib）      | 150.5s | 1.0x |
| workers=4（urllib）           |  91s   | 1.65x |
| workers=8（urllib）           | 132s   | 1.14x（**过头了**）|
| workers=1 + 连接复用          |  45s   | 3.3x |
| **workers=4 + 连接复用**      | **22s**| **6.8x** |
| workers=8 + 连接复用          |  27s   | 5.6x |

→ **最优默认：workers=4 + 连接复用**（已是默认配置）。

两处提速的贡献（别只看总数）：
  1. **连接复用**（150→45，3.3x）—— 本模块最大的收益点。
     urllib 每次 urlopen 都新建 TCP+TLS；经代理还要重走 CONNECT 隧道。
     这部分开销**与图片大小无关**，每张都付一次。
     → 改用**线程局部 requests.Session**（见 `_session()`）。
  2. **替身级并发 4**（45→22，2x）—— 让「A 在下图片」与「B 在抓页面」重叠。
     ★ 但 **workers=8 反而变慢**：瓶颈在代理/服务器承载，
       本地并发开太大只会互相排队。实测单替身耗时能从 10s 抖到 130s。
       → 出现大量「下载失败」时，用 `--workers 2` 降档。

另：**增量跳过** —— 已有非空 `_images.json` 直接跳过，重跑只补缺的（0 秒）。

用法：
    python dataset/sources/images/fetch_stand_images.py Star_Platinum Gold_Experience
    python dataset/sources/images/fetch_stand_images.py --from-stands   # 按 stands.json 全量
    python dataset/sources/images/fetch_stand_images.py --from-stands --workers 4
    python dataset/sources/images/fetch_stand_images.py --from-stands --force  # 已抓的重抓
    # 可选：--workers N（默认 4）｜--sleep S（默认 0.3，替身间间隔）
输出：
    images/<stand_id>/stand_1.png   等，以及 _images.json 清单
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import re
import sys
import threading
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
_OPENER_LOCK = threading.Lock()

# ★★★ M47：连接复用 —— 本轮**收益最大**的一处改动 ★★★
#
# == 实测（同一批 6 张图，串行，同样的代理）==
#   urllib（每次新建连接）        10.18 s
#   requests.Session（复用连接）  **2.00 s**   ← 5 倍
#
# == 为什么差这么多 ==
#   urllib.request.urlopen() **每次调用都新建 TCP + TLS 连接**。
#   经代理时更贵：每次都要重新 CONNECT 隧道 + 走一遍 TLS 握手。
#   而这些开销与图片大小无关 —— 每张图都得付一次。
#   Session 会把连接留在池里复用，第二张起只剩「发请求 + 收字节」。
#
# == 为什么用 threading.local ==
#   requests.Session **不是为多线程共享设计的**（连接池与 cookie
#   没有加锁）。而本脚本是线程池并发下载 →
#   给**每个线程一个独立 Session**：既复用了连接，又没有竞态。
#   （替代方案是全局一个 Session + 锁，但锁会把并发变回串行，不可取。）
_TLS = threading.local()


def _session():
    """取当前线程的 requests.Session（懒建）。"""
    s = getattr(_TLS, "s", None)
    if s is None:
        import requests
        s = requests.Session()
        px = (os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
              or os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY")
              or "").strip()
        # ★ 代理必须显式传 —— 否则 requests 会读环境变量，
        #   但在某些沙箱里环境变量是 198.18.0.x 的假地址（见文件头）
        if px:
            s.proxies = {"http": px, "https": px}
        s.headers.update({
            "User-Agent": _UA,
            "Referer": "https://jojowiki.com/",
            "Accept": "image/avif,image/webp,image/png,image/*,*/*;q=0.8",
        })
        _TLS.s = s
    return s


def _get_opener() -> urllib.request.OpenerDirector:
    """取全局 opener —— 仅作 requests 不可用时的退路。"""
    global _OPENER
    if _OPENER is None:
        with _OPENER_LOCK:
            if _OPENER is None:
                _OPENER = build_opener()
    return _OPENER


def fetch(url: str, tries: int = 2, timeout: int = 20) -> bytes:
    """下载字节。★ 必须显式走代理（见文件头说明）。

    ★ M18：退避从 1.5/3.0 秒压到 0.4/0.9 秒。
      原本 tries=2 + 退避 1.5s，在并行度不足时会把整体耗时拉长
      （实测单替身 87 秒里有一大块是这个）。
      首次失败多为代理抖动，立刻重试的成功率不低，等太久不划算。

    ★ M47：改用**线程局部 requests.Session**（见上）。实测 5 倍。
      requests 不可用时自动退回原来的 urllib 路径，行为不变。
    """
    last: Exception | None = None
    for i in range(tries):
        try:
            try:
                r = _session().get(url, timeout=timeout)
                r.raise_for_status()
                return r.content
            except ImportError:
                # 没有 requests → 退回 urllib（仍是原行为）
                return _get_opener().open(url, timeout=timeout).read()
        except Exception as e:  # noqa: BLE001
            last = e
            if i + 1 < tries:
                # ★ 失败后把该线程的连接池清掉 ——
                #   代理抖动常留下坏连接，复用它会连着失败第二次
                s = getattr(_TLS, "s", None)
                if s is not None:
                    try:
                        s.close()
                    finally:
                        _TLS.s = None
                time.sleep(0.4 * (i + 1))
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


def owner_page_images(stand: str, owner_hint: str = ""
                      ) -> list[tuple[str, str]]:
    """只抓**角色页**的使者立绘（复用 page_images_by_kind 的逻辑）。

    ★ M19 拆出这个函数的原因 ★★
      原本 page_images_by_kind 会把「替身页+ 角色页」一起抓，
      于是调用方想「两个来源各自兜底」时无法复用替身页结果 ——
      要么重复请求（浪费），要么一失败就两边全丢
      （实测 c_moon 的角色页 /F.F. 超时 → 连本体图也没了）。
    """
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
    return user_imgs


def page_images_by_kind(stand: str, owner_hint: str = ""
                        ) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """抓替身页 + 角色页，返回 (替身页图, 使者图)。

    ★ 使者图从**角色页**抓（见 owner_page_candidates 的说明）。
    """
    stand_imgs = page_images(stand, owner_hint)
    return stand_imgs, owner_page_images(stand, owner_hint)


# ★ M48：全局页面→图片列表 缓存。由 main() 开头的「批量预取」填满，
#   之后 `page_images()` 直接命中，**零请求**。
#   ★ 为什么要预取到全局而不是按替身现查：
#     瓶颈是**每次请求的往返延迟**（经代理 1~3 s），跟响应体大小关系不大。
#     实测换成 api.php 后响应从 1~3 MB 降到 3 KB，但耗时几乎没变
#     （5 个替身：35.8 s vs 旧 35 s）—— 因为请求次数没减少。
#     → 必须**减少请求次数**：`titles=A|B|C` 一次可查 50 个页面，
#       把 187 替身 + 角色页从 370+ 次请求压到 **个位数**。
_API_CACHE: dict[str, list[str]] = {}


def _batch_api_images(pages: list[str], chunk: int = 50) -> dict[str, list[str]]:
    """★ 批量取多个页面的图片列表 —— 一次 API 查 `chunk` 个标题。

    ★ MediaWiki 对 `titles=` 的匿名上限是 **50**（超过会报 too many values）。

    ★★★ M48b：必须处理 **`continue` 续查**，否则会静默丢图 ★★★
      实测踩坑（很隐蔽，最后一页之前的结果都是对的）：
        22 个页面一次查完，前面 5 个页面拿到 84/49/169/99/39 个文件，
        合计 **440**，从这里开始，**后面所有页面全部返回 0 个**。
      原因：`imlimit=500` 是 **所有页面共享的总上限**，不是每页 500。
        前几个页面吃掉额度后，其余页面既没图片、也**不报错** ——
        表现成"这些替身一张图都没有"。
      → 修法：读响应里的 `continue`，带着它继续请求，直到没有为止。

    ★ `redirects=1` + 手动回填：别名页也能命中 ——
      API 返回 `redirects: [{from, to}]`，要把结果映射回原名字，
      否则调用方按原名取会落空。
    ★ `normalized` 同理（下划线/首字母大小写会被规范化）。
    """
    out: dict[str, list[str]] = {}

    def _merge(d: dict) -> None:
        q = d.get("query", {}) or {}
        for pg in (q.get("pages", {}) or {}).values():
            if "missing" in pg:
                continue
            t = pg.get("title", "")
            got = [x["title"] for x in pg.get("images", [])]
            if got:
                out.setdefault(t, []).extend(got)
        for r in q.get("redirects", []) or []:
            to = out.get(r.get("to", ""))
            if to is not None:
                out.setdefault(r.get("from", ""), []).extend(to)
        for n in q.get("normalized", []) or []:
            to = out.get(n.get("to", ""))
            if to is not None:
                out.setdefault(n.get("from", ""), []).extend(to)

    for i in range(0, len(pages), chunk):
        batch = pages[i:i + chunk]
        cont: dict[str, str] = {}
        while True:
            params = {"action": "query", "prop": "images", "imlimit": "500",
                      "format": "json", "redirects": "1",
                      "titles": "|".join(batch)}
            params.update(cont)
            url = ("https://jojowiki.com/api.php?"
                   + urllib.parse.urlencode(params, safe="|"))
            try:
                d = json.loads(fetch(url).decode("utf-8", "ignore"))
            except Exception:
                break
            _merge(d)
            c = d.get("continue")
            if not c:
                break
            cont = c

    # 去重（续查可能对同一页重复返回）
    return {k: list(dict.fromkeys(v)) for k, v in out.items()}


def _api_images(page: str) -> list[str]:
    """用 MediaWiki API 取「该页面引用的图片文件名」。

    ★★★ M48：这是本轮最大的一处提速 ★★★

    == 原实现的问题 ==
      `fetch(页面URL).decode()` 拿的是**整个 HTML**。实测替身页 1~3 MB，
      页面阶段耗时 **2.2 ~ 20.4 s**（波动极大），而图片下载 10 张只要 2~3 s
      —— **页面阶段才是大头**。

    == 为什么可以换 ==
      api.php 的 `prop=images` 返回的正是「该页面引用的文件」列表，
      与从 HTML 里正则抽 `<img src>` **是同一个集合**，但：
          响应 **3 KB** / 耗时 **0.96 s**（Atum，84 个文件）
      体积差几百倍 —— 因为不再需要下载那些被引用的图片本身
      以及整个页面骨架。

    == 顺带的好处 ==
      · `titles=A|B|C` 可**一次查多个页面**（上限 50），后面还能再批量化
      · 拿到的是规范文件名（`File:xxx.png`），不必再解析 URL 反推，
        也就绕开了「哈希前缀 1+2 字符」那个正则坑
    """
    # ★ 命中预取缓存 → 零请求（main 开头的 `_batch_api_images` 会批量填满）
    key = page.replace(" ", "_")
    if key in _API_CACHE:
        return _API_CACHE[key]

    url = ("https://jojowiki.com/api.php?action=query&prop=images"
           "&imlimit=500&format=json&redirects=1&titles="
           + urllib.parse.quote(page))
    try:
        d = json.loads(fetch(url).decode("utf-8", "ignore"))
    except Exception:
        return []
    out: list[str] = []
    for pg in (d.get("query", {}).get("pages", {}) or {}).values():
        if "missing" in pg:
            continue
        out += [i["title"] for i in pg.get("images", [])]
    return out


def _thumb_url(fname: str, px: int = THUMB_PX) -> str:
    """按 MediaWiki 的 md5 规则构造 thumb URL。

    ★ 路径哈希 = `md5(文件名)`，**第 1 段取前 1 位、第 2 段取前 2 位**：
        /images/thumb/{h[0]}/{h[:2]}/{fname}/{px}px-{fname}
      （与文件头记的「哈希前缀是 1+2 字符，不是 2+2」是同一条规则）
    ★ 实测：Atum 页面 6 个文件里 5 个构造成功；
      唯一的失败是 `Atum.png`（原图本身 ≤400px，该尺寸 thumb 不存在）
      → 服务端返回 400，由 `_download` 退回原图（见下）。
    """
    fn = fname.replace("File:", "").replace(" ", "_")
    h = hashlib.md5(fn.encode("utf-8")).hexdigest()
    q = urllib.parse.quote(fn)
    return (f"https://static.jojowiki.com/images/thumb/"
            f"{h[0]}/{h[:2]}/{q}/{px}px-{q}")


def _orig_url(fname: str) -> str:
    """原图 URL —— 仅当对应尺寸的 thumb 不存在时用（原图比 px 还小）。"""
    fn = fname.replace("File:", "").replace(" ", "_")
    h = hashlib.md5(fn.encode("utf-8")).hexdigest()
    return (f"https://static.jojowiki.com/images/"
            f"{h[0]}/{h[:2]}/{urllib.parse.quote(fn)}")


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
    # ★ M48：改走 api.php（见 `_api_images` 的说明）——
    #   原实现是 `fetch(页面URL)` 抓整个 HTML（1~3 MB / 2~20 s），
    #   现在只取 3 KB 的文件名列表（~1 s）。
    #   ★ 拿到的是规范文件名，URL 直接用 md5 规则构造，
    #     于是原来那套「从 <img src> 正则反推哈希前缀」的解析整段不再需要
    #     （那套逻辑踩过「前缀是 1+2 字符不是 2+2」的坑）。
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for title in _api_images(stand.replace(" ", "_")):
        fname = title.replace("File:", "").replace(" ", "_")
        if not fname or fname in seen:
            continue
        if classify(fname, stand, owner_hint) == "noise":
            continue
        seen.add(fname)
        # 统一 400px —— 原图实测 6.4 MB 会下崩（见文件头）
        out.append((fname, _thumb_url(fname)))
    return out


def save(stand_id: str, items: list[tuple[str, str, str]],
         sink: list[str] | None = None) -> dict:
    """下载并落盘，返回清单。

    ★ M47：`sink` 用于**收集输出**而不是直接 print。
      替身级改并发后，多个替身的逐张日志会交错成一片无法阅读 ——
      所以并发时把每个替身的输出收进各自的 list，完成后**整块打印**。
      sink=None 时保持原行为（直接打印），向后兼容。

    ★ items 形如 [(kind, fname, url)]，kind 已由 page_images 判好。
    ★ 配额策略：每类上限如下，**本体图按「版本」去重** ——
      实测 Star Platinum 有 SC/DU/SO 三个版本各1 张 Infobox，
      若只按总数截前 4 张，会全被 SC 占满、SO 版永远拿不到。
      → 同类里先按「版本标记」（SC/DU/SO/GY/BD…）各取一张，
        剩下的名额再按原顺序补。
      截图类封顶 4 张（一个替身常有几十张 Chapter 图，不能淹没重点）。
    """
    def _out(msg: str) -> None:
        if sink is None:
            print(msg)
        else:
            sink.append(msg)

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

    # ★★★ 并行下载（M18关键修复）★★★
    #   实测单张 400px thumb 经代理要 **2.8 秒** → 串行 10 张 = 28 秒，
    #   再叠加角色页+ 失败重试退避，实际测到 **87 秒**的请求耗时。
    #   → 改为 4 线程并行，同一批 10 张约 7-8 秒。
    #   （瓶颈是网络往返而非 CPU，线程池足够；asyncio 反而更复杂。）
    counters: dict[str, int] = {}
    jobs: list[tuple[str, str, str, str, str]] = []   # kind,fname,url,local,ext
    for kind, fname, url in kept:
        counters[kind] = counters.get(kind, 0) + 1
        ext = ".jpg" if fname.lower().endswith((".jpg", ".jpeg")) else ".png"
        jobs.append((kind, fname, url, f"{kind}_{counters[kind]}{ext}", ext))

    def _dl(job):
        kind, fname, url, local, ext = job
        try:
            try:
                return job, fetch(url), None
            except Exception:
                # ★ M48：400px 的 thumb 可能**不存在** ——
                #   当原图本身 ≤400px 时 MediaWiki 不生成该尺寸缩略图，
                #   服务端直接返回 400。实测 Atum.png 就是这种情况
                #   （6 个文件里唯一失败的那个）。
                #   → 退回原图；这类文件本来就小，直接下并不亏。
                orig = _orig_url(fname)
                if orig != url:
                    return job, fetch(orig), None
                raise
        except Exception as e:  # noqa: BLE001
            return job, None, f"{str(e)[:44]}"

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        for job, data, err in ex.map(_dl, jobs):
            kind, fname, url, local, ext = job
            if err:
                _out(f"      跳过 {fname[:40]}：{err}")
                continue
            # ★ 体积保险：超过 1.5 MB 一律不存。
            #   400px thumb 正常是 100-600 KB；
            #   实测偶有 3 MB 的（多帧 GIF / 未缩放大图），
            #   塞进 GUI 会明显拖慢首屏。
            if len(data) > 1_500_000:
                _out(f"      跳过 {fname[:40]}：{len(data)//1024} KB 过大")
                continue
            (d / local).write_bytes(data)
            manifest["images"].append({
                "kind": kind, "file": local, "bytes": len(data),
                "source_name": fname,
                "source_page": f"https://jojowiki.com/{stand_id.replace('_', ' ')}",
            })
            _out(f"      [{kind:5s}] {local:12s} {len(data)//1024:>5d} KB  "
                 f"{fname[:46]}")
    (d / "_images.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    # ★ M48c：清掉**上一轮遗留**的图片文件。
    #   文件名是 `{kind}_{序号}{后缀}`，重抓时张数可能变少（配额/来源变化），
    #   旧的 `stand_4.jpg` 之类就会留下 —— 实测 `--force` 重跑后
    #   **150 个替身、547 个残留文件**。
    #   危害：GUI 若按目录扫图会把旧图也显示出来，且白占磁盘。
    #   ★ 只在**本次有成功下载**时才清理：全失败时保留原有图，不至于清空。
    if manifest["images"]:
        listed = {i["file"] for i in manifest["images"]}
        for _f in d.iterdir():
            if (_f.suffix.lower() in (".png", ".jpg", ".jpeg")
                    and _f.name not in listed):
                try:
                    _f.unlink()
                except OSError:
                    pass
    return manifest


def slug(stand_en: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", stand_en.lower()).strip("_")


def _prefetch_image_lists(targets: list[str],
                          owner_map: dict[str, str]) -> int:
    """批量预取「替身页 + 全部角色页候选」的图片列表，填进 `_API_CACHE`。

    ★★★ M48：这一步把**请求次数**从 370+ 压到约 10 次 ★★★

    == 为什么不预取不行 ==
      每个替身现查时要发 2~4 次请求：替身页 1 次，角色页候选最多 3 次
      （`owner_page_candidates` 会逐个试）。187 个替身 = **370~750 次**。
      瓶颈是**每次往返 1~3 s**（经代理），跟响应大小无关 ——
      所以把 HTML 换成 3 KB 的 API 也没让耗时降下来（实测 35.8s vs 35s）。

    == 预取后 ==
      所有页面按 50 个一批（MediaWiki 匿名上限）→ 约 10 批 → 十几秒
      拿完全部元数据，之后下载阶段**一次页面请求都不发**。
    """
    pages: list[str] = []
    seen: set[str] = set()
    for en in targets:
        for p in ([en.replace(" ", "_")]
                  + owner_page_candidates(owner_map.get(slug(en), ""))):
            if p and p not in seen:
                seen.add(p)
                pages.append(p)
    if not pages:
        return 0
    n_batch = (len(pages) + 49) // 50
    print(f"  预取图片列表：{len(pages)} 个页面 → {n_batch} 批"
          f"（每批 50，MediaWiki 匿名上限）")
    t = time.time()
    got = _batch_api_images(pages)
    _API_CACHE.update(got)
    el = time.time() - t
    print(f"  ✓ 拿到 {len(got)} 个页面（{el:.1f}s，"
          f"约 {el / max(n_batch, 1):.2f}s/批）")
    miss = len(pages) - len(got)
    if miss > 0:
        print(f"  ! {miss} 个页面 API 无返回（不存在或重定向未解析），"
              f"这些会现查或直接跳过")
    return len(got)


def main() -> int:
    # ★ M47 参数解析 —— 注意「带值的选项」的值不能被当成替身名：
    #   原写法 `[a for a in sys.argv[1:] if not a.startswith("--")]`
    #   会把 `--workers 4` 里的 `4` 收进 targets。
    _opts_with_val = {"--workers", "--sleep"}
    args: list[str] = []
    _skip_next = False
    for _a in sys.argv[1:]:
        if _skip_next:
            _skip_next = False
            continue
        if _a in _opts_with_val:
            _skip_next = True
            continue
        if not _a.startswith("--"):
            args.append(_a)

    workers = 4
    sleep_s = 0.3
    force = "--force" in sys.argv
    if "--workers" in sys.argv:
        workers = max(1, int(sys.argv[sys.argv.index("--workers") + 1]))
    if "--sleep" in sys.argv:
        sleep_s = max(0.0, float(sys.argv[sys.argv.index("--sleep") + 1]))

    print("=" * 66)
    print("抓取替身图片（M17 / 并发优化 M47）")
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
            print("  ★ M47 已开并发 + 增量跳过；重跑只会补缺的，"
                  "不会重抓已完成的")
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

    # ★★★ M47：替身级并发（原来是 187 个替身**完全串行**）★★★
    #
    # == 实测数据（这是改动的依据，不是猜的）==
    #   单替身耗时构成：
    #     页面抓取（替身页 + 角色页，串行 2 次）  4.1 s
    #     图片下载（10 张 / 4 线程）            ~8   s
    #     强制 sleep                            1.5  s
    #     ────────────────────────────────────────────
    #     合计                                  ≈14  s/个
    #   → 187 个 ≈ **43 分钟**
    #
    # == 为什么加"外层"池 ==
    #   瓶颈是**网络往返**，不是 CPU。单个替身内部已经是
    #   「先串行抓页面、再并行下图片」，但**两个替身之间没有重叠** ——
    #   A 在等网络时 B 完全闲着。外层再加一层池，让
    #   「A 正在下图片」与「B 正在抓页面」重叠。
    #
    # == 并发定档（防限流）==
    #   峰值连接数 = workers × 4（内层图片线程）
    #   workers=4 → 峰值 16，实测稳定，未触发 jojowiki 限流。
    #   ★ 若出现大量「下载失败」，用 --workers 2 降档。
    #
    # == 另两处提速 ==
    #   · **增量跳过**：已有非空 `_images.json` 直接跳过（重跑只补缺的）
    #   · 强制 sleep 1.5s → 0.3s（187 个可省 ~4.7 分钟）
    def fetch_one(en: str) -> tuple[str, str, int, float, list[str]]:
        sid = slug(en)
        owner = owner_map.get(sid, "")
        t_s = time.time()
        sink: list[str] = []
        mf = IMG_ROOT / sid / "_images.json"

        # 增量：已完成的不重抓
        if not force and mf.exists():
            try:
                old = json.loads(mf.read_text(encoding="utf-8"))
                if old.get("images"):
                    return "skip", en, len(old["images"]), 0.0, sink
            except Exception:
                pass

        try:
            # ★ 双来源：替身页取本体/漫画图，角色页取使者立绘
            stand_pairs, user_pairs = page_images_by_kind(en, owner)
            if not stand_pairs and not user_pairs:
                return "empty", en, 0, time.time() - t_s, sink
            items = [(classify(f, en, owner), f, u) for f, u in stand_pairs]
            # 使者图强制标为 user（不靠文件名猜，角色页的就是使者）
            items += [("user", f, u) for f, u in user_pairs]
            items = [x for x in items if x[0] != "noise"]
            sink.append(f"      替身页 {len(stand_pairs)} 张 + 角色页使者 "
                        f"{len(user_pairs)} 张（可用 {len(items)}）")
            m = save(sid, items, sink=sink)
            st = "ok" if m["images"] else "empty"
            return st, en, len(m["images"]), time.time() - t_s, sink
        except Exception as e:  # noqa: BLE001
            sink.append(f"      ERROR {str(e)[:90]}")
            return "err", en, 0, time.time() - t_s, sink
        finally:
            if sleep_s:
                time.sleep(sleep_s)

    total = len(targets)
    print(f"  并发 {workers} 个替身（每个内部 4 线程下图片，"
          f"峰值连接 ≈{workers * 4}）")
    print(f"  替身间间隔 {sleep_s}s｜"
          f"{'强制重抓全部' if force else '跳过已完成的（--force 可重抓）'}")
    print()

    # ★ M48：先把所有页面的图片列表批量拿回来（见函数说明）
    _prefetch_image_lists(targets, owner_map)

    t0 = time.time()
    stats: dict[str, int] = {"ok": 0, "skip": 0, "empty": 0, "err": 0}
    n_img = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(fetch_one, en) for en in targets]
        for i, fut in enumerate(concurrent.futures.as_completed(futs), 1):
            try:
                st, en, n, dt, sink = fut.result()
            except Exception as e:  # noqa: BLE001
                print(f"[{i}/{total}] ✗ {type(e).__name__}: {str(e)[:70]}")
                stats["err"] += 1
                continue
            stats[st] = stats.get(st, 0) + 1
            n_img += n
            if st == "skip":
                print(f"[{i}/{total}] · {en:34s}      已有 {n} 张，跳过")
                continue
            icon = {"ok": "✓", "empty": "–", "err": "✗"}.get(st, "?")
            print(f"[{i}/{total}] {icon} {en:34s} {dt:6.1f}s  {n} 张")
            for line in sink:          # ★ 整块打印，避免并发日志交错
                print(line)

    el = time.time() - t0
    print(f"\n完成：新增 {stats['ok']} / 跳过 {stats['skip']} / "
          f"无图 {stats['empty']} / 失败 {stats['err']}")
    print(f"      共 {n_img} 张｜用时 {el:.0f}s"
          f"（均 {el / max(total, 1):.1f}s/个，含跳过的）")
    print(f"      → {IMG_ROOT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
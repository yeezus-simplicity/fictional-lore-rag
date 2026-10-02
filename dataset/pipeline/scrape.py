"""
抓取层：从 jojowiki 抓取替身主表与详情页。

实测结构（2026-10-02 探查）：

1. 主表 https://jojowiki.com/Stand_Stats
   <table> 第一个，158行 × 7 列（表头 + 157 条）
   列：Stand / PWR / SPD / RNG / STA / PRC / DEV

2. 详情页 https://jojowiki.com/<Stand_Name>
   - 正文段落 <p> 全部为空（长文本段落由JS 加载），**但 h3+div 结构有内容**
   - **标量字段（User/Type/Origin 等）**：<h3>标题</h3> 后紧跟的<div>
     例：<h3>User</h3><div>Jotaro Kujo</div>
   - **形态数值组**：<td class="pi-data-value" data-source="destpower"> 等
     同一 data-source 出现多次 → 对应不同形态
     例：Star Platinum 有 3 组（基础 / The World / 觉醒后）
   - 详情页的价值：标量字段 + **形态链**（正好解释主表的源内重复登记）

依赖：lxml（必须用 lxml 解析 infobox）
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

from lxml import html as LH

from encode import STAT_DIMS, make_stand_id, normalize

# ------------------------------------------------------------------
# 配置
# ------------------------------------------------------------------

BASE = "https://jojowiki.com"
MAIN_TABLE_URL = f"{BASE}/Stand_Stats"
UA = "Mozilla/5.0 (research; educational; rag-kb project)"

# 映射：主表列名 → 六维
COL_TO_DIM = {"PWR": "PWR", "SPD": "SPD", "RNG": "RNG",
              "STA": "STA", "PRC": "PRC", "DEV": "DEV"}

# 映射：infobox data-source → 六维
DATA_SOURCE_TO_DIM = {
    "destpower": "PWR",
    "speed": "SPD",
    "range": "RNG",
    "stamina": "STA",
    "precision": "PRC",
    "potential": "DEV",
}

# infobox 里的 h3 标量字段 → 目标字段名
# 实测：<h3>User</h3><div>Jotaro Kujo</div>
H3_FIELDS = {
    "japanese name": "name_ja",
    "romanized name": "name_romaji",
    "user": "owner",
    "namesake": "reference",
    "type": "stand_type",
    "origin": "origin",
    "awakening": "awakening",
    "manga debut": "manga_debut",
    "anime debut": "anime_debut",
    "first named": "first_named",
    "rush attack": "rush_attack",
}

# h3 标题里的噪声（wiki 自身的编辑提示）
H3_NOISE = ("citation", "without a citation", "not confirmed")

DATA_DIR = Path(__file__).resolve().parents[1] / "sources"


# ------------------------------------------------------------------
# HTTP
# ------------------------------------------------------------------

def fetch(url: str, *, retries: int = 3, delay: float = 1.0,
          timeout: int = 30) -> Optional[str]:
    """带重试与限速的 GET。

    Args:
        retries: 失败重试次数
        delay: 每次请求后的间隔（对 157 次详情抓取必须限速）

    Returns:
        HTML 文本；全部失败返回 None
    """
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            # jojowiki 为 UTF-8，但兜底 latin-1（实测存在非 UTF-8 片段）
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                return raw.decode("latin-1")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt == retries:
                print(f"    [FAIL] {url} -> {type(e).__name__}: {e}")
                return None
            wait = delay * (2 ** (attempt - 1))
            print(f"    [retry {attempt}/{retries}] {type(e).__name__}, "
                  f"等待 {wait:.1f}s")
            time.sleep(wait)
    return None


# ------------------------------------------------------------------
# 数据结构
# ------------------------------------------------------------------

@dataclass
class MainRow:
    """主表一行（未编码）。"""

    name_raw: str
    stand_id: str
    values: dict[str, str]        # dim -> 原始字面量
    row_index: int


@dataclass
class FormEntry:
    """详情页提取的一个形态数值组。"""

    form_label: str                # 形态标签（从 group 推断）
    values: dict[str, str]         # dim -> 字面量
    raw_order: int                 # 在页面中的出现顺序


@dataclass
class DetailPage:
    """详情页提取结果。"""

    stand_id: str
    name_raw: str
    url: str
    infobox: dict[str, str] = field(default_factory=dict)
    forms: list[FormEntry] = field(default_factory=list)
    form_count: int = 0

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


# ------------------------------------------------------------------
# 主表抓取
# ------------------------------------------------------------------

def parse_main_table(html_text: str) -> list[MainRow]:
    """解析 Stand_Stats 主表。

    严格校验每行 7 列；不足则跳过并计数。
    """
    root = LH.fromstring(html_text)
    tables = root.xpath("//table")
    if not tables:
        return []
    table = tables[0]

    rows: list[MainRow] = []
    idx = 0
    for tr in table.xpath(".//tr")[1:]:
        cells = [c.text_content().strip().replace("\n", " ")
                 for c in tr.xpath("./th|./td")]
        if len(cells) != 7:
            continue
        name = cells[0]
        if not name:
            continue
        rows.append(MainRow(
            name_raw=name,
            stand_id=make_stand_id(name),
            values={COL_TO_DIM[COL_TO_DIM and k]: v
                    for k, v in zip(["PWR", "SPD", "RNG", "STA", "PRC", "DEV"],
                                    cells[1:])},
            row_index=idx,
        ))
        idx += 1
    return rows


def fetch_main_table(*, use_cache: bool = True) -> list[MainRow]:
    cache = DATA_DIR / "stand_stats_main.html"
    if use_cache and cache.exists():
        print(f"  使用缓存：{cache}")
        return parse_main_table(cache.read_text(encoding="utf-8", errors="replace"))

    print(f"  抓取：{MAIN_TABLE_URL}")
    html_text = fetch(MAIN_TABLE_URL)
    if html_text is None:
        return []
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(html_text, encoding="utf-8")
    return parse_main_table(html_text)


# ------------------------------------------------------------------
# 详情页解析
# ------------------------------------------------------------------

def _clean(text: str) -> str:
    """压缩空白并去除引用标记 [12] 与脚注字母 [a]。"""
    t = " ".join(unicodedata.normalize("NFKC", text).split())
    t = re.sub(r"\[\d+\]", "", t)          # 参考文献编号
    t = re.sub(r"\[[a-z]\]", "", t)         # 脚注字母
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def parse_detail(html_text: str, url: str) -> DetailPage:
    """解析详情页，提取 infobox 字段与形态数值组。

    关键：同一 data-source 出现多次即对应不同形态。
    形态标签从该组所属 section 的上下文推断。
    """
    root = LH.fromstring(html_text)
    h1 = root.xpath("//h1//text()")
    name_raw = _clean("".join(h1[0])) if h1 else ""
    stand_id = make_stand_id(name_raw)

    page = DetailPage(stand_id=stand_id, name_raw=name_raw, url=url)

    # --- 1. 标量字段：<h3>标题</h3> 后紧跟的 <div> ---
    for h3 in root.xpath("//h3"):
        raw_label = _clean(h3.text_content())
        if not raw_label:
            continue
        # 标题里可能混入 wiki 编辑提示，取首个词组做匹配
        label = raw_label.lower()
        for noise in H3_NOISE:
            if noise in label:
                label = label.split(noise)[0].strip()
        field = H3_FIELDS.get(label)
        if not field or field in page.infobox:
            continue
        sib = h3.getnext()
        if sib is None:
            continue
        val = _clean(sib.text_content())
        if val and len(val) < 400:
            page.infobox[field] = val

    # --- 2. 形态数值组 ---
    # 按 DOM 顺序扫描 pi-data-value，把连续 6 维视为一组
    order: list[tuple[str, str]] = []
    for td in root.xpath('//td[contains(@class,"pi-data-value")]'):
        src = td.get("data-source") or ""
        dim = DATA_SOURCE_TO_DIM.get(src)
        if not dim:
            continue
        val = _clean(td.text_content())
        if val:
            order.append((dim, val))

    groups: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for dim, val in order:
        if dim in current:
            # 同一组内维度重复出现（多形态）→ 收尾当前组
            groups.append(current)
            current = {}
        current[dim] = val
    if len(current) >= 4:                # 至少 4 维才算有效组
        groups.append(current)

    # 形态标签：section 标题不可靠（多态共表），
    # 改用「形态序号 + Type 字段」联合命名
    type_hint = page.infobox.get("stand_type", "")
    labels = _extract_form_labels(root, len(groups))
    for i, g in enumerate(groups):
        if i < len(labels) and labels[i]:
            label = labels[i]
        else:
            label = f"form_{i}"
        # Type 字段常含多形态标注，作为提示附在标签后
        page.forms.append(FormEntry(
            form_label=label,
            values=g,
            raw_order=i,
        ))
    page.form_count = len(groups)
    page.infobox["_type_hint"] = type_hint
    return page


def _extract_form_labels(root, n: int) -> list[str]:
    """推断形态标签。

    实测：形态信息不在 section 标题里，而在 Type 字段的值中
    （如 'Close-Range' / 'Range Irrelevant (Star Platinum: The World)'）。
    section 标题会混在一起无法对应，因此这里只做保守推断：
    能拿到 Type 就用它，否则留空由调用方回退到 form_N。
    """
    labels: list[str] = []
    for sec in root.xpath('//section[contains(@class,"pi-group")]'):
        heads = sec.xpath(".//th[not(contains(@class,'pi-data-label'))]")
        title = ""
        for h in heads[:1]:
            t = _clean(h.text_content())
            if t and t not in ("Destructive Power", "Speed", "Range",
                               "Stamina", "Precision", "Potential"):
                title = t
                break
        labels.append(title)
        if len(labels) >= n:
            break
    while len(labels) < n:
        labels.append("")
    return labels


def _url_variants(name_raw: str) -> list[str]:
    """生成详情页 URL 的候选形式，按优先级排列。

    实测（2026-10-02）：
      'Star Platinum'  -> Star_Platinum            200
      'Echoes (ACT1)'  -> Echoes_(ACT1)             404（带括号）
      'Echoes (ACT1)'  -> Echoes_ACT1               200（括号转下划线）
      'Foo Fighters"'  -> Foo_Fighters"             含脏字符，需剥离

    策略：先试原名，再试括号转下划线，最后试去括号。
    """
    base = re.sub(r'["“”]+$', "", name_raw).strip()
    out: list[str] = []

    def enc(s: str) -> str:
        return urllib.parse.quote(s.replace(" ", "_"), safe="_()!,'-")

    out.append(enc(base))                                  # 原名
    out.append(enc(re.sub(r"\s*\((.*?)\)\s*", r"_\1", base)))  # 括号→下划线
    out.append(enc(re.sub(r"\s*\(.*?\)\s*", "", base)))     # 去括号
    # 去重且保序
    seen: set[str] = set()
    return [u for u in out if not (u in seen or seen.add(u))]


def fetch_detail(stand_id: str, name_raw: str, *,
                 delay: float = 1.0) -> Optional[DetailPage]:
    """抓取单个详情页，失败时自动尝试 URL 变体。"""
    html_text = None
    used_url = ""
    for slug in _url_variants(name_raw):
        url = f"{BASE}/{slug}"
        html_text = fetch(url, retries=1, delay=delay)
        if html_text:
            used_url = url
            break
    if html_text is None:
        return None
    page = parse_detail(html_text, used_url)
    time.sleep(delay)
    return page


# ------------------------------------------------------------------
# 批量详情抓取
# ------------------------------------------------------------------

def fetch_all_details(stands: list[MainRow], *, delay: float = 0.8,
                      limit: Optional[int] = None) -> list[DetailPage]:
    """批量抓取详情页。

    去重：同名 stand_id 只抓一次（主表有 3 组重复登记）。
    """
    seen: dict[str, MainRow] = {}
    for s in stands:
        if s.stand_id not in seen:
            seen[s.stand_id] = s
    todo = list(seen.values())
    if limit:
        todo = todo[:limit]

    print(f"  待抓详情页 {len(todo)} 个（限速 {delay}s/次，约 "
          f"{len(todo) * delay / 60:.1f} 分钟）")

    out: list[DetailPage] = []
    for i, s in enumerate(todo, 1):
        page = fetch_detail(s.stand_id, s.name_raw, delay=delay)
        if page:
            out.append(page)
        if i % 10 == 0 or i == len(todo):
            print(f"    进度 {i}/{len(todo)}成功 {len(out)}")
    return out


def save_json(obj, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8"
    )

/**
 * 渲染抓取层：用 Playwright 渲染详情页，抽取非结构化正文。
 *
 * 背景（实测）：
 *   静态 HTML 里 #mw-content-text 的 <p> 全部为空，
 *   能力描述 / 招式列表 / 战斗表现等长文本由 JavaScript 注入。
 *   渲染后可得 25k+ 字符、26 个有效段落。
 *
 * 抽取内容（对应数据规范 §4.1 的检索单元）：
 *   ability_overview  能力概述（首段 + 概述区）
 *   move              招式条目
 *   battle_record     战斗表现（含 Manga/Chapter 引用）
 *   lore              命名出处、外观设定
 *
 * 输出：dataset/sources/rendered/<stand_id>.json
 * 用法：node render_fetch.js [--limit N] [--delay MS]
 */

const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const BASE = 'https://jojowiki.com';
const OUT_DIR = path.resolve(__dirname, '..', 'dataset', 'sources', 'rendered');
const DETAILS = path.resolve(__dirname, '..', 'dataset', 'sources', 'details.json');
const UA = 'Mozilla/5.0 (research; educational; rag-kb project)';

// ---------- 页面内抽取逻辑（跑在浏览器上下文） ----------
const EXTRACT = () => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const stripRefs = s =>
    (s || '').replace(/\[\d+\]/g, '').replace(/\[[a-z]\]/gi, '')
     .replace(/\s+/g, ' ').trim();
  // 章节条目（战斗记录），在浏览器上下文内也要能判
  const CHAPTER_ITEM_LOCAL = /^(?:[A-Za-z ]+)?\s*Chapter\s\d+\s*:|^Chapter\s\d+\s*:|Episode\s\d+/i;

  // ★ 实测结构（2026-10-03）：
  //   #mw-content-text 只有 3 个子节点，真正的正文在
  //   第一个 div.mw-parser-output 里（16k+ 字符、32 个 h2/h3）。
  //   层级：#mw-content-text > div.mw-parser-output > (h2|h3|p|ul|...)
  const outer =
    document.querySelector('#mw-content-text') ||
    document.querySelector('.mw-parser-output');
  if (!outer) return { error: 'no outer' };

  // 优先取 .mw-parser-output，退化到 #mw-content-text 本身
  let root = outer.querySelector('.mw-parser-output') || outer;
  // 排除侧栏/导航（class 含 pi- 的是 infobox，toc/nav 是导航）
  const isNoise = el => {
    const c = (el.className || '').toString();
    return /\b(pi-|toc|navbox|metadata|printfooter|thumb|caption)/i.test(c);
  };

  const allText = clean(root.innerText);
  if (allText.length < 400) return { error: 'content too short', total_len: allText.length };

  // 1. 全量段落
  //    注意：不要在 filter 回调里引用 map 的参数（作用域会失效）
  const paragraphs = [];
  const pEls = Array.from(root.querySelectorAll('p'));
  for (const el of pEls) {
    const t = clean(el.innerText);
    if (t.length > 50 && !isNoise(el)) paragraphs.push(t);
  }

  // 2. 小节切分
  //    ★实测：内容量大的页面，h2/h3 与p 可能同层，也可能 p 被包在 div 里。
  //    因此用「文档序遍历所有后代节点」的方式，遇到标题开新节。
  const sections = [];
  let cur = null;
  const push = () => { if (cur) sections.push(cur); };
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
  let node;
  while ((node = walker.nextNode())) {
    if (isNoise(node)) continue;
    const tag = node.tagName.toLowerCase();
    if (tag === 'h2' || tag === 'h3' || tag === 'h4') {
      push();
      cur = { heading: clean(node.innerText), level: tag, paras: [], lists: [] };
      continue;
    }
    // 只处理直属正文元素：p / li（避免把 div 内部的重复计入）
    if (tag === 'p') {
      // 跳过已存在于某个 li 内的 p
      if (node.closest('li')) continue;
      const t = clean(node.innerText);
      if (t.length < 40) continue;
      if (!cur) cur = { heading: '(lead)', level: 'p', paras: [], lists: [] };
      cur.paras.push(t);
    } else if (tag === 'li') {
      if (!cur) continue;
      const t = clean(node.innerText);
      if (t.length < 15) continue;
      // 只取最外层 li（避免嵌套 ul 的子项重复）
      if (node.parentElement && node.parentElement.tagName.toLowerCase() === 'li') continue;
      cur.lists.push([t]);
    }
  }
  push();

  // 3. 招式识别
  //    ★★ 实测招式在 div.techBox 里（jojowiki 专有结构），格式：
  //       Ora Ora(オラオラ, Ora Ora) Debut: Chapter 119: ...
  //       Star Finger(流星指刺スターフィンガー, Sutā Fingā, lit. "Meteor Finger Stab") Debut: ...
  //    不是 "Name : desc" 也不是 <li>，因此必须单独处理 techBox。
  const moves = [];
  const seenMove = new Set();

  // 3a. techBox —— 招式主来源
  //   ★ techBox 同时用于「其他替身引用卡片」，需过滤：
  //     引用卡片名称以引号包裹（如 '"Silver Chariot" Plus'）且无读音块；
  //     真招式的括号内必含片假名 / 罗马音 / lit. / Dial.
  const isQuotedName = s => /^[""].+[""]/.test(s.trim());
  const hasPhonetic = s => /[ぁ-んァ-ヶ一-龥]/.test(s) || /lit\.|Dial\./.test(s);

  const boxes = Array.from(root.querySelectorAll('div.techBox'));
  for (const box of boxes) {
    const raw = clean(box.innerText);
    if (raw.length < 30) continue;

    //拆字段：名称块 / Debut: 章节块
    const dm = raw.match(/^(.*?)\s*Debut:\s*(.*)$/s);
    const nameBlock = (dm ? dm[1] : raw).trim();
    const debut = dm ? clean(dm[2]) : '';

    // ★ 过滤引用卡片
    if (isQuotedName(nameBlock)) continue;

    let name = nameBlock, phonetic = null, alias = null;
    const pm = nameBlock.match(/^([^(\[]+?)\s*[\(（]([^\)）]*)[\)）]\s*(?:\[([^\]]*)\])?\s*(.*)$/);
    if (pm) {
      name = clean(pm[1]);
      const inner = clean(pm[2]);
      // inner 形如 "オラオラ, Ora Ora" 或 "流星指刺, Sutā Fingā, lit. "Meteor Finger Stab""
      const parts = inner.split(',').map(s => clean(s));
      phonetic = parts.length ? parts[0] : null;
      alias = parts.length > 1 ? parts.slice(1).join(', ') : null;
      if (pm[3]) alias = [alias, pm[3]].filter(Boolean).join(', ');
    }
    // 必须有读音块，否则不是招式条目
    if (!pm || !hasPhonetic(nameBlock)) continue;

    // 从描述文本（techBox 内 p/div）取更详细的说明
    let detail = '';
    const inner = box.querySelector('p, .techTable, div:not([class])');
    if (inner) {
      const t = clean(inner.innerText);
      if (t.length > nameBlock.length + 10) detail = t;
    }
    const key = name.toLowerCase();
    if (!name || seenMove.has(key)) continue;
    seenMove.add(key);
    moves.push({
      section: 'TECHNIQUES',
      name,
      phonetic,
      alias: alias || null,
      debut: debut || null,
      text: detail || raw,
    });
  }

  // 3b. 列表/段落型招式（兜底）："Name (Alias) : desc"
  const isToc = s => /^\d+(\.\d+)*\s/.test(s);
  for (const sec of sections) {
    const h = sec.heading || '';
    if (!/attack|move|abilit|technique|method|form/i.test(h)) continue;
    const flat = [];
    for (const lst of sec.lists) for (const it of lst) flat.push(it);
    for (const item of flat) {
      if (isToc(item)) continue;
      if (CHAPTER_ITEM_LOCAL.test(item)) continue;
      const nm = item.match(/^([A-Z][A-Za-z0-9 '\-]{1,40}?)\s*(?:\(([^)]{2,40})\))?\s*[:：]/);
      if (!nm) continue;
      const name = nm[1];
      const key = name.toLowerCase();
      if (seenMove.has(key)) continue;
      seenMove.add(key);
      moves.push({ section: h, name, phonetic: null, alias: nm[2] || null, debut: null, text: item });
    }
  }
  for (const p of paragraphs) {
    if (p.length < 120 || p.length > 1200) continue;
    const m = p.match(/^([A-Z][A-Za-z0-9 '\-]{2,40})\s*(?:\(([^)]{2,40})\))?\s*[:：]\s*(.+)$/);
    if (!m) continue;
    const key = m[1].toLowerCase();
    if (seenMove.has(key)) continue;
    seenMove.add(key);
    moves.push({ section: '(paragraph)', name: m[1], phonetic: null, alias: m[2] || null, debut: null, text: p });
  }

  // 4. 战斗表现：★实测 wiki 用 ul>li 列表，格式
  //    "Chapter 114: Jotaro Kujo, Part 1" / "Stone Ocean Chapter 12: ..."
  //    同时保留含引用关键词的段落
  const battles = [];
  const seenBattle = new Set();
  const RE_BATTLE = /^(?:[A-Za-z ]+)?\s*Chapter\s\d+|^Chapter\s\d+|Episode\s\d+/i;
  for (const el of root.querySelectorAll('li')) {
    if (isNoise(el)) continue;
    const t = clean(el.innerText);
    if (t.length < 8 || t.length > 300) continue;
    if (RE_BATTLE.test(t) && !seenBattle.has(t)) {
      seenBattle.add(t);
      battles.push({ kind: 'chapter', text: t });
    }
  }
  for (const p of paragraphs) {
    if (/(Chapter\s\d+|Episode\s\d+)/i.test(p) && !seenBattle.has(p)) {
      seenBattle.add(p);
      battles.push({ kind: 'mention', text: p });
    }
  }

  // 5. 命名出处与设定
  const lore = [];
  for (const p of paragraphs) {
    if (/(tarot|card|deity|god|goddess|named after|reference|originates|based on|mytholog)/i.test(p)) {
      lore.push({ text: p });
    }
  }

  // 6. 小节序列化
  const sectionsOut = [];
  for (const s of sections) {
    sectionsOut.push({
      heading: s.heading, level: s.level,
      n_paras: s.paras.length, n_lists: s.lists.length,
      paras: s.paras, lists: s.lists,
    });
  }

  return {
    total_len: allText.length,
    overview: paragraphs.slice(0, 3),
    overview_all: paragraphs,
    sections: sectionsOut,
    moves,
    battles,
    lore,
  };
};

// ---------- URL 变体（与 Python 侧保持一致） ----------
function urlVariants(nameRaw) {
  const base = nameRaw.replace(/["“”]+$/, '').trim();
  const out = [
    base.replace(/ /g, '_'),
    base.replace(/\s*\((.*?)\)\s*/g, '_$1').replace(/ /g, '_'),
    base.replace(/\s*\(.*?\)\s*/g, '').replace(/ /g, '_'),
  ];
  return [...new Set(out)];
}

// ---------- 主流程 ----------
async function main() {
  const args = process.argv.slice(2);
  const getArg = (k, d) => {
    const i = args.indexOf(k);
    return i >= 0 ? args[i + 1] : d;
  };
  const limit = parseInt(getArg('--limit', '0'), 10);
  const delay = parseInt(getArg('--delay', '900'), 10);
  const force = args.includes('--force');
  const only = getArg('--only', '');       // 逗号分隔的 stand_id

  if (!fs.existsSync(DETAILS)) {
    console.error(`缺少 ${DETAILS}，请先运行 run_pipeline.py`);
    process.exit(1);
  }
  fs.mkdirSync(OUT_DIR, { recursive: true });

  const details = JSON.parse(fs.readFileSync(DETAILS, 'utf8'));
  let todo = details.map(d => ({ stand_id: d.stand_id, name_raw: d.name_raw }));
  if (only) {
    const want = new Set(only.split(',').map(s => s.trim()).filter(Boolean));
    todo = todo.filter(t => want.has(t.stand_id));
    console.log(`--only 过滤后 ${todo.length} 个：${[...want].join(', ')}`);
  }
  if (limit > 0) todo = todo.slice(0, limit);
  console.log(`待渲染 ${todo.length} 个页面（间隔 ${delay}ms，约 ${(todo.length * delay / 1000 / 60).toFixed(1)} 分钟）`);

  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({ userAgent: UA });
  const ok = [];
  let fails = 0;
  let totalChars = 0;

  for (let i = 0; i < todo.length; i++) {
    const { stand_id, name_raw } = todo[i];
    const outFile = path.join(OUT_DIR, `${stand_id}.json`);
    if (!force && fs.existsSync(outFile)) {
      try {
        const prev = JSON.parse(fs.readFileSync(outFile, 'utf8'));
        if (prev && prev.total_len > 500) { ok.push(stand_id); totalChars += prev.total_len; continue; }
      } catch (e) { /* 损坏则重抓 */ }
    }

    let got = null;
    let lastErr = '';
    for (const slug of urlVariants(name_raw)) {
      const page = await ctx.newPage();
      try {
        await page.goto(`${BASE}/${slug}`, { waitUntil: 'domcontentloaded', timeout: 45000 });
        await page.waitForFunction(() => {
          const ps = document.querySelectorAll('#mw-content-text p');
          return Array.from(ps).some(p => (p.innerText || '').trim().length > 120);
        }, { timeout: 15000 }).catch(() => {});
        const data = await page.evaluate(EXTRACT);
        if (data && !data.error) {
          got = { stand_id, name_raw, slug, url: `${BASE}/${slug}`, ...data };
          break;
        }
        lastErr = data && data.error ? `${data.error} (len=${data.total_len || 0})` : 'empty result';
      } catch (e) {
        lastErr = `${e.name}: ${String(e.message).slice(0, 80)}`;
      } finally {
        await page.close();
      }
    }

    if (got) {
      fs.writeFileSync(outFile, JSON.stringify(got, null, 2), 'utf8');
      ok.push(stand_id);
      totalChars += got.total_len;
      console.log(`  [${i + 1}/${todo.length}] ${stand_id}  ${got.total_len}ch  ` +
                  `paras=${got.overview_all.length} moves=${got.moves.length}`);
    } else {
      fails++;
      console.log(`  [${i + 1}/${todo.length}] ${stand_id}  FAIL  ${lastErr}`);
    }
    if (i < todo.length - 1) await new Promise(r => setTimeout(r, delay));
  }

  await browser.close();
  const avg = ok.length ? Math.round(totalChars / ok.length) : 0;
  console.log(`\n完成：成功 ${ok.length} / ${todo.length}，失败 ${fails}`);
  console.log(`总字符 ${totalChars.toLocaleString()}，平均 ${avg} 字符/页`);
  console.log(`输出目录：${OUT_DIR}`);
}

main().catch(e => { console.error(e); process.exit(1); });

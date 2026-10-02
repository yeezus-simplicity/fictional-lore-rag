# tools/ 渲染抓取

用 Playwright 渲染详情页，抽取静态 HTML 里拿不到的非结构化正文。

## 为什么需要渲染

详情页的能力描述、招式列表、战斗记录等长文本由 JavaScript 注入。
静态 HTML 中 37 个 `<p>` 的 `text_content()` 长度全为0；
渲染后可得 **148 万字符**（平均 9617 字符/页）。

## 前置

```bash
# Playwright（已随项目安装到WorkBuddy 的 node workspace）
NODE_PATH=<node-workspace>/node_modules

# 浏览器（本机已有 chromium-1234 / chromium-1243）
# 若缺失：npx playwright install chromium
```

## 用法

```bash
node render_fetch.js                  # 增量（已抓的跳过）
node render_fetch.js --force          # 全量重抓（约 7 分钟）
node render_fetch.js --limit 10       # 只抓前 10 个
node render_fetch.js --only a,b,c     # 指定 stand_id
node render_fetch.js --delay 1200# 放慢限速
```

## 抽取内容

| 字段 | 对应检索单元 |
| --- | --- |
| `overview` / `overview_all` | 能力概述 |
| `sections[]` | 小节（外观/性格/能力/历史/台词…）|
| `moves[]` | 招式（★ 从 `div.techBox` 解析，含读音/别名/首发章节）|
| `battles[]` | 战斗表现（`Chapter N: ...` 列表项）|
| `lore[]` | 命名出处与设定 |

## 关键结构（实测）

```html
<div class="mw-parser-output">              ← 正文真正所在
  <h3>User</h3><div>Jotaro Kujo</div>       ← 标量字段
  <td data-source="destpower">A</td>        ← 形态数值（多次出现=多形态）
  <div class="techBox">Ora Ora(オラオラ) Debut: Chapter 119</div>  ← 招式
  <li>Chapter 114: Jotaro Kujo, Part 1</li>  ← 战斗记录
</div>
```

## 踩坑：page.evaluate 作用域陷阱

```js
// ✗ 错误：箭头函数参数不跨回调共享，且异常被 catch 静默吞掉
Array.from(qs).map(p => f(p)).filter(t => t.length > 50 && !isNoise(p))

// ✓ 正确：显式循环 + 错误必须上报
for (const el of els) { const t = f(el); if (ok(t)) out.push(t); }
```

**必须给 catch 加错误上报**，否则失败原因不可见（踩过：全部 FAIL 但无错误信息）。

---

*详见 `../docs/M1数据层完成报告.md` §4、§8*

# Tusk 射程分歧根因调查报告

> 完成日期：2026-10-03
> 起点：M4 遗留项 —— 「Tusk 4 形态射程主源 E/D/D/C，两镜像一致 D/B/B/A，疑为不同射程标准」
> **核心结论：不是数据错误，是**度量标准差异**；且发现「两个镜像源不独立」，**
> **导致 M4 的 `prefer_consensus` 策略失效**

---

## 1. 调查过程（四步）

### 第一步：先怀疑自己的解析

Tusk 的六维里，`RNG` 恰好等于 `SPD`（E/E/E、D/D/D、D/D/D、C/B/C 中 RNG=SPD），
第一反应是「解析 bug 复制了列」。

**验证**：

```
bogdan CSV：RNG == SPD 的行  42/154 = 27.3%
jojowiki：  RNG == SPD 的形态  51/177 = 28.8%
```

约 27% 与随机水平相当 → **假设否定，Tusk 四形态属巧合**。

> ★ 教训：观察到"看起来像 bug"的现象时，先量化它的**发生率**。
> 如果是复制应该是 100%，27% 说明是巧合。

### 第二步：确认主源没错

直接抓 jojowiki 的 Tusk 页面官方表格：

```
Destructive Power | Speed | Range
E                 | E     | E        ← ACT1
D                 | D     | D        ← ACT2
D                 | D     | D        ← ACT3
A                 | B     | C        ← ACT4
```

**与主源数据完全一致（E/D/D/C）→ 主源没有录入错误。**

### 第三步：查官方对射程的定义

从 jojowiki 的 `Stand` 页面 `RANGE` 小节抓到原文：

> "One of the most important features of a Stand is its **range**.
> A Stand's **range and accuracy are inversely proportional**;
> the further the Stand moves away from its **firing range**,
> the weaker and less subtle it becomes... this can be construed
> more specifically as an **'effective firing range'**."

> "all Stands can be roughly divided into two types: 'short distance',
> and 'long distance'. As a trend, **short-range types display great power**...
> Long-range types typically have a special ability that lets them
> attack indirectly."

并且 Tusk ACT4 的条目里有：

> "Tusk ACT4 thus one of the most powerful Stands in the series...
> **displays the physical abilities of a close-range Stand**"

**主源是唯一给出「有效射程」口径定义的来源。**

### 第四步：全量量化偏向

对 802 个可比格子（主源 × 镜像 bogdan）逐维度统计：

| 维度 | n | 镜像更高 | 镜像更低 | 相同 | 净偏移 | 偏向强度 |
|---|---|---|---|---|---|---|
| PWR | 129 | 0 | 0 | 129 | 0 | — |
| SPD | 138 | 0 | 0 | 138 | 0 | — |
| **RNG** | 130 | **3** | **0** | 127 | **+5** | **+1.67** |
| **STA** | 143 | **2** | **0** | 141 | **+8** | **+4.00** |
| PRC | 135 | 0 | 0 | 135 | 0 | — |
| DEV | 127 | 0 | 0 | 127 | 0 | — |

冲突的差值分布：

```
RNG: {+1: 4条, +2: 6条}    差值 0 或负数：0 条
```

**四个维度 100% 一致，分歧只集中在 RNG/STA，且方向全部单向。**

---

## 2. ★★ 最重要的发现：两个镜像源不独立

调查中发现了一件必须纠正的事 —— M4 的 `prefer_consensus` 策略**建立在一个错误前提上**。

### 检验

```python
# 逐格比对两个 CSV 的 154 个共同条目
same                = 696   # 相同
only_in_bogdan      = 154   # bogdan 有值、topology 空
only_in_topology    =   0   # ← topology 有值、bogdan 空的：0 处
real_value_conflicts=  16   # 归一化后真正的等级分歧
```

**`only_in_topology == 0` 是决定性的**：这说明

> **topology ⊆ bogdan —— topology 是 bogdan 的严格子集（删减版）**

进一步逐条看那 16 处"真分歧"，绝大多数其实是**字面量写法差异**：

```
Bohemian Rhapsody  RNG: bogdan=infinite  topology=Infi        ← 同一个意思
Gold Experience Requiem  PWR: bogdan=infinite  topology=Infi ← 同一个意思
C-Moon            PRC: bogdan=unknown   topology=A
```

**两边都是有效等级字母（A–E）且数值不同的，只有 1 处**：

```
Black Sabbath  PWR:  bogdan=E  topology=A
```

### 结论

> **两个「镜像源」是同源的** —— topology 是 bogdan 的删减版，
> 而非独立采集的第三方数据。
>
> **「两个源一致」在这里是必然结果，不构成交叉验证。**

---

## 3. ★ 对 M4 结论的修正

M4 用 `prefer_consensus` 采信了 CSV 的 D/B/B/A。**这个结论是错的。**

| 替身 | 主源 | 两镜像 | M4 原结论 | **修正后** |
|---|---|---|---|---|
| Tusk ACT1 | E | D | D (2) | **E (1)** |
| Tusk ACT2 | D | B | B (4) | **D (2)** |
| Tusk ACT3 | D | B | B (4) | **D (2)** |
| Tusk ACT4 | C | A | A (5) | **C (3)** |
| Boy II Man | C | B | B (4) | **C (3)** |

**依据**：主源是唯一给出射程口径定义的来源（"effective firing range"），
CSV 是无出处的第三方整理表，且两者不独立。

### 消解器已加入独立性前置检验

```python
consensus_usable = (
    self.use_consensus
    and both_mirrors_agree
    and (self.mirror_independent is not False)   # ★ 新增
)
```

独立性不成立时，共识策略自动失效并降级为 `prefer_primary`，
同时在 `rationale` 里写明原因。

### 注意：置信度反而下降了

| | 修正前 | 修正后 |
|---|---|---|
| 平均置信度 | 0.7364 | **0.7023** |
| `prefer_consensus` | 5 条 | **0 条** |
| `prefer_primary` | 1 条 | 6 条 |

**这是正确的** —— 置信度应反映真实把握。
采信一个不独立源的"伪共识"本就不该给 0.95。

---

## 4. 为什么「系统性偏移」指向标准差异而非错误

三条证据链：

| 判据 | 观察 | 排除的解释 |
|---|---|---|
| **集中在特定维度** | PWR/SPD/PRC/DEV 零分歧，只有 RNG/STA 有 | 排除「整体数据质量差」 |
| **方向单向** | RNG 高 3/低 0，STA 高 2/低 0 | 排除「随机噪声」（应对称分布） |
| **差值规整** | `{+1: 4条, +2: 6条}`，无 0 无负 | 排除「个别录入错误」 |

若是录入错误，不会出现"只错两个维度、且一律偏高、且差值这么规整"的模式。

**最可能的解释**：两张表对 `RNG`（和 `STA`）使用了不同的**分级基准**。
例如 jojowiki 按「有效射程」（射程-精度反比曲线上的拐点）分级，
第三方表可能按「理论最大射程」或某种平均标准。

差值 `{+1, +2}` 混合而不是固定值，说明**两表的分级粒度不同**，
而非简单的整体平移。

---

## 5. 方法论沉淀

### 5.1 ★「多源交叉验证」的前提是源独立

这是本调查最重要的可迁移结论。

```
错误思路：三个源里两个一致 → 置信度 0.95
正确思路：三个源里两个【独立】一致 → 置信度才高
```

**如果两个源同源（一个是另一个的子集、复制、或共享上游），
它们的一致性是数学必然，提供零额外信息。**

判定方法很直接：
- 看「只有 A 有的条目数」与「只有 B 有的条目数」
- 若一方为 0 而另一方 > 0 → **子集关系，不独立**
- 再看归一化后真正的数值分歧数，< 3 则不足以支撑交叉验证

### 5.2 观察到"像 bug"的现象，先量化发生率

Tusk 的 `RNG == SPD` 看起来像复制 bug，但 27% 的发生率说明是巧合。
**如果不去量化，很容易把巧合当成 bug 去"修"，反而引入错误。**

### 5.3 单向偏移是系统性差异的强信号

双向均等的差异 = 噪声；单向偏移 = 口径差异。
这个判据能把"数据质量差"与"标准不同"区分开。

---

## 6. 遗留

| 事项 | 说明 |
|---|---|
| **CSV 究竟按什么标准分级** | 未知。CSV 无出处、无限定文档。**可能永远查不到** |
| STA 也有 +4.0 的强偏向 | 本次聚焦 RNG，STA 待同样分析（推测是「持续力」的定义差异） |
| 是否要给 STA 加同样的处理 | 已加（消解器对所有 VALUE_MISMATCH 统一处理） |
| 13 条 `?` 的真实值 | 仍需查设定集原文 |

---

## 7. 产出

| 文件 | 作用 |
|---|---|
| `services/analyze_conflicts.py` | 根因分析（假说检验） |
| `services/conflict_resolver.py` | 消解器（新增独立性前置检验） |
| `dataset/processed/conflict_root_cause.json` | 原始分析数据 |

**复现**：
```bash
python services/analyze_conflicts.py    # 假说检验 + 偏向统计
python services/conflict_resolver.py    # 消解（会打印独立性证据）
```

---

*根因调查完成 · 分歧是标准差异非数据错误 · 发现镜像源不独立，修正 M4 结论*

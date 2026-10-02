# dataset/ 数据层

M1 数据管道：抓取 → 编码 → 三源合并 → 校验 → 落盘

## 快速开始

```bash
cd pipeline
python run_pipeline.py            # 用缓存跑全流程（秒级）
python run_pipeline.py --refetch  # 重抓详情页（约 10 分钟，154 请求）
python run_pipeline.py --dry-run  # 不落盘
python selftest.py                # 编码层 + 校验层自测
```

## 目录

```
dataset/
├── schema/data_schema.sql    六表 + 枚举 + CHECK 约束 + 视图
├── pipeline/
│   ├── encode.py             判定表实现（数据规范 §1.4）
│   ├── validate.py           V1–V11 校验器
│   ├── scrape.py             抓取层
│   ├── merge.py              合并层
│   ├── run_pipeline.py       主控
│   └── selftest.py           自测
├── sources/                  原始抓取结果（可重跑复用）
│   ├── stand_stats_main.html
│   └── details.json
└── processed/                产出
    ├── stands.json  stand_stats.json  stand_forms.json
    ├── conflicts.json  normalizations.json
    └── merge_report.json  validation_report.json
```

## 关键设计

- **结构化数据不进向量索引**：六维能力值走 SQL，文本才走向量
- **原始字面量全保留**（`*_raw`）：归一化是 lossy 操作，保留原值才能在规范演进时重跑
- **冲突不静默覆盖**：全部落 `conflicts.json`，含消解策略
- **composite 禁止取平均**：任一维缺失即置 NULL

详见 [`../docs/数据规范.md`](../docs/数据规范.md) 与 [`../docs/M1-3数据管道报告.md`](../docs/M1-3数据管道报告.md)。

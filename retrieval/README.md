# retrieval/ 检索层

M3 混合检索：BM25 词法 + bge-m3 稠密 + RRF 融合。

## 快速开始

```bash
# 1. 下载模型（必须，huggingface.co 在本环境被拦）
python fetch_model.py --file-set weights_only

# 2. 构建向量索引（约 35 秒，含模型加载）
python dense.py

# 3. 跑实验
cd ../evaluation
python run_experiments.py --all        # D2 + D4 + BM25PARAM
python run_experiments.py --exp D2     # 只跑核心实验
```

## 文件

| 文件 | 作用 |
| --- | --- |
| `lexical.py` | BM25 倒排索引 + RRF 融合 + 规则路由（零依赖） |
| `dense.py` | 向量索引（bge-m3 / char-ngram 双后端 + 优雅降级） |
| `fetch_model.py` | 模型下载器（绕过 huggingface_hub 的 302 bug） |
| `rerank.py` | 交叉编码器重排（bge-reranker-v2-m3） |
| `chunking.py` | 切块视图与溯源映射（D1 对照用） |

## ★ 核心实验结论（D2）

```
方案                recall@1  recall@3  recall@5     MRR  nDCG@5   延迟
BM25 only            0.1522    0.3261    0.5272  0.3223   0.3371    0.3ms
Vector only          0.1576    0.3641    0.4728  0.3490   0.3270   28.1ms
Hybrid RRF           0.1739    0.3913    0.5435  0.3564   0.3652   28.9ms
```

**反直觉发现：纯向量的recall@5 竟然低于 BM25。**

向量「排得更准但捞得更少」——recall@1/@3 更优（头部精度），
recall@5 更差（召回广度）。原因：块长方差大（p50=229, max=2174），
`max_seq_length=1024` 导致长块被截断；且语料专有名词密集，向量泛化不如精确词形匹配。

**这就是「为什么要混合检索」的实证依据**——不是理论推导，是数据上两条路线各有胜负。

## 索引选型

**内存暴力检索，不上HNSW/IVF**：

```
2407 块 × 1024 维 × 4 byte = 9.4 MB
```

这个规模下暴力精确检索的延迟（<1ms）优于近似索引的额外开销，
且结果精确无召回损失。**按数据规模选型**——与「结构化数据不进向量库」是同源判断。

## 模型配置

| 项 | 值 | 说明 |
| --- | --- | --- |
| 模型 | bge-m3 | 568M，1024 维，MIT 授权，中英混合 |
| 精度 | fp16 (cuda) | 显存 1.2GB；**cpu 上必须 fp16→fp32**（fp16 在 cpu 极慢且数值不稳） |
| max_seq_length | 1024 | 块长 p95=671 |
| 索引内存 | 9.4 MB | fp32，L2 归一化后余弦 = 点积 |

## 踩坑

| 坑 | 现象 | 修法 |
| --- | --- | --- |
| **huggingface.co被拦** | 502 | 用 `hf-mirror.com` |
| **snapshot_download 得到 0 字节** | "下载成功"但文件空 | 库的 302 跟随逻辑失效，自写 urllib 下载器 |
| **权重文件名不通用** | 404 | bge-m3 只有 `pytorch_model.bin`；bge-reranker-v2-m3 有 `model.safetensors`。**下载前先查仓库文件列表** |
| **ST 6.x API 变更** | `dtype` 参数不存在 | 多参数回退链；`get_sentence_embedding_dimension` 改名 |
| **本地模型路径** | 加载时触发网络请求失败 | `resolve_model_path()` 优先 `models/` 下的本地目录 |
| **大文件下载中断** | 2.2GB 传到一半失败 | 大文件（>50MB）改用 `curl -C -` 断点续传 |
| **rerank 延迟统计被污染** | 首次推理 20s+ | `warmup()` 触发 kernel 编译，计时前必做 |

## 重排层说明（D3）

### 双编码器 vs 交叉编码器

| | bge-m3（双编码器） | bge-reranker（交叉编码器） |
|---|---|---|
| 方式 | query/doc **分别**编码成向量再算相似度 | query+doc **拼在一起**过一遍模型 |
| 预计算 | doc 向量可预计算，检索 O(1) | **不可预计算**，必须实时算 |
| 精度 | 有上限（交互信息丢失） | 高得多 |
| 用途 | 全库召回 | **只对Top-N 重排** |

标准范式「召回（快、粗）→ 重排（慢、精）」的理论依据就在这里。
实测：相关块 0.9952 分，无关块 0.66–0.72 分，区分度明显。

**性能**：约 49ms/条（GPU, batch=16），因此只用于 Top-20/50 重排，
不可用于全库（2407 块 × 49ms ≈ 118 秒）。

---

*详见 `../docs/M3实验报告.md`*

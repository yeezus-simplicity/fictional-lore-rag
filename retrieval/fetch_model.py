"""
模型下载器：绕过 huggingface_hub 的 302 跟随问题。

★ 实测踩坑（2026-10-03）：
  - `huggingface.co` 被沙箱代理拦（502）
  - `hf-mirror.com` 根路径 200，但 `huggingface_hub.snapshot_download`
    下载得到 **0 字节文件**——库对 302 重定向的跟随逻辑在此环境失效
  - `curl -L` 手动跟随重定向则完全正常

对策：直接用 urllib 手动跟随 302，逐文件下载到本地目录，
      再让 sentence-transformers 从本地路径加载。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ENDPOINTS = [
    "https://hf-mirror.com",
    "https://huggingface.co",
]

UA = "Mozilla/5.0 (research; educational; rag-kb project)"

# 常见模型文件清单（按需下载，避免拉取不必要的大文件）
# ★ 实测教训：权重文件名**不通用**，必须先查仓库实际文件列表：
#     curl -sSL "https://hf-mirror.com/api/models/<repo>" | python -c "..."
#   - BAAI/bge-m3            →只有 pytorch_model.bin（无 safetensors）
#   - BAAI/bge-reranker-v2-m3 → 有 model.safetensors
#   因此下面同时列出两者，谁成功用谁。
FILE_SETS = {
    # 通用句向量模型（bge-m3）
    "sentence-transformers": [
        "config.json", "tokenizer.json", "tokenizer_config.json",
        "special_tokens_map.json", "modules.json",
        "sentence_bert_config.json", "1_Pooling/config.json",
        "model.safetensors", "pytorch_model.bin",
    ],
    # CrossEncoder 重排模型（bge-reranker-v2-m3）：XLM-R 架构，无需 ST 包装
    "crossencoder": [
        "config.json", "tokenizer.json", "tokenizer_config.json",
        "special_tokens_map.json", "model.safetensors",
        "sentencepiece.bpe.model",
    ],
    "weights_only": [
        "config.json", "tokenizer.json", "tokenizer_config.json",
        "special_tokens_map.json", "pytorch_model.bin",
    ],
}


def pick_endpoint() -> str:
    for ep in ENDPOINTS:
        try:
            req = urllib.request.Request(ep, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=15) as r:
                if r.status == 200:
                    return ep
        except Exception:
            continue
    raise RuntimeError("无可用端点")


def download_file(repo: str, filename: str, dest: Path,
                   endpoint: str, retries: int = 3,
                   timeout: int = 180) -> bool:
    """下载单文件。

    ★ 大文件（>50MB）优先走 curl：
      - 支持断点续传（-C -），慢速网络下失败可续
      - 实测 urllib 在 2.27GB 上可能 20 分钟无进展，curl 更稳
    """
    if dest.exists() and dest.stat().st_size > 0:
        # 断点续传：curl -C - 会接着已有的部分下
        if dest.stat().st_size > 50 * 1024 * 1024:
            return _download_curl(repo, filename, dest, endpoint)
        return True

    url = f"{endpoint}/{repo}/resolve/main/{filename}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
            if not data:
                raise ValueError("空响应（302 未正确跟随）")
            dest.write_bytes(data)
            return True
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False        # 该文件不存在（如可选的 Dense 层）
            if attempt == retries:
                print(f"    [FAIL] {filename}: HTTP {e.code}")
                return False
        except Exception as e:
            if attempt == retries:
                print(f"    [FAIL] {filename}: {type(e).__name__}")
                return False
        time.sleep(1.5 * attempt)
    return False


def _download_curl(repo: str, filename: str, dest: Path,
                   endpoint: str, max_seconds: int = 3000) -> bool:
    """用 curl 下载大文件（支持断点续传）。"""
    import subprocess
    url = f"{endpoint}/{repo}/resolve/main/{filename}"
    try:
        r = subprocess.run(
            ["curl", "-sSL", "-C", "-", "-o", str(dest),
             "--max-time", str(max_seconds), url],
            capture_output=True, text=True, timeout=max_seconds + 60,
        )
        if dest.exists() and dest.stat().st_size > 0:
            return True
        print(f"    [curl FAIL] {filename}: rc={r.returncode} {r.stderr[:100]}")
        return False
    except subprocess.TimeoutExpired:
        # 超时但已下部分 → 保留，下次会续传
        if dest.exists() and dest.stat().st_size > 0:
            print(f"    [curl PARTIAL] {filename} "
                  f"{dest.stat().st_size/1024/1024:.0f}MB（可续传）")
        return False
    except FileNotFoundError:
        return False               # 没有 curl，退回 urllib


def fetch_model(repo: str, outdir: Path, file_set: str = "sentence-transformers",
                verbose: bool = True) -> Path:
    """下载模型到本地目录。返回本地路径。"""
    outdir.mkdir(parents=True, exist_ok=True)
    endpoint = pick_endpoint()
    if verbose:
        print(f"  端点: {endpoint}")
        print(f"  仓库: {repo}")
        print(f"  目标: {outdir}")

    files = FILE_SETS.get(file_set, FILE_SETS["sentence-transformers"])
    ok_files: list[str] = []
    missing: list[str] = []
    # 权重文件只需成功一个（safetensors 优先，回退 pytorch_model.bin）
    weight_ok = False

    for fn in files:
        is_weight = fn in ("model.safetensors", "pytorch_model.bin",
                           "model.onnx")
        if is_weight and weight_ok:
            missing.append(fn)
            if verbose:
                print(f"    SKIP {fn:40s} 已有可用权重")
            continue
        ok = download_file(repo, fn, outdir / fn, endpoint, timeout=1800)
        (ok_files if ok else missing).append(fn)
        if ok and is_weight:
            weight_ok = True
        if verbose:
            size = (outdir / fn).stat().st_size / 1024 / 1024 if ok else 0
            print(f"    {'OK  ' if ok else 'MISS'} {fn:40s} {size:8.2f} MB")

    # 必需文件校验：权重二选一即可
    if not weight_ok:
        raise RuntimeError("权重文件缺失（model.safetensors / pytorch_model.bin 均失败）")
    essential = ["config.json", "tokenizer.json"]
    absent = [e for e in essential if e in missing]
    if absent:
        raise RuntimeError(f"必需文件缺失：{absent}")

    (outdir / "_download_info.json").write_text(
        json.dumps({"repo": repo, "endpoint": endpoint,
                    "ok": ok_files, "missing": missing},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    if verbose:
        total = sum(f.stat().st_size for f in outdir.rglob("*") if f.is_file())
        print(f"  完成：{len(ok_files)} 个文件，合计 {total/1024/1024:.1f} MB")
    return outdir


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="BAAI/bge-m3")
    ap.add_argument("--out", default=None)
    ap.add_argument("--file-set", default="sentence-transformers")
    args = ap.parse_args()

    root = Path(__file__).resolve().parents[1]
    out = Path(args.out) if args.out else root / "models" / args.repo.replace("/", "_")
    print("=" * 68)
    print("模型下载")
    print("=" * 68)
    try:
        p = fetch_model(args.repo, out, args.file_set)
        print(f"\n模型已就绪：{p}")
    except RuntimeError as e:
        print(f"\n下载失败：{e}")
        sys.exit(1)

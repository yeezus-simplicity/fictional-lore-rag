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
# ★ 实测：BAAI/bge-m3 仓库**没有** model.safetensors，只有 pytorch_model.bin。
#   不要假设 safetensors 是通用格式——必须先查仓库实际文件列表。
FILE_SETS = {
    "sentence-transformers": [
        "config.json", "tokenizer.json", "tokenizer_config.json",
        "special_tokens_map.json", "modules.json",
        "sentence_bert_config.json", "1_Pooling/config.json",
        #权重二选一：safetensors 优先，否则回退 pytorch_model.bin
        "model.safetensors", "pytorch_model.bin",
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
    """下载单文件，手动跟随 302。

    ★ 大文件（权重）需要长 timeout：2.3GB 在慢速网络下可能十几分钟。
    """
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

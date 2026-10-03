"""
生成层（M6）。

★★ 与 M5 的关系：M5 是**抽取式**（直接返回原文），
   本模块是**生成式**。两者共存，由参数 `mode` 切换。

设计立场（M5 定下，M6 继承）：
  生成层只做「**基于证据的组织与表述**」，不做「补充模型自己的知识」。
  证据不足时应明确说「证据中未提及」，而不是编造。
  —— 这正是本模块要评测的「忠实度」。

★ 为什么不做成「RAG 硬约束」（如 constrained decoding）？
  1. 1.5B 模型的硬约束解码实现成本高，且会显著拖慢
  2. 忠实度本身就是**要测的对象** —— 如果用硬约束保证，
     评测就失去意义了
  → 忠实度靠**评测**保证，不靠机制保证。这是本项目的立场。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
MODEL_DIR = ROOT / "models" / "Qwen_Qwen2.5-1.5B-Instruct"

GEN_MODEL_NAME = "Qwen/Qwen2.5-1.5B-Instruct"

# 系统提示词：★★ 忠实度约束的核心
SYSTEM_PROMPT = """你是一个严谨的数据问答助手。请严格遵守以下规则：

1. **只使用提供的证据**回答问题。证据是你唯一的信息来源。
2. 如果证据不足以回答，明确说「提供的证据中未提及」，**不要推测或补充**。
3. 不要引入证据中没有的名称、数值或描述。
4. 如果证据中标注了「未知」或数据缺失，照实说明，不要替换成猜测值。
5. 回答要简洁，直接给出答案，不需要复述问题。

证据：
{evidence}"""

USER_TEMPLATE = """问题：{question}

请基于上述证据回答。"""


@dataclass
class GenAnswer:
    """生成结果（含评测所需的全链路信息）。"""

    text: str
    n_prompt_tokens: int
    n_gen_tokens: int
    elapsed_ms: float
    evidence_used: list[str] = field(default_factory=list)
    mode: str = "generate"

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "n_prompt_tokens": self.n_prompt_tokens,
            "n_gen_tokens": self.n_gen_tokens,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "evidence_used": self.evidence_used,
            "mode": self.mode,
        }


class Generator:
    """Qwen2.5-1.5B 生成器。懒加载，未加载时优雅降级。"""

    def __init__(self, model_path: Optional[str] = None,
                 device: Optional[str] = None,
                 max_new_tokens: int = 256,
                 temperature: float = 0.1,
                 load_in_4bit: bool = False):
        """
        Args:
            temperature: ★ 固定用低温（默认 0.1）
              理由：忠实度评测需要**确定性**输出。
              高温度会增加随机性，使评测结果不可复现。
              这不是「追求更好答案」，而是「保证评测可重复」。
        """
        self.model_path = model_path or (
            str(MODEL_DIR) if (MODEL_DIR / "config.json").exists()
            else GEN_MODEL_NAME)
        self.device = device
        self.max_new_tokens = max_new_tokens
        self.temperature = temperature
        self.load_in_4bit = load_in_4bit
        self.model = None
        self.tokenizer = None
        self._error = None
        self._device = device
        self._load()

    # ---------------------------------------------------------
    def _load(self) -> None:
        if not (MODEL_DIR / "config.json").exists():
            self._error = f"模型不存在：{MODEL_DIR}（先跑 fetch_model.py）"
            return
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer

            if self._device is None:
                self._device = "cuda" if torch.cuda.is_available() else "cpu"

            self.tokenizer = AutoTokenizer.from_pretrained(
                self.model_path, trust_remote_code=True)

            kwargs: dict = {"trust_remote_code": True}
            if self._device == "cuda":
                kwargs["torch_dtype"] = torch.float16
                if self.load_in_4bit:
                    try:
                        from transformers import BitsAndBytesConfig
                        kwargs["quantization_config"] = BitsAndBytesConfig(
                            load_in_4bit=True,
                            bnb_4bit_compute_dtype=torch.float16,
                        )
                        kwargs["device_map"] = "auto"
                    except ImportError:
                        print("[gen] bitsandbytes 未安装，退回 fp16")
            else:
                kwargs["torch_dtype"] = torch.float32

            self.model = AutoModelForCausalLM.from_pretrained(
                self.model_path, **kwargs)
            if not self.load_in_4bit:
                self.model.to(self._device)
            self.model.eval()
        except Exception as e:
            self._error = f"{type(e).__name__}: {e}"
            self.model = None

    # ---------------------------------------------------------
    def warmup(self) -> None:
        """预热：触发 CUDA 上下文与 kernel 编译。"""
        if self.model is None:
            return
        t0 = time.time()
        self.generate("预热问题", ["Star Platinum 的破坏力是 A 级。"], max_new_tokens=16)
        if time.time() - t0 > 3:
            print(f"[gen] 预热 {time.time() - t0:.1f}s")

    # ---------------------------------------------------------
    def generate(self, question: str, evidence_texts: list[str],
                 max_new_tokens: Optional[int] = None) -> GenAnswer:
        """基于证据生成回答。"""
        if self.model is None:
            raise RuntimeError(f"生成模型不可用：{self._error}")

        import torch

        ev = "\n\n".join(
            f"[证据 {i + 1}]（来源：{t.get('stand_name', '?')}"
            f"{'/' + t['chunk_type'] if t.get('chunk_type') else ''}）\n"
            f"{(t.get('content') if isinstance(t, dict) else t) or ''}"
            for i, t in enumerate(evidence_texts)
        )
        if not ev.strip():
            ev = "（无可用证据）"

        messages = [
            {"role": "system",
             "content": SYSTEM_PROMPT.format(evidence=ev)},
            {"role": "user", "content": USER_TEMPLATE.format(question=question)},
        ]
        prompt = self.tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True)

        inputs = self.tokenizer(prompt, return_tensors="pt",
                                truncation=True, max_length=3072)
        inputs = {k: v.to(self.model.device) for k, v in inputs.items()}

        t0 = time.time()
        with torch.no_grad():
            out = self.model.generate(
                **inputs,
                max_new_tokens=max_new_tokens or self.max_new_tokens,
                do_sample=self.temperature > 0,
                temperature=max(self.temperature, 1e-5),
                top_p=0.9,
                repetition_penalty=1.05,
                pad_token_id=self.tokenizer.pad_token_id
                or self.tokenizer.eos_token_id,
            )
        dt = time.time() - t0

        gen_ids = out[0][inputs["input_ids"].shape[1]:]
        text = self.tokenizer.decode(gen_ids, skip_special_tokens=True).strip()

        return GenAnswer(
            text=text,
            n_prompt_tokens=int(inputs["input_ids"].shape[1]),
            n_gen_tokens=int(len(gen_ids)),
            elapsed_ms=dt * 1000,
            evidence_used=[str(t.get("chunk_id")) for t in evidence_texts
                           if isinstance(t, dict)],
            mode="generate",
        )

    # ---------------------------------------------------------
    def extractive(self, question: str, evidence_texts: list[str],
                   max_chars: int = 400) -> GenAnswer:
        """抽取式（M5 模式）—— 直接返回最相关的原文。

        作为忠实度上界对照（B 组）：照抄不可能有幻觉。
        """
        t0 = time.time()
        best, best_hit = "", -1
        words = [w.lower() for w in re.findall(r"[A-Za-z]{3,}", question)]
        words = [w for w in words if w not in ("the", "and", "for")][:8]

        for t in evidence_texts:
            text = t.get("content", "") if isinstance(t, dict) else str(t)
            sents = re.split(r"(?<=[.!?])\s+", text)
            for s in sents:
                h = sum(s.lower().count(w) for w in words)
                if h > best_hit:
                    best, best_hit = s, h
        if not best and evidence_texts:
            t = evidence_texts[0]
            best = (t.get("content", "") if isinstance(t, dict) else str(t))
        return GenAnswer(
            text=best[:max_chars],
            n_prompt_tokens=0, n_gen_tokens=0,
            elapsed_ms=(time.time() - t0) * 1000,
            evidence_used=[str(t.get("chunk_id")) for t in evidence_texts
                           if isinstance(t, dict)],
            mode="extractive",
        )

    # ---------------------------------------------------------
    def info(self) -> dict:
        return {
            "model": self.model_path,
            "device": self._device,
            "loaded": self.model is not None,
            "temperature": self.temperature,
            "max_new_tokens": self.max_new_tokens,
            "load_in_4bit": self.load_in_4bit,
            "error": self._error,
        }


# ==================================================================
if __name__ == "__main__":
    print("=" * 62)
    print("生成模型自测")
    print("=" * 62)
    g = Generator()
    print(f"  info: {g.info()}")
    if g.model is None:
        print("  模型不可用，退出")
        raise SystemExit(1)
    g.warmup()

    ev = [
        {"chunk_id": 1, "stand_name": "Star Platinum",
         "chunk_type": "ability_overview",
         "content": "Star Platinum is a close-range Stand with exceptional "
                    "physical strength and speed. Its rating breakdown: "
                    "Destructive Power A, Speed A, Range C, Stamina B."},
        {"chunk_id": 2, "stand_name": "The World",
         "chunk_type": "ability_overview",
         "content": "The World possesses the ability Time Stop, allowing it "
                    "to stop time for a brief period."},
    ]
    tests = [
        ("Star Platinum 的破坏力是几级？", "有证据的问题"),
        ("The World 的时间停止能力是什么？", "有证据的问题"),
        ("Star Platinum 的射程是多少？", "证据里有 Range C"),
        ("DIO 的能力值是多少？", "★ 证据里没有 DIO（测是否会编造）"),
    ]
    for q, note in tests:
        r = g.generate(q, ev)
        print(f"\n  Q: {q}\n     ({note})")
        print(f"     A: {r.text[:180]}")
        print(f"     {r.n_gen_tokens} tokens / {r.elapsed_ms:.0f}ms")

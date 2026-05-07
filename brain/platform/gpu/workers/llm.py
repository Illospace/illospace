"""LLM worker — local text generation via transformers."""

import logging
import os
import re
import time

try:
    import torch
except ImportError:
    torch = None  # type: ignore[assignment]

from brain.platform.gpu.config import WorkerManifest
from brain.platform.gpu.device import default_dtype, empty_device_cache, select_device
from brain.platform.gpu.worker_protocol import BaseWorker

logger = logging.getLogger("brain.platform.gpu.worker.llm")
DEFAULT_NON_CUDA_FALLBACK_MODEL = "Qwen/Qwen3-0.6B"


class LLMWorker(BaseWorker):
    """Loads a causal LM on GPU, serves /infer for generation requests."""

    def __init__(self, manifest: WorkerManifest):
        super().__init__(manifest)
        self.model = None
        self.tokenizer = None
        self.device = "cpu"
        self.loaded_model_path = manifest.model_path

    def _model_path_for_device(self, device: str) -> str:
        if device == "cuda":
            return self.manifest.model_path
        if os.environ.get("GPU_ALLOW_LARGE_LLM_ON_NON_CUDA", "").lower() in {"1", "true", "yes", "on"}:
            return self.manifest.model_path
        fallback_model = (
            os.environ.get("GPU_LLM_FALLBACK_MODEL")
            or os.environ.get("LLM_FALLBACK_MODEL")
            or DEFAULT_NON_CUDA_FALLBACK_MODEL
        )
        logger.warning(
            "CUDA is unavailable; loading lightweight LLM fallback %s instead of %s",
            fallback_model,
            self.manifest.model_path,
        )
        return fallback_model

    def load_model(self):
        from transformers import AutoModelForCausalLM, AutoTokenizer

        if torch is None:
            raise RuntimeError("PyTorch is required for the LLM worker")

        t0 = time.time()
        self.device = select_device(torch, "GPU_LLM_DEVICE", "LLM_DEVICE")
        self.loaded_model_path = self._model_path_for_device(self.device)
        dtype = default_dtype(torch, self.device)
        logger.info(f"Using device: {self.device}, dtype: {dtype}")
        self.tokenizer = AutoTokenizer.from_pretrained(self.loaded_model_path)
        try:
            if self.device == "cuda":
                self.model = AutoModelForCausalLM.from_pretrained(
                    self.loaded_model_path,
                    torch_dtype=dtype,
                    device_map="cuda",
                )
            else:
                self.model = AutoModelForCausalLM.from_pretrained(
                    self.loaded_model_path,
                    torch_dtype=dtype,
                )
                self.model.to(self.device)
        except Exception:
            if self.device == "cpu":
                raise
            logger.exception("Failed to load on %s — falling back to CPU", self.device)
            empty_device_cache(torch, self.device)
            self.device = "cpu"
            self.loaded_model_path = (
                os.environ.get("GPU_LLM_FALLBACK_MODEL")
                or os.environ.get("LLM_FALLBACK_MODEL")
                or DEFAULT_NON_CUDA_FALLBACK_MODEL
            )
            self.model = AutoModelForCausalLM.from_pretrained(
                self.loaded_model_path,
                torch_dtype=torch.float32,
            )
        self.model.eval()
        elapsed = time.time() - t0
        device_mb = torch.cuda.memory_allocated() / 1024**2 if self.device == "cuda" else 0
        logger.info(f"Model loaded in {elapsed:.1f}s — device={self.device} allocated={device_mb:.0f}MB")

    def unload_model(self):
        if self.model is not None:
            del self.model
            del self.tokenizer
            self.model = None
            self.tokenizer = None
            try:
                empty_device_cache(torch, self.device)
            except Exception:
                pass
            logger.info("Model unloaded, GPU memory freed")

    async def handle_request(self, data: dict) -> dict:
        prompt = data.get("prompt", "")
        max_tokens = min(data.get("max_tokens", 500), 2048)
        temperature = data.get("temperature", 0.7)
        stop_sequences = data.get("stop", [])
        think = data.get("think", False)  # qwen3.5 thinking mode is expensive; opt in per request

        if not prompt:
            return {"error": "prompt required"}

        t0 = time.time()

        inputs = self.tokenizer(
            prompt, return_tensors="pt", truncation=True, max_length=4096
        )
        input_ids = inputs["input_ids"].to(self.device)
        input_len = input_ids.shape[1]

        generate_kwargs = {
            "input_ids": input_ids,
            "max_new_tokens": max_tokens,
            "do_sample": temperature > 0,
            "eos_token_id": self.tokenizer.eos_token_id,
        }
        if temperature > 0:
            generate_kwargs["temperature"] = temperature

        try:
            with torch.no_grad():
                output_ids = self.model.generate(**generate_kwargs)

            new_ids = output_ids[0][input_len:]
            text = self.tokenizer.decode(new_ids, skip_special_tokens=True).strip()
        finally:
            # Explicitly free intermediate tensors to prevent VRAM leak
            del input_ids, inputs
            try:
                del output_ids, new_ids
            except NameError:
                pass
            empty_device_cache(torch, self.device)

        # Strip thinking tags when think=False (qwen3.5 thinking mode)
        if not think and "<think>" in text:
            text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

        for stop in stop_sequences:
            if stop in text:
                text = text[:text.index(stop)]

        elapsed = time.time() - t0
        return {
            "text": text,
            "model": self.loaded_model_path,
            "tokens_generated": len(text.split()),  # approximate after cleanup
            "elapsed_ms": round(elapsed * 1000, 1),
        }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--socket", required=True)
    args = parser.parse_args()

    manifest = WorkerManifest(name=args.name, model_path=args.model_path, vram_mb=3000,
                              worker_module="brain.platform.gpu.workers.llm")
    worker = LLMWorker(manifest)
    worker.run(socket_path=args.socket)

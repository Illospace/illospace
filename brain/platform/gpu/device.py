"""Torch device helpers for GPU workers."""

from __future__ import annotations

import os


def select_device(torch_module, *env_keys: str) -> str:
    """Pick the best available torch device, with an env override."""
    requested = "auto"
    for key in (*env_keys, "GPU_DEVICE"):
        value = os.environ.get(key)
        if value:
            requested = value.strip().lower()
            break

    has_cuda = torch_module.cuda.is_available()
    has_mps = (
        hasattr(torch_module, "backends")
        and hasattr(torch_module.backends, "mps")
        and torch_module.backends.mps.is_available()
    )

    if requested == "cuda" and has_cuda:
        return "cuda"
    if requested == "mps" and has_mps:
        return "mps"
    if requested == "cpu":
        return "cpu"
    if has_cuda:
        return "cuda"
    if has_mps:
        return "mps"
    return "cpu"


def default_dtype(torch_module, device: str):
    if device == "cuda":
        return torch_module.bfloat16 if torch_module.cuda.is_bf16_supported() else torch_module.float16
    if device == "mps":
        return torch_module.float16
    return torch_module.float32


def empty_device_cache(torch_module, device: str) -> None:
    try:
        if device == "cuda":
            torch_module.cuda.empty_cache()
        elif device == "mps" and hasattr(torch_module, "mps"):
            torch_module.mps.empty_cache()
    except Exception:
        pass

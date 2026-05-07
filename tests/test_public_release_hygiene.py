"""Lightweight checks for public release setup files."""

from pathlib import Path

from packaging.requirements import Requirement


ROOT = Path(__file__).resolve().parents[1]


def test_requirements_gpu_is_parseable():
    for line in (ROOT / "requirements-gpu.txt").read_text(encoding="utf-8").splitlines():
        candidate = line.strip()
        if not candidate or candidate.startswith("#") or candidate.startswith("--"):
            continue
        Requirement(candidate)


def test_env_example_matches_runtime_embedding_default():
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "EMBEDDING_API_MODEL=gemini-embedding-2" in env_example
    assert "EMBEDDING_API_MODEL=gemini-embedding-001" not in env_example


def test_contributing_keeps_env_file_optional():
    contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")

    assert "cp .env.example .env" not in contributing
    assert ".env` is optional" in contributing

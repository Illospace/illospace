"""Base recipe contracts."""

from __future__ import annotations

from brain.systems.runs.engine import RunRecipeResult, RunRuntime


class BaseRunRecipe:
    name = "base"

    def execute(self, runtime: RunRuntime) -> RunRecipeResult:
        raise NotImplementedError


__all__ = ["BaseRunRecipe"]

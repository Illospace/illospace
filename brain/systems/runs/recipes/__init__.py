"""Run recipes."""

from brain.systems.runs.recipes.base import BaseRunRecipe
from brain.systems.runs.recipes.fast import FastRecipe
from brain.systems.runs.recipes.workers import WorkerRecipe


def default_recipes() -> dict[str, BaseRunRecipe]:
    return {
        FastRecipe.name: FastRecipe(),
        WorkerRecipe.name: WorkerRecipe(),
    }


__all__ = [
    "BaseRunRecipe",
    "FastRecipe",
    "WorkerRecipe",
    "default_recipes",
]

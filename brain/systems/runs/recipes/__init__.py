"""Run recipes."""

from brain.systems.runs.recipes.base import BaseRunRecipe
from brain.systems.runs.recipes.fast import FastRecipe
from brain.systems.runs.recipes.deep import DeepRecipe
from brain.systems.runs.recipes.scout import ScoutRecipe
from brain.systems.runs.recipes.workers import WorkerRecipe


def default_recipes() -> dict[str, BaseRunRecipe]:
    return {
        FastRecipe.name: FastRecipe(),
        DeepRecipe.name: DeepRecipe(),
        ScoutRecipe.name: ScoutRecipe(),
        WorkerRecipe.name: WorkerRecipe(),
    }


__all__ = [
    "BaseRunRecipe",
    "DeepRecipe",
    "FastRecipe",
    "ScoutRecipe",
    "WorkerRecipe",
    "default_recipes",
]

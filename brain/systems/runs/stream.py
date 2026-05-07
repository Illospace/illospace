"""Run event streaming primitives."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


RunStreamSink = Callable[[str, dict[str, Any]], None]


class RunStream:
    def __init__(self, sink: RunStreamSink | None = None):
        self._sink = sink

    def publish(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._sink:
            self._sink(event_type, payload)


__all__ = ["RunStream", "RunStreamSink"]

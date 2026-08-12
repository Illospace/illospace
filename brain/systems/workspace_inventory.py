"""Build a symlink-safe inventory of a workspace tree."""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict


class WorkspaceConsumer(TypedDict):
    path: str
    bytes_used: int


class WorkspaceScanError(TypedDict):
    path: str
    operation: Literal["scan", "stat"]
    message: str


@dataclass(frozen=True, slots=True)
class WorkspaceInventory:
    """Regular-file bytes and any errors observed while walking a workspace."""

    root: str
    bytes_used: int
    top_consumers: tuple[WorkspaceConsumer, ...]
    scan_errors: tuple[WorkspaceScanError, ...]

    @property
    def complete(self) -> bool:
        return not self.scan_errors


def inventory_workspace(
    workspace_root: str | Path,
    *,
    top_consumer_limit: int = 10,
) -> WorkspaceInventory:
    """Inventory regular files without following symlinks.

    Consumers are direct children of ``workspace_root``. Errors are part of the
    result so callers can reject decisions based on an incomplete scan.
    """

    root = Path(workspace_root)
    sizes: dict[str, int] = {}
    errors: list[WorkspaceScanError] = []
    pending: list[tuple[Path, str | None]] = [(root, None)]

    while pending:
        current, consumer = pending.pop()
        try:
            entries = os.scandir(current)
        except OSError as exc:
            errors.append(
                {
                    "path": str(current),
                    "operation": "scan",
                    "message": str(exc),
                }
            )
            continue

        with entries:
            for entry in entries:
                entry_path = Path(entry.path)
                entry_consumer = consumer or entry.name
                try:
                    entry_stat = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    errors.append(
                        {
                            "path": str(entry_path),
                            "operation": "stat",
                            "message": str(exc),
                        }
                    )
                    continue

                if stat.S_ISREG(entry_stat.st_mode):
                    sizes[entry_consumer] = (
                        sizes.get(entry_consumer, 0) + entry_stat.st_size
                    )
                elif stat.S_ISDIR(entry_stat.st_mode):
                    sizes.setdefault(entry_consumer, 0)
                    pending.append((entry_path, entry_consumer))

    consumers = [
        WorkspaceConsumer(path=path, bytes_used=bytes_used)
        for path, bytes_used in sizes.items()
    ]
    consumers.sort(key=lambda item: (-item["bytes_used"], item["path"]))
    limit = max(1, int(top_consumer_limit))
    return WorkspaceInventory(
        root=str(root),
        bytes_used=sum(sizes.values()),
        top_consumers=tuple(consumers[:limit]),
        scan_errors=tuple(errors),
    )


__all__ = [
    "WorkspaceConsumer",
    "WorkspaceInventory",
    "WorkspaceScanError",
    "inventory_workspace",
]

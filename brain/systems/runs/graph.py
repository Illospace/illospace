"""Pure AgentRun graph primitives for Deep planning."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
import json
from typing import Any


PENDING_NODE_STATUSES = frozenset({"", "pending", "queued", "ready"})
ACTIVE_NODE_STATUSES = frozenset({"starting", "running"})
SUCCESS_NODE_STATUSES = frozenset({"completed", "succeeded", "success", "passed"})
FAILED_NODE_STATUSES = frozenset({"failed", "canceled", "cancelled", "error"})
TERMINAL_NODE_STATUSES = SUCCESS_NODE_STATUSES | FAILED_NODE_STATUSES | frozenset({"skipped"})


class RunGraphError(ValueError):
    """Base error for invalid run graphs."""


class RunGraphMissingDependencyError(RunGraphError):
    """Raised when a graph references a node that does not exist."""


class RunGraphCycleError(RunGraphError):
    """Raised when a graph contains a dependency cycle."""


@dataclass(frozen=True)
class RunNode:
    """A planned AgentRun node in a Deep execution graph."""

    id: str
    role: str = "worker"
    objective: str = ""
    recipe: str = "worker"
    kind: str = "worker"
    title: str = ""
    status: str = "pending"
    run_id: int | None = None
    wave: int | None = None
    assignment: Any | None = None
    depends_on: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        node_id = str(self.id or "").strip()
        if not node_id:
            raise ValueError("RunNode id is required")
        object.__setattr__(self, "id", node_id)
        object.__setattr__(self, "role", str(self.role or "worker").strip() or "worker")
        object.__setattr__(self, "objective", str(self.objective or "").strip())
        object.__setattr__(self, "recipe", str(self.recipe or "worker").strip().lower() or "worker")
        object.__setattr__(self, "kind", str(self.kind or self.role or "worker").strip().lower() or "worker")
        object.__setattr__(self, "title", str(self.title or "").strip())
        object.__setattr__(self, "status", _status_value(self.status))
        object.__setattr__(self, "depends_on", _string_tuple(self.depends_on))
        object.__setattr__(self, "metadata", dict(self.metadata or {}))

    @property
    def node_id(self) -> str:
        return self.id

    @property
    def is_pending(self) -> bool:
        return self.status in PENDING_NODE_STATUSES

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_NODE_STATUSES

    @property
    def is_completed(self) -> bool:
        return self.status in SUCCESS_NODE_STATUSES

    @property
    def is_failed(self) -> bool:
        return self.status in FAILED_NODE_STATUSES

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_NODE_STATUSES

    def with_status(
        self,
        status: str,
        *,
        run_id: int | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> "RunNode":
        next_metadata = dict(self.metadata)
        if metadata:
            next_metadata.update(dict(metadata))
        return replace(
            self,
            status=_status_value(status),
            run_id=self.run_id if run_id is None else int(run_id),
            metadata=next_metadata,
        )

    def with_wave(self, wave: int) -> "RunNode":
        return replace(self, wave=int(wave))

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "role": self.role,
            "objective": self.objective,
            "recipe": self.recipe,
            "kind": self.kind,
            "title": self.title,
            "status": self.status,
            "depends_on": list(self.depends_on),
            "metadata": dict(self.metadata),
        }
        if self.assignment is not None:
            payload["assignment"] = (
                self.assignment.to_payload() if hasattr(self.assignment, "to_payload") else self.assignment
            )
        if self.run_id is not None:
            payload["run_id"] = self.run_id
        if self.wave is not None:
            payload["wave"] = self.wave
        return payload

    def to_dict(self) -> dict[str, Any]:
        return self.to_payload()

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any] | "RunNode") -> "RunNode":
        if isinstance(payload, RunNode):
            return payload
        if not isinstance(payload, Mapping):
            raise TypeError("RunNode payload must be a mapping")
        node_id = payload.get("id") or payload.get("node_id")
        depends_on = (
            payload.get("depends_on")
            or payload.get("dependencies")
            or payload.get("dependency_ids")
            or ()
        )
        assignment = payload.get("assignment")
        if isinstance(assignment, Mapping):
            from brain.systems.runs.assignments import WorkerAssignment

            assignment = WorkerAssignment.from_payload(assignment, default_id=str(node_id or "worker"))
        return cls(
            id=str(node_id or "").strip(),
            role=str(payload.get("role") or payload.get("kind") or "worker"),
            objective=str(payload.get("objective") or payload.get("prompt") or ""),
            recipe=str(payload.get("recipe") or "worker"),
            kind=str(payload.get("kind") or payload.get("node_kind") or payload.get("role") or "worker"),
            title=str(payload.get("title") or ""),
            status=str(payload.get("status") or "pending"),
            run_id=_optional_int(payload.get("run_id")),
            wave=_optional_int(payload.get("wave")),
            assignment=assignment,
            depends_on=_string_tuple(depends_on),
            metadata=dict(payload.get("metadata") or {}),
        )


@dataclass(frozen=True)
class RunEdge:
    """A dependency edge from one run node to another."""

    source: str
    target: str
    edge_type: str = "depends_on"
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        source = str(self.source or "").strip()
        target = str(self.target or "").strip()
        if not source or not target:
            raise ValueError("RunEdge source and target are required")
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "edge_type", str(self.edge_type or "depends_on").strip() or "depends_on")
        object.__setattr__(self, "metadata", dict(self.metadata or {}))

    @property
    def from_node(self) -> str:
        return self.source

    @property
    def to_node(self) -> str:
        return self.target

    def to_payload(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "edge_type": self.edge_type,
            "metadata": dict(self.metadata),
        }

    def to_dict(self) -> dict[str, Any]:
        return self.to_payload()

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any] | "RunEdge") -> "RunEdge":
        if isinstance(payload, RunEdge):
            return payload
        if not isinstance(payload, Mapping):
            raise TypeError("RunEdge payload must be a mapping")
        source = (
            payload.get("source")
            or payload.get("from")
            or payload.get("from_node")
            or payload.get("from_node_id")
        )
        target = (
            payload.get("target")
            or payload.get("to")
            or payload.get("to_node")
            or payload.get("to_node_id")
        )
        return cls(
            source=str(source or "").strip(),
            target=str(target or "").strip(),
            edge_type=str(payload.get("edge_type") or payload.get("type") or "depends_on"),
            metadata=dict(payload.get("metadata") or {}),
        )


@dataclass(frozen=True)
class DeepPlan:
    """A deterministic, validated graph of native AgentRun nodes."""

    nodes: tuple[RunNode, ...] = ()
    edges: tuple[RunEdge, ...] = ()
    id: str = "deep-plan"
    objective: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        nodes = tuple(RunNode.from_payload(node) for node in self.nodes)
        edges = _canonical_edges(nodes, tuple(RunEdge.from_payload(edge) for edge in self.edges))
        _assert_unique_node_ids(nodes)
        waves = compute_waves(nodes, edges)
        nodes_with_waves = tuple(
            replace(node, wave=waves[node.id], depends_on=_dependency_ids_for(edges, node.id))
            for node in nodes
        )
        object.__setattr__(self, "nodes", _sort_nodes(nodes_with_waves))
        object.__setattr__(self, "edges", edges)
        object.__setattr__(self, "id", str(self.id or "deep-plan").strip() or "deep-plan")
        object.__setattr__(self, "objective", str(self.objective or "").strip())
        object.__setattr__(self, "metadata", dict(self.metadata or {}))

    @property
    def plan_id(self) -> str:
        return self.id

    @property
    def node_ids(self) -> tuple[str, ...]:
        return tuple(node.id for node in self.nodes)

    @property
    def wave_index(self) -> dict[str, int]:
        return {node.id: int(node.wave or 0) for node in self.nodes}

    @property
    def waves(self) -> "_WaveGroups":
        return _WaveGroups(self.nodes_by_wave())

    def nodes_by_wave(self) -> tuple[tuple[RunNode, ...], ...]:
        by_wave: dict[int, list[RunNode]] = {}
        for node in self.nodes:
            by_wave.setdefault(int(node.wave or 0), []).append(node)
        return tuple(
            tuple(nodes)
            for _, nodes in sorted(by_wave.items(), key=lambda item: item[0])
        )

    def ordered_nodes(self) -> tuple[RunNode, ...]:
        return self.nodes

    def get_node(self, node_id: str) -> RunNode | None:
        target = str(node_id or "").strip()
        return next((node for node in self.nodes if node.id == target), None)

    def require_node(self, node_id: str) -> RunNode:
        node = self.get_node(node_id)
        if node is None:
            raise KeyError(f"Run node not found: {node_id}")
        return node

    def status_for(self, node_id: str) -> str:
        return self.require_node(node_id).status

    def nodes_with_status(self, status: str) -> tuple[RunNode, ...]:
        target = _status_value(status)
        return tuple(node for node in self.nodes if node.status == target)

    def completed_nodes(self) -> tuple[RunNode, ...]:
        return tuple(node for node in self.nodes if node.is_completed)

    def failed_nodes(self) -> tuple[RunNode, ...]:
        return tuple(node for node in self.nodes if node.is_failed)

    def pending_nodes(self) -> tuple[RunNode, ...]:
        return tuple(node for node in self.nodes if node.is_pending)

    def active_nodes(self) -> tuple[RunNode, ...]:
        return tuple(node for node in self.nodes if node.is_active)

    def is_node_completed(self, node_id: str) -> bool:
        return self.require_node(node_id).is_completed

    def is_node_terminal(self, node_id: str) -> bool:
        return self.require_node(node_id).is_terminal

    def dependency_ids(self, node_id: str) -> tuple[str, ...]:
        self.require_node(node_id)
        return tuple(sorted(edge.source for edge in self.edges if edge.target == str(node_id)))

    def dependencies_for(self, node_id: str) -> tuple[str, ...]:
        return self.dependency_ids(node_id)

    def dependent_ids(self, node_id: str) -> tuple[str, ...]:
        self.require_node(node_id)
        return tuple(sorted(edge.target for edge in self.edges if edge.source == str(node_id)))

    def ready_nodes(self) -> tuple[RunNode, ...]:
        blocked = set(self.blocked_node_ids())
        return tuple(
            node
            for node in self.nodes
            if node.id not in blocked
            and node.is_pending
            and all(self.require_node(dependency).is_completed for dependency in self.dependency_ids(node.id))
        )

    def ready_node_ids(self) -> tuple[str, ...]:
        return tuple(node.id for node in self.ready_nodes())

    def blocked_node_ids(self) -> tuple[str, ...]:
        blocked: set[str] = set()
        changed = True
        while changed:
            changed = False
            for node in self.nodes:
                if node.id in blocked or node.is_terminal:
                    continue
                dependencies = tuple(self.require_node(dependency) for dependency in self.dependency_ids(node.id))
                if any(dependency.is_failed or dependency.id in blocked for dependency in dependencies):
                    blocked.add(node.id)
                    changed = True
        return tuple(sorted(blocked, key=lambda node_id: (self.wave_index[node_id], node_id)))

    def with_node_status(
        self,
        node_id: str,
        status: str,
        *,
        run_id: int | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> "DeepPlan":
        target = str(node_id or "").strip()
        nodes = tuple(
            node.with_status(status, run_id=run_id, metadata=metadata) if node.id == target else node
            for node in self.nodes
        )
        if all(node.id != target for node in self.nodes):
            raise KeyError(f"Run node not found: {node_id}")
        return replace(self, nodes=nodes)

    def compute_waves(self) -> dict[str, int]:
        return compute_waves(self.nodes, self.edges)

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "plan_id": self.id,
            "objective": self.objective,
            "nodes": [node.to_payload() for node in self.nodes],
            "edges": [edge.to_payload() for edge in self.edges],
            "waves": [
                {"wave": index, "nodes": [node.id for node in wave]}
                for index, wave in enumerate(self.nodes_by_wave())
            ],
            "metadata": dict(self.metadata),
        }

    def to_dict(self) -> dict[str, Any]:
        return self.to_payload()

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any] | "DeepPlan" | None) -> "DeepPlan":
        if isinstance(payload, DeepPlan):
            return payload
        payload = dict(payload or {})
        raw_nodes = payload.get("nodes")
        if raw_nodes is None and isinstance(payload.get("workers"), Sequence):
            raw_nodes = _nodes_from_worker_payloads(payload.get("workers") or ())
        raw_edges = payload.get("edges") or payload.get("dependencies") or ()
        return cls(
            id=str(payload.get("id") or payload.get("plan_id") or "deep-plan"),
            objective=str(payload.get("objective") or payload.get("summary") or ""),
            nodes=tuple(raw_nodes or ()),
            edges=tuple(raw_edges or ()),
            metadata=dict(payload.get("metadata") or {}),
        )


RunGraph = DeepPlan


class _WaveGroups(tuple):
    def __new__(cls, waves: tuple[tuple[RunNode, ...], ...]):
        return super().__new__(cls, waves)

    def __call__(self) -> tuple[tuple[RunNode, ...], ...]:
        return tuple(self)


def compute_waves(nodes: Sequence[RunNode], edges: Sequence[RunEdge]) -> dict[str, int]:
    """Compute deterministic dependency waves for a node graph."""

    node_ids = tuple(node.id for node in nodes)
    node_id_set = set(node_ids)
    dependencies: dict[str, set[str]] = {node_id: set() for node_id in node_ids}
    for edge in edges:
        if edge.source not in node_id_set or edge.target not in node_id_set:
            missing = tuple(sorted({edge.source, edge.target} - node_id_set))
            raise RunGraphMissingDependencyError(
                f"Run graph references missing node(s): {', '.join(missing)}"
            )
        dependencies[edge.target].add(edge.source)

    waves: dict[str, int] = {}
    completed: set[str] = set()
    remaining = set(node_ids)
    wave = 0
    while remaining:
        ready = sorted(node_id for node_id in remaining if dependencies[node_id].issubset(completed))
        if not ready:
            cycle_nodes = ", ".join(sorted(remaining))
            raise RunGraphCycleError(f"Run graph contains a dependency cycle: {cycle_nodes}")
        for node_id in ready:
            waves[node_id] = wave
        completed.update(ready)
        remaining.difference_update(ready)
        wave += 1
    return waves


def _nodes_from_worker_payloads(workers: Sequence[Any]) -> tuple[RunNode, ...]:
    nodes: list[RunNode] = []
    for index, worker in enumerate(workers, start=1):
        if not isinstance(worker, Mapping):
            continue
        role = str(worker.get("role") or f"worker-{index}").strip() or f"worker-{index}"
        node_id = str(worker.get("id") or worker.get("node_id") or role).strip()
        nodes.append(
            RunNode(
                id=node_id,
                role=role,
                objective=str(worker.get("objective") or ""),
                recipe=str(worker.get("recipe") or "worker"),
                status=str(worker.get("status") or "pending"),
                run_id=_optional_int(worker.get("run_id")),
                depends_on=_string_tuple(worker.get("depends_on") or worker.get("dependencies") or ()),
                metadata={
                    key: value
                    for key, value in worker.items()
                    if key
                    not in {
                        "id",
                        "node_id",
                        "role",
                        "objective",
                        "recipe",
                        "status",
                        "run_id",
                        "depends_on",
                        "dependencies",
                    }
                },
            )
        )
    return tuple(nodes)


def _canonical_edges(nodes: Sequence[RunNode], edges: Sequence[RunEdge]) -> tuple[RunEdge, ...]:
    merged = [*edges]
    for node in nodes:
        merged.extend(RunEdge(source=dependency, target=node.id) for dependency in node.depends_on)
    seen: set[tuple[str, str, str]] = set()
    unique: list[RunEdge] = []
    for edge in sorted(merged, key=_edge_sort_key):
        key = (edge.source, edge.target, edge.edge_type)
        if key in seen:
            continue
        seen.add(key)
        unique.append(edge)
    return tuple(unique)


def _edge_sort_key(edge: RunEdge) -> tuple[str, str, str, str]:
    return (
        edge.source,
        edge.target,
        edge.edge_type,
        json.dumps(edge.metadata, default=str, sort_keys=True),
    )


def _sort_nodes(nodes: Sequence[RunNode]) -> tuple[RunNode, ...]:
    return tuple(sorted(nodes, key=lambda node: int(node.wave or 0)))


def _dependency_ids_for(edges: Sequence[RunEdge], node_id: str) -> tuple[str, ...]:
    return tuple(edge.source for edge in edges if edge.target == node_id)


def _assert_unique_node_ids(nodes: Sequence[RunNode]) -> None:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for node in nodes:
        if node.id in seen:
            duplicates.add(node.id)
        seen.add(node.id)
    if duplicates:
        raise RunGraphError(f"Run graph contains duplicate node id(s): {', '.join(sorted(duplicates))}")


def _status_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip().lower()


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, Mapping):
        value = value.values()
    try:
        return tuple(str(item).strip() for item in value if str(item).strip())
    except TypeError:
        text = str(value).strip()
        return (text,) if text else ()


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


__all__ = [
    "ACTIVE_NODE_STATUSES",
    "DeepPlan",
    "FAILED_NODE_STATUSES",
    "PENDING_NODE_STATUSES",
    "RunEdge",
    "RunGraph",
    "RunGraphCycleError",
    "RunGraphError",
    "RunGraphMissingDependencyError",
    "RunNode",
    "SUCCESS_NODE_STATUSES",
    "TERMINAL_NODE_STATUSES",
    "compute_waves",
]

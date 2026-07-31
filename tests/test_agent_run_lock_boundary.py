from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOCK_OWNER = Path("brain/systems/runs/store.py")


def _call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _mentions_agent_run_row(node: ast.AST) -> bool:
    return any(
        (isinstance(child, ast.Name) and child.id == "AgentRunRow")
        or (isinstance(child, ast.Attribute) and child.attr == "AgentRunRow")
        for child in ast.walk(node)
    )


def _is_agent_run_select(expression: ast.AST, known_names: set[str]) -> bool:
    current = expression
    while True:
        if isinstance(current, ast.Name):
            return current.id in known_names
        if not isinstance(current, ast.Call):
            return False
        if _call_name(current) == "select":
            return any(_mentions_agent_run_row(argument) for argument in current.args)
        if not isinstance(current.func, ast.Attribute):
            return False
        current = current.func.value


def _is_agent_run_get(call: ast.Call) -> bool:
    return (
        _call_name(call) == "get"
        and bool(call.args)
        and _mentions_agent_run_row(call.args[0])
        and any(
            keyword.arg == "with_for_update"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for keyword in call.keywords
        )
    )


def _explicit_lock_target_excludes_agent_run(call: ast.Call) -> bool:
    for keyword in call.keywords:
        if keyword.arg == "of":
            return not _mentions_agent_run_row(keyword.value)
    return False


class _AgentRunLockVisitor(ast.NodeVisitor):
    """Find ordinary AgentRunRow lock forms without guessing SQLAlchemy types.

    This deliberately conservative matcher catches direct fluent
    ``select(AgentRunRow...).with_for_update()`` chains, simple local statement
    variables derived from those selects, and literal
    ``session.get(AgentRunRow, ..., with_for_update=True)`` calls. It does not
    resolve renamed model imports, helper-returned statements, or statements
    stored in attributes or containers. Restricting the select check to its
    projection avoids flagging locks on other tables that merely reference an
    AgentRunRow in a predicate or projection when ``of=`` explicitly limits
    the lock to other tables. It also does not inspect raw SQL strings.
    """

    def __init__(self) -> None:
        self.known_names: set[str] = set()
        self.offenders: list[tuple[int, str]] = []

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        outer_names = self.known_names
        self.known_names = set()
        for statement in node.body:
            self.visit(statement)
        self.known_names = outer_names

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        is_run_select = _is_agent_run_select(node.value, self.known_names)
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            if is_run_select:
                self.known_names.add(target.id)
            else:
                self.known_names.discard(target.id)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is None:
            return
        self.visit(node.value)
        if isinstance(node.target, ast.Name):
            if _is_agent_run_select(node.value, self.known_names):
                self.known_names.add(node.target.id)
            else:
                self.known_names.discard(node.target.id)

    def visit_Call(self, node: ast.Call) -> None:
        if (
            _call_name(node) == "with_for_update"
            and isinstance(node.func, ast.Attribute)
            and _is_agent_run_select(node.func.value, self.known_names)
            and not _explicit_lock_target_excludes_agent_run(node)
        ):
            self.offenders.append((node.lineno, "AgentRunRow select calls with_for_update"))
        elif _is_agent_run_get(node):
            self.offenders.append(
                (node.lineno, "session.get(AgentRunRow) passes with_for_update=True")
            )
        self.generic_visit(node)


def test_agent_run_row_locks_are_owned_by_run_store() -> None:
    offenders: list[str] = []
    for path in sorted((ROOT / "brain").rglob("*.py")):
        relative_path = path.relative_to(ROOT)
        if relative_path == LOCK_OWNER:
            continue
        source = path.read_text(encoding="utf-8")
        if "with_for_update" not in source:
            continue
        tree = ast.parse(source, filename=str(relative_path))
        visitor = _AgentRunLockVisitor()
        visitor.visit(tree)
        offenders.extend(
            f"{relative_path}:{line}: {description}"
            for line, description in visitor.offenders
        )

    assert offenders == [], (
        "AgentRunRow locks must stay inside brain/systems/runs/store.py. "
        "Use RunStore.lock_run: append_event depends on its root-before-child "
        "ordering, which a caller-local row lock can violate.\n"
        + "\n".join(offenders)
    )

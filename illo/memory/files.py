"""Validate a full workspace copy before a memory file changes."""

import shutil
import tempfile
from pathlib import Path

import yaml

from illo.notes.files import file_path, write
from illo.tasks.checks import known
from illo.workspace import WorkspaceError, load_workspace

PATTERNS = (
    "people.yaml", "agents.yaml", "illo.yaml", "lists/*.md", "notes/*.md",
    "rules/*.md", "skills/*/SKILL.md", "routines/*.md", "connectors/*.yaml",
)


def render(front, body):
    return "---\n" + yaml.safe_dump(front, sort_keys=False) + "---\n\n" + body.strip() + "\n"


def split(text):
    lines = text.splitlines()
    end = lines.index("---", 1)
    return yaml.safe_load("\n".join(lines[1:end])), "\n".join(lines[end + 1:]).strip()


def validate(root, folder, name, text):
    """Use the real loader, including references and the complete prompt budget."""
    file_path(root, folder, name, create=False)
    with tempfile.TemporaryDirectory(prefix="illo-memory-") as temp:
        candidate = Path(temp)
        if (root / "lists").exists():
            (candidate / "lists").mkdir()
        for pattern in PATTERNS:
            for source in root.glob(pattern):
                relative = source.relative_to(root)
                if source.is_symlink() or any(p.is_symlink() for p in source.parents if p != root):
                    raise WorkspaceError("path: Use regular workspace files.")
                target = candidate / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
        target = candidate / folder / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if text is None:
            target.unlink(missing_ok=True)
        else:
            target.write_text(text)
        try:
            workspace = load_workspace(candidate)
        except WorkspaceError as exc:
            raise WorkspaceError(str(exc).replace(str(candidate), "workspace")) from None
        if folder == "lists" and text is not None:
            entry = next(e for e in workspace.lists if e.key == Path(name).stem)
            refs = {"person:" + p.key for p in workspace.people if p.active}
            refs |= {"agent:" + a.key for a in workspace.agents}
            for field in ("holders", "default_holder", "reviewer"):
                value = getattr(entry, field)
                if any(ref not in refs for ref in (value if isinstance(value, list) else [value]) if ref):
                    raise WorkspaceError(f"{field}: Use a known active person or agent.")
            if entry.holders and entry.default_holder and entry.default_holder not in entry.holders:
                raise WorkspaceError("default_holder: Choose a holder from holders.")
            if isinstance(entry.done, list) and any(not known(check) for check in entry.done):
                raise WorkspaceError("done: Use a known Done check.")


def replace(root, folder, name, text):
    validate(root, folder, name, text)
    if text is None:
        path = file_path(root, folder, name)
        path.unlink()
        return path
    return write(root, folder, name, text)

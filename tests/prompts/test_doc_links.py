"""Each relative link and each named repository path in the docs exists."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCS = [
    ROOT / "README.md",
    ROOT / "CONTRIBUTING.md",
    *sorted((ROOT / "docs").glob("*.md")),
    *sorted((ROOT / "illo").glob("**/README.md")),
    ROOT / "brain" / "README.md",
]
LINK = re.compile(r"\]\(([^)\s]+)\)")
# A path that starts at the repository root, such as `tests/safety/` or `illo/tools/web.py`.
# The look-behind skips `~/.config/illo/token` and other paths that only contain `illo/`.
PATH = re.compile(r"(?<![\w./~-])((?:illo|tests|brain|collectors|deploy)/[\w./*-]*)")


def prose(path: Path) -> str:
    """Code blocks hold commands and folder sketches, not path claims."""
    return re.sub(r"```.*?```", "", path.read_text(), flags=re.S)


def relative_links(path: Path) -> list[str]:
    return [
        link.split("#")[0]
        for link in LINK.findall(prose(path))
        if not re.match(r"^[a-z][a-z0-9+.-]*:", link) and not link.startswith("#")
    ]


def named_paths(path: Path) -> list[str]:
    """Paths in the text. A trailing period or `:<line>` is not part of the path."""
    names = []
    for match in PATH.finditer(prose(path)):
        name = match.group(1).rstrip(".")
        after = prose(path)[match.end() : match.end() + 1]
        if after in {"<", "{"}:
            # A pattern such as `tests/<area>/` or `illo/{x}`, not a real path.
            continue
        names.append(name)
    return names


def exists(name: str) -> bool:
    return any(ROOT.glob(name)) if "*" in name else (ROOT / name).exists()


@pytest.mark.parametrize("path", DOCS, ids=lambda p: str(p.relative_to(ROOT)))
def test_relative_links_resolve(path):
    for link in relative_links(path):
        target = (path.parent / link).resolve()
        assert target.exists(), f"{path.relative_to(ROOT)}: broken link {link}"


@pytest.mark.parametrize("path", DOCS, ids=lambda p: str(p.relative_to(ROOT)))
def test_named_paths_exist(path):
    missing = sorted({name for name in named_paths(path) if not exists(name)})
    assert not missing, f"{path.relative_to(ROOT)}: missing paths {missing}"


def test_the_patterns_find_links_and_paths(tmp_path):
    assert relative_links(ROOT / "README.md")
    doc = tmp_path / "doc.md"
    doc.write_text(
        "See `tests/safety/test_review_s*.py`, `illo/tools/web.py:12`, and `tests/<area>/`.\n"
        "Keep `~/.config/illo/token` out.\n```sh\nillo/none.py\n```\n"
    )
    assert named_paths(doc) == ["tests/safety/test_review_s*.py", "illo/tools/web.py"]
    assert all(exists(name) for name in named_paths(doc))
    doc.write_text("Read `illo/tools/missing.py`.")
    assert not exists(named_paths(doc)[0])

"""Each relative link in the docs points to a file that exists."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCS = [ROOT / "README.md", ROOT / "CONTRIBUTING.md", *sorted((ROOT / "docs").glob("*.md"))]
LINK = re.compile(r"\]\(([^)\s]+)\)")

# Other areas write these files. A link to one passes until that area merges.
# Remove a name here when its file is on the branch.
PENDING = {"docs/install.md", "docs/agents.md", "docs/collectors.md", "docs/connectors.md"}


def relative_links(path: Path) -> list[str]:
    text = re.sub(r"```.*?```", "", path.read_text(), flags=re.S)
    return [
        link.split("#")[0]
        for link in LINK.findall(text)
        if not re.match(r"^[a-z][a-z0-9+.-]*:", link) and not link.startswith("#")
    ]


@pytest.mark.parametrize("path", DOCS, ids=lambda p: str(p.relative_to(ROOT)))
def test_relative_links_resolve(path):
    for link in relative_links(path):
        target = (path.parent / link).resolve()
        name = target.relative_to(ROOT).as_posix() if target.is_relative_to(ROOT) else link
        assert target.exists() or name in PENDING, f"{path.name}: broken link {link}"


def test_the_link_pattern_finds_links():
    assert relative_links(ROOT / "README.md")

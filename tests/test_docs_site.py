"""The guide's site stages only the pages zensical.toml's nav lists, so a page under guide/ left off
it is missing from the site, and every link to it quietly goes to GitHub instead."""

import re
import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITE = runpy.run_path(str(ROOT / "docs" / "site" / "stage.py"))


def test_every_guide_page_is_on_the_site():
    guide = {page.relative_to(ROOT).as_posix() for page in ROOT.glob("guide/*.md")}
    missing = sorted(guide.difference(SITE["pages"]()))
    assert not missing, f"zensical.toml's nav does not list {missing}"


def test_the_guide_names_nothing_private():
    """The guide is for people using the tools, who never meet `_quiet`, `view._serve_requests` or
    `editor::Form`: how the code does it belongs to the internals under docs/."""
    for page in ROOT.glob("guide/*.md"):
        prose = re.sub(r"^ *```.*?^ *```", "", page.read_text(encoding="utf-8"), flags=re.DOTALL | re.MULTILINE)
        private = [code for code in re.findall(r"`([^`\n]+)`", prose) if re.search(r"(^|\W)_\w|::", code)]
        assert not private, f"{page.relative_to(ROOT)}: {private}"


def test_the_guide_lists_the_mcp_tools(tmp_path):
    """guide/agents.md's table of tools is drawn from their docstrings as the site is staged."""
    SITE["stage"](tmp_path)
    page = (tmp_path / "guide" / "agents.md").read_text(encoding="utf-8")
    assert "mcp-tools" not in page, "guide/agents.md's mcp-tools markers were not replaced"
    assert re.search(r"^\| `check_spec` \| \w", page, re.MULTILINE), "the staged table has no check_spec row"


def test_every_diagram_is_described():
    """Mermaid makes a diagram's accTitle and accDescr its SVG's title and description: what a
    screen reader says for it, on GitHub and on the site alike."""
    for page in SITE["pages"]():
        text = (ROOT / page).read_text(encoding="utf-8")
        for diagram in re.findall(r"^```mermaid\n(.*?)^```", text, re.DOTALL | re.MULTILINE):
            assert "accTitle:" in diagram and "accDescr" in diagram, f"{page}: a diagram with no accTitle or accDescr"


def test_every_nested_list_nests_on_the_site():
    """GitHub nests a list item indented two spaces, and the site, Python-Markdown, only one indented
    four: at two, the site draws it as an item of the list above."""
    for page in SITE["pages"]():
        text = (ROOT / page).read_text(encoding="utf-8")
        prose = re.sub(r"^ *```.*?^ *```", "", text, flags=re.DOTALL | re.MULTILINE)
        for item in re.findall(r"^( +)(?:[-*+]|\d+[.)]) ", prose, re.MULTILINE):
            assert len(item) % 4 == 0, f"{page}: a list item indented {len(item)} spaces, not a multiple of four"

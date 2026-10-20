"""The docs site stages only the pages zensical.toml's nav lists, so a page under docs/ left off it
is missing from the site, and every link to it quietly goes to GitHub instead."""

import re
import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITE = runpy.run_path(str(ROOT / "docs" / "site" / "stage.py"))


def test_every_doc_is_on_the_site():
    docs = {page.relative_to(ROOT).as_posix() for page in ROOT.glob("docs/*.md")}
    missing = sorted(docs.difference(SITE["pages"]()))
    assert not missing, f"zensical.toml's nav does not list {missing}"


def test_every_diagram_is_described():
    """Mermaid makes a diagram's accTitle and accDescr its SVG's title and description: what a
    screen reader says for it, on GitHub and on the site alike."""
    for page in SITE["pages"]():
        text = (ROOT / page).read_text(encoding="utf-8")
        for diagram in re.findall(r"^```mermaid\n(.*?)^```", text, re.DOTALL | re.MULTILINE):
            assert "accTitle:" in diagram and "accDescr" in diagram, f"{page}: a diagram with no accTitle or accDescr"

"""Stages the docs site: copies every page in zensical.toml's nav, and its `extra_css`, to its
`docs_dir`, each under its path in the repository.

Keeping the paths keeps every link between two pages working as written. A link to anything
else, source, a directory or a page left off the site, is rewritten to point at it on GitHub, at
the commit being built. A line that is only a GitHub attachment URL, which GitHub plays as a
video, becomes a `<video>` tag.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tomllib
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG = tomllib.loads((ROOT / "zensical.toml").read_text(encoding="utf-8"))["project"]
OUT = ROOT / CONFIG["docs_dir"]
if not OUT.is_relative_to(ROOT / "target"):
    raise SystemExit(f"docs_dir is emptied on every run, so it has to be under target/: {OUT}")

TOKEN = re.compile(
    r"(?P<code>^ *```.*?^ *```|`[^`\n]*`)|\]\((?P<target>[^)\s#]*)(?P<anchor>#[^)\s]*)?\)"
    r"|^(?P<video>https://github\.com/user-attachments/assets/[0-9a-f-]+)$",
    re.DOTALL | re.MULTILINE,
)
"""Code, which is copied as it is, an inline link, or a bare attachment URL on a line of its own:
docs/publish.sh writes the README's video that way, and GitHub plays it."""
URL = re.compile(r"[a-z][a-z0-9+.-]*:")


def pages(nav: object = CONFIG["nav"]) -> Iterator[str]:
    """Every page in the nav, by its path in the repository."""
    if isinstance(nav, str):
        yield nav
    elif isinstance(nav, list):
        for entry in nav:
            yield from pages(entry)
    elif isinstance(nav, dict):
        for entry in nav.values():
            yield from pages(entry)


def stage(out: Path = OUT) -> list[Path]:
    """Writes the site's pages under `out`, emptied first, and returns them."""
    site = set(pages())
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()

    def link(match: re.Match[str], page: Path) -> str:
        if match["video"]:
            return f'<video src="{match["video"]}" controls muted playsinline></video>'
        target = match["target"]
        if match["code"] or not target or URL.match(target):
            return match[0]
        path = (page.parent / target).resolve()
        name = path.relative_to(ROOT).as_posix()
        if name in site:
            return match[0]
        kind = "tree" if path.is_dir() else "blob"
        return f"]({CONFIG['repo_url']}/{kind}/{commit}/{name}{match['anchor'] or ''})"

    shutil.rmtree(out, ignore_errors=True)
    for name in CONFIG.get("extra_css", []):
        (out / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / name, out / name)
    staged = []
    for name in site:
        page = ROOT / name
        text = TOKEN.sub(lambda match: link(match, page), page.read_text(encoding="utf-8"))
        dest = out / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        staged.append(dest)
    return staged


if __name__ == "__main__":
    stage()

"""Stages the guide's site: copies every page in zensical.toml's nav, and its `extra_css`, to its
`docs_dir`, each under its path in the repository.

Keeping the paths keeps every link between two pages working as written. A link to anything
else, source, a directory or a page left off the site, is rewritten to point at it on GitHub, at
the commit being built. A line that is only a GitHub attachment URL, which GitHub plays as a
video, becomes a `<video>` tag, and the lines between a page's `mcp-tools` markers become a table
of the MCP tools, drawn from their docstrings.

With `--readme`, it rewrites README.md in place instead, as PyPI shows it from the wheel: every
relative link pointed at GitHub. CI runs that before building the wheel.
"""

from __future__ import annotations

import ast
import re
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Collection, Iterator
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
TOOLS = re.compile(r"^<!-- mcp-tools -->\n.*?^<!-- /mcp-tools -->$", re.DOTALL | re.MULTILINE)
"""Where the guide lists the MCP tools. What stands between the markers is for GitHub, which
draws the page as written."""
SERVER = ROOT / "python" / "arena_composer" / "mcp_server.py"


def tools() -> str:
    """The MCP tools as a table, each by its docstring's first paragraph: what the client shows the
    model, and so the only description of a tool there is. Read with `ast`, since importing the
    server imports Arena."""
    rows = ["| Tool | What it is for | Read-only |", "| --- | --- | --- |"]
    for node in ast.parse(SERVER.read_text(encoding="utf-8")).body:
        decorators = [ast.unparse(decorator) for decorator in getattr(node, "decorator_list", [])]
        if not any(decorator.startswith("mcp.tool") for decorator in decorators):
            continue
        what = ast.get_docstring(node).split("\n\n")[0].replace("\n", " ").replace("|", r"\|")
        read_only = "yes" if any("_READ_ONLY" in decorator for decorator in decorators) else "no"
        rows.append(f"| `{node.name}` | {what} | {read_only} |")
    return "\n".join(rows)


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


def head() -> str:
    """The commit being built, where a link pointed at GitHub points."""
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()


def relink(match: re.Match[str], page: Path, commit: str, site: Collection[str] = ()) -> str:
    """A `TOKEN` match in `page` with a relative link pointed at GitHub, unless it goes to a page in
    `site`. Code, an anchor, a URL and a video line stay as they are."""
    target = match["target"]
    if match["code"] or not target or URL.match(target):
        return match[0]
    path = (page.parent / target).resolve()
    name = path.relative_to(ROOT).as_posix()
    if name in site:
        return match[0]
    kind = "tree" if path.is_dir() else "blob"
    return f"]({CONFIG['repo_url']}/{kind}/{commit}/{name}{match['anchor'] or ''})"


def readme() -> str:
    """README.md as PyPI shows it, with no repository to resolve a relative link against. The video
    line stays a URL: PyPI draws no `<video>`."""
    page = ROOT / "README.md"
    commit = head()
    return TOKEN.sub(lambda match: relink(match, page, commit), page.read_text(encoding="utf-8"))


def stage(out: Path = OUT) -> list[Path]:
    """Writes the site's pages under `out`, emptied first, and returns them."""
    site = set(pages())
    commit = head()

    def link(match: re.Match[str], page: Path) -> str:
        if match["video"]:
            return f'<video src="{match["video"]}" controls muted playsinline></video>'
        return relink(match, page, commit, site)

    # Emptied rather than removed: `zensical serve` stops watching a docs_dir that is deleted.
    out.mkdir(parents=True, exist_ok=True)
    for entry in out.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    for name in CONFIG.get("extra_css", []):
        (out / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / name, out / name)
    staged = []
    for name in site:
        page = ROOT / name
        text = TOKEN.sub(lambda match: link(match, page), page.read_text(encoding="utf-8"))
        text = TOOLS.sub(lambda _: tools(), text)
        dest = out / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        staged.append(dest)
    return staged


if __name__ == "__main__":
    if sys.argv[1:] == ["--readme"]:
        (ROOT / "README.md").write_text(readme(), encoding="utf-8")
    else:
        stage()

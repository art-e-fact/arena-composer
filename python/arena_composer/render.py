"""One-shot offscreen renders of a packed scene.

The other half of :mod:`arena_composer.view`: same payload, same Bevy code, but
no window, no watcher and no display server — a few PNGs and a process that is
gone a second later. That is what lets a long-lived caller (the MCP server) show
a layout without holding a GPU open between calls.

Drawing happens in a child process (:mod:`arena_composer.render_process`) for
the reasons given there.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from arena_composer import render_process

#: View names the renderer understands, matching `arena_composer::layout::View`.
#:
#: The three axis views are orthographic. What they are asked is "does the
#: banana rest on the table or float above it" and "do these two overlap", and
#: under perspective both answers depend on where in the frame the object sits.
VIEWS = ("default", "top", "front", "side")

#: Square, and modest: an image costs the model that reads it roughly a
#: thousand tokens, and more pixels do not make a gap easier to see.
DEFAULT_SIZE = 768

#: Bevy logs its adapter, its diagnostics and every screenshot it writes. Only
#: the tail is worth quoting back when a render fails, and only the first few
#: errors when it draws the wrong thing.
_ERROR_LINES = 20


def render(
    payload: bytes,
    out_dir: Path,
    views: Sequence[str] = ("default",),
    size: int = DEFAULT_SIZE,
) -> list[Path]:
    """Draw `payload` from each of `views`, returning one PNG path per view.

    Blocks for as long as the render takes — a second or so, nearly all of it
    Bevy starting up, which is why the views are drawn together rather than one
    call each.
    """
    # Same view twice would name one file twice; keep the first of each.
    views = list(dict.fromkeys(views))
    if not views:
        raise ValueError("no views to render")
    if unknown := [view for view in views if view not in VIEWS]:
        raise ValueError(f"unknown view(s) {unknown}, expected any of {list(VIEWS)}")

    out_dir.mkdir(parents=True, exist_ok=True)
    paths = [out_dir / f"{view}.png" for view in views]
    # A render that fails after a previous one wrote here would otherwise pass
    # its stale image off as the current layout.
    for path in paths:
        path.unlink(missing_ok=True)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            render_process.__name__,
            "--size",
            str(size),
            *(f"{view}={path}" for view, path in zip(views, paths)),
        ],
        input=payload,
        capture_output=True,
        check=False,
        # Bevy colours its log unless this is set, and the reader here is a
        # pipe, not a terminal.
        env={**os.environ, "NO_COLOR": "1"},
    )
    stderr = result.stderr.decode(errors="replace")

    if missing := [path.name for path in paths if not path.exists()]:
        tail = "\n".join(stderr.splitlines()[-_ERROR_LINES:])
        raise RuntimeError(f"render wrote no {', '.join(missing)} (exit {result.returncode})\n{tail}")
    # Bevy draws the rest of the scene around what it could not load and writes
    # the picture anyway, so a failure at this point looks like a good render of
    # a scene missing an object. Returning that image would answer "did my edit
    # land" with a no that isn't true.
    if errors := logged_errors(stderr):
        raise RuntimeError("render logged {} error(s):\n{}".format(len(errors), "\n".join(errors[:_ERROR_LINES])))
    return paths


def logged_errors(stderr: str) -> list[str]:
    """The ERROR lines out of a render's log.

    Bevy's format puts the level second, after the timestamp; matching the field
    rather than the text keeps a message that merely mentions an error out.
    """
    return [line for line in stderr.splitlines() if line.split(maxsplit=2)[1:2] == ["ERROR"]]

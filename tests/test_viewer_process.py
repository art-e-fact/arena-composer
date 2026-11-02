"""The pipe `arena-view` sends scenes down.

Only the child's end of it, and only what fails before a window is asked for —
drawing needs a display. The reader is in Rust (`src/wire.rs`) and tested there.
"""

from __future__ import annotations

import subprocess
import sys

import msgpack

from arena_composer.viewer_process import pack_scene

CHILD = [sys.executable, "-m", "arena_composer.viewer_process"]


def test_a_whole_scene_reaches_the_decoder():
    """`assets` sits behind two megabytes of padding, so the decoder reaches it
    — and rejects it — only by reading the whole payload off the pipe."""
    scene = msgpack.packb({"pad": "x" * (2 << 20), "assets": 7})
    done = subprocess.run(CHILD, input=pack_scene(scene), capture_output=True)
    assert done.returncode == 1
    assert b"invalid type: integer `7`" in done.stderr


def test_a_layout_naming_geometry_the_window_never_had_is_refused():
    """A layout names by its id alone only what the layout before it drew. The
    first has none before it, so naming one there means the two ends disagree
    on what the window holds, and the reader says so rather than drawing the
    asset as nothing."""
    from arena_composer.payload import pack

    layout = {"assets": [], "geometries": [{"id": "ab12", "nodes": []}]}
    done = subprocess.run(CHILD, input=pack_scene(pack(layout, held={"ab12"})), capture_output=True)
    assert done.returncode == 1
    assert b"geometry ab12 is named by its id" in done.stderr


def test_an_empty_pipe_is_not_a_scene():
    done = subprocess.run(CHILD, input=b"", capture_output=True)
    assert done.returncode == 1
    assert b"no scene arrived on stdin" in done.stderr

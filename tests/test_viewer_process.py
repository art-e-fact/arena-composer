"""The pipe `arena-view` hands scenes to the viewer down.

Only the child's end of it: framing, and what reaches the decoder. Drawing
needs a display, and none of these get that far — a scene is rejected or
accepted before a window is asked for.
"""

from __future__ import annotations

import io
import subprocess
import sys

import msgpack

from arena_composer.viewer_process import pack_frame, read_frame

CHILD = [sys.executable, "-m", "arena_composer.viewer_process"]

# Payloads run to megabytes, so no read may assume it got the whole body.
BIG = bytes(range(256)) * 8192


def test_frames_are_read_back_one_at_a_time():
    stream = io.BytesIO(pack_frame(BIG) + pack_frame(b"the next scene"))
    assert read_frame(stream) == BIG
    assert read_frame(stream) == b"the next scene"
    assert read_frame(stream) is None


def test_a_whole_frame_reaches_the_decoder():
    """The decoder rejects `assets`, which it only reaches by reading past the
    padding in front of it — a short read would fail before ever seeing it."""
    scene = msgpack.packb({"pad": "x" * len(BIG), "assets": 7})
    done = subprocess.run(CHILD, input=pack_frame(scene), capture_output=True)
    assert done.returncode == 1
    assert b"invalid type: integer `7`" in done.stderr


def test_an_empty_pipe_is_not_a_scene():
    done = subprocess.run(CHILD, input=b"", capture_output=True)
    assert done.returncode == 2
    assert b"no scene" in done.stderr

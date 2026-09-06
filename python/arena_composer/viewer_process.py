"""The viewer child process: draws packed scenes, and nothing else.

``arena-view`` runs this as ``python -m arena_composer.viewer_process`` so the
Bevy window gets a main thread of its own — its event loop wants one and never
returns — while the parent stays free to do everything else. Scenes arrive on
stdin as length-prefixed frames; the parent keeps the pipe open after the first
one, which is where later scenes will come from.
"""

from __future__ import annotations

import sys
from typing import BinaryIO

#: Bytes of big-endian length in front of every frame body.
HEADER = 4


def pack_frame(body: bytes) -> bytes:
    """Frame one payload for the pipe."""
    return len(body).to_bytes(HEADER, "big") + body


def read_frame(stream: BinaryIO) -> bytes | None:
    """Read one frame off `stream`, or None once it ends.

    `stream` must be buffered: bodies are megabytes, and only a buffered read
    is guaranteed to return all of them rather than one pipe-sized chunk.
    """
    header = stream.read(HEADER)
    if len(header) < HEADER:
        return None
    return stream.read(int.from_bytes(header, "big"))


def main() -> int:
    payload = read_frame(sys.stdin.buffer)
    if payload is None:
        print("arena-view: no scene arrived on stdin", file=sys.stderr)
        return 2

    from arena_composer import _core

    # Blocks until the window is closed.
    _core.run_viewer(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())

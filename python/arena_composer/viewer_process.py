"""The viewer child process: draws the scenes it is sent, and nothing else.

``arena-view`` runs this as ``python -m arena_composer.viewer_process`` so the
Bevy window gets a main thread of its own — its event loop wants one and never
returns.

Scenes arrive on stdin, each one its byte length in four big-endian bytes
followed by that much MessagePack. A pipe has no message boundaries of its own:
two payloads written back to back are one run of bytes, and the length is what
says where the first ends. Closing the pipe closes the window.
"""

from __future__ import annotations

import sys

#: Bytes of big-endian length in front of every scene.
HEADER = 4


def pack_scene(payload: bytes) -> bytes:
    """Frame one packed scene for the pipe."""
    return len(payload).to_bytes(HEADER, "big") + payload


def main() -> int:
    from arena_composer import _core

    # Blocks until the window is closed.
    _core.run_viewer_stdin()
    return 0


if __name__ == "__main__":
    sys.exit(main())

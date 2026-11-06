"""The viewer child process: draws the scenes it is sent, and nothing else.

``arena-view`` runs this as ``python -m arena_composer.viewer_process`` so the
Bevy window gets a main thread of its own — its event loop wants one and never
returns.

Scenes arrive on stdin, each one its byte length in four big-endian bytes
followed by that much MessagePack. A pipe has no message boundaries of its own:
two payloads written back to back are one run of bytes, and the length is what
says where the first ends. Closing the pipe closes the window.

The parent's end of that is :class:`ChildViewer`, one :class:`Viewer` of
any: ``view.serve`` draws in whatever carries the same messages to a window,
a browser tab's WebSocket included.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from typing import Protocol

#: Bytes of big-endian length in front of every scene.
HEADER = 4


def pack_scene(payload: bytes) -> bytes:
    """Frame one packed scene for the pipe."""
    return len(payload).to_bytes(HEADER, "big") + payload


class Viewer(Protocol):
    """A window, as ``view.serve`` talks to it: one MessagePack message per
    `send` and per `recv`, unframed. However the transport frames them is its
    own business."""

    #: None while the window is open, and its exit status once it has closed.
    returncode: int | None

    async def send(self, message: bytes) -> None: ...

    async def recv(self) -> bytes | None:
        """The panel's next request, or None once the window has closed."""
        ...

    def terminate(self) -> None:
        """Close the window. Safe once it has closed itself."""
        ...

    async def wait(self) -> int:
        """Wait for the window to close, and return its exit status."""
        ...


class ChildViewer:
    """The window as a child process running :func:`main`, over its stdin and
    stdout."""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self._proc = proc

    @classmethod
    async def spawn(cls) -> ChildViewer:
        return cls(
            await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                __name__,
                stdin=asyncio.subprocess.PIPE,
                # The editor's requests come back this way. Bevy logs to
                # stderr, so stdout carries nothing else — see `src/wire.rs`.
                stdout=asyncio.subprocess.PIPE,
            )
        )

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode

    async def send(self, message: bytes) -> None:
        """Safe to call from more than one task: the whole framed message goes
        into the transport in a single `write`, so two senders cannot
        interleave a length with somebody else's body."""
        assert self._proc.stdin is not None
        self._proc.stdin.write(pack_scene(message))
        await self._proc.stdin.drain()

    async def recv(self) -> bytes | None:
        assert self._proc.stdout is not None
        try:
            header = await self._proc.stdout.readexactly(HEADER)
            return await self._proc.stdout.readexactly(int.from_bytes(header, "big"))
        except asyncio.IncompleteReadError:
            return None

    def terminate(self) -> None:
        if self._proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._proc.terminate()

    async def wait(self) -> int:
        return await self._proc.wait()


def main() -> int:
    from arena_composer import _core

    # Blocks until the window is closed.
    _core.run_viewer_stdin()
    return 0


if __name__ == "__main__":
    sys.exit(main())

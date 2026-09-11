"""How `arena-view` answers an edit: the spec at once, the layout once solved.

The solve is seconds and the write that precedes it is milliseconds, so the two
answers are on separate paths — which is also what lets a burst of edits share
one solve instead of paying for one each.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import msgpack
import pytest

from arena_composer import view, viewer_process


class _Viewer:
    """Enough of the viewer process to talk to: edits go in through `stdout`,
    answers come back through `stdin`, framed the way the pipe frames them."""

    returncode = None

    def __init__(self) -> None:
        self.answers: list[dict] = []
        self.stdout = asyncio.StreamReader()
        self.stdin = SimpleNamespace(
            # One `_send` is one `write` of one whole message.
            write=lambda data: self.answers.append(msgpack.unpackb(data[viewer_process.HEADER :])),
            drain=self._drained,
        )

    async def _drained(self) -> None:
        pass

    def edit(self, value: str) -> None:
        packed = msgpack.packb({"op": "set", "path": ["env_name"], "value": value}, use_bin_type=True)
        self.stdout.feed_data(viewer_process.pack_scene(packed))


async def _until(predicate) -> None:
    while not predicate():
        await asyncio.sleep(0.01)


def test_edits_are_answered_while_the_solver_is_still_busy(monkeypatch, tmp_path: Path):
    """Three edits, answered three times, against one solve still running — and
    then a second solve carrying the two that arrived while it was.

    A solve per edit is what this replaces: the third edit would have waited on
    the first two, which is seconds of a panel that will not move.
    """
    solving = threading.Event()
    solves: list[int] = []

    def resolve(spec: Path, seed: int) -> bytes:
        solves.append(seed)
        assert solving.wait(timeout=5), "the test never released the solver"
        return msgpack.packb({"assets": [], "spec": {"env_name": "solved"}}, use_bin_type=True)

    monkeypatch.setattr(view, "_resolve", resolve)
    monkeypatch.setattr(view.spec_edit, "apply", lambda *_: SimpleNamespace(ok=True, errors=[], changed=["set"]))
    monkeypatch.setattr(
        view,
        "encode_spec",
        lambda spec, errors, solving=False: msgpack.packb(
            {"spec": {"env_name": "edited"}, "errors": list(errors), "solving": solving}, use_bin_type=True
        ),
    )

    async def run() -> None:
        viewer = _Viewer()
        reading = asyncio.create_task(view._apply_edits(tmp_path / "task.yaml", 42, viewer))
        for value in ("one", "two", "three"):
            viewer.edit(value)

        await asyncio.wait_for(_until(lambda: len(viewer.answers) == 3), timeout=5)
        assert [answer["solving"] for answer in viewer.answers] == [True, True, True]
        assert [answer["spec"]["env_name"] for answer in viewer.answers] == ["edited"] * 3
        assert solves == [42], "the answers waited on a solve, or one ran per edit"

        solving.set()
        # The two edits that landed mid-solve are answered together by the next.
        await asyncio.wait_for(_until(lambda: len(solves) == 2), timeout=5)
        await asyncio.wait_for(_until(lambda: any(a.get("assets") is not None for a in viewer.answers)), timeout=5)

        viewer.stdout.feed_eof()
        await asyncio.wait_for(reading, timeout=5)

    asyncio.run(run())


def test_a_refused_edit_is_answered_without_a_solve(monkeypatch, tmp_path: Path):
    """The spec did not change, so there is nothing to re-resolve — the answer
    is that same spec carrying the reason, and no solve is coming."""
    monkeypatch.setattr(view, "_resolve", lambda *_: pytest.fail("a refused edit must not resolve"))
    monkeypatch.setattr(
        view.spec_edit, "apply", lambda *_: SimpleNamespace(ok=False, errors=["objects.0: named by a task"], changed=[])
    )
    monkeypatch.setattr(
        view,
        "encode_spec",
        lambda spec, errors, solving=False: msgpack.packb(
            {"spec": {"env_name": "unchanged"}, "errors": list(errors), "solving": solving}, use_bin_type=True
        ),
    )

    async def run() -> None:
        viewer = _Viewer()
        viewer.edit("nope")
        viewer.stdout.feed_eof()
        await asyncio.wait_for(view._apply_edits(tmp_path / "task.yaml", 42, viewer), timeout=5)
        assert [a["errors"] for a in viewer.answers] == [["objects.0: named by a task"]]
        assert viewer.answers[0]["solving"] is False
        assert viewer.answers[0]["spec"]["env_name"] == "unchanged"

    asyncio.run(run())

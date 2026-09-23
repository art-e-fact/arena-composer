"""How `arena-view` answers what the editor sends.

An edit is answered twice: the spec at once, the layout once solved. The solve
is seconds and the write that precedes it is milliseconds, so the two answers
are on separate paths — which is also what lets a burst of edits share one solve
instead of paying for one each.

A crawl is neither written nor solved. It is minutes of network against
NVIDIA's bucket, so it is served beside the edits, and it is answered by the
prop list — the only thing it changes. The pass that catches the registered
assets up on their pictures is answered the same way, and runs beside both.
Neither says anything about the last edit or its solve.
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

    def ask(self, request: dict) -> None:
        self.stdout.feed_data(viewer_process.pack_scene(msgpack.packb(request, use_bin_type=True)))

    def edit(self, value: str) -> None:
        self.ask({"op": "set", "path": ["env_name"], "value": value})


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
        lambda spec, errors, solving=False, remote=False: msgpack.packb(
            {"spec": {"env_name": "edited"}, "errors": list(errors), "solving": solving}, use_bin_type=True
        ),
    )

    async def run() -> None:
        viewer = _Viewer()
        reading = asyncio.create_task(view._serve_requests(tmp_path / "task.yaml", 42, viewer))
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


def test_a_layout_is_followed_by_the_spec_as_the_file_now_reads(monkeypatch, tmp_path: Path):
    """A layout carries the spec its own solve began from, and an edit made
    during those seconds leaves that copy behind. So the panel is answered
    again, from the file, behind the layout."""
    monkeypatch.setattr(
        view,
        "encode_spec",
        lambda spec, errors, solving=False, remote=False: msgpack.packb(
            {"spec": {"env_name": spec.read_text()}, "errors": list(errors), "solving": solving}, use_bin_type=True
        ),
    )
    spec = tmp_path / "task.yaml"
    spec.write_text("current")
    layout = msgpack.packb({"assets": [], "spec": {"env_name": "as the solve began"}}, use_bin_type=True)

    async def run() -> None:
        viewer = _Viewer()
        await view._draw(viewer, spec, layout, solving=True)
        assert [a.get("assets") is not None for a in viewer.answers] == [True, False], "expected a layout, then a spec"
        assert viewer.answers[0]["spec"]["env_name"] == "as the solve began", "the layout was rewritten"
        assert viewer.answers[1]["spec"]["env_name"] == "current", "the file was not read again"
        # Another solve is queued, so the panel goes on saying the view is behind.
        assert viewer.answers[1]["solving"] is True

        # A solve that produced nothing still answers, with the reason.
        viewer.answers.clear()
        await view._draw(viewer, spec, None, ["Exception: no layout"])
        assert [a["errors"] for a in viewer.answers] == [["Exception: no layout"]]

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
        lambda spec, errors, solving=False, remote=False: msgpack.packb(
            {"spec": {"env_name": "unchanged"}, "errors": list(errors), "solving": solving}, use_bin_type=True
        ),
    )

    async def run() -> None:
        viewer = _Viewer()
        viewer.edit("nope")
        viewer.stdout.feed_eof()
        await asyncio.wait_for(view._serve_requests(tmp_path / "task.yaml", 42, viewer), timeout=5)
        assert [a["errors"] for a in viewer.answers] == [["objects.0: named by a task"]]
        assert viewer.answers[0]["solving"] is False
        assert viewer.answers[0]["spec"]["env_name"] == "unchanged"

    asyncio.run(run())


def _packed(spec, errors=None, solving=None, remote=False, indexing=None) -> bytes:
    """Stand in for `payload.encode_spec`, which would import Arena."""
    return msgpack.packb(
        {"errors": errors, "solving": solving, "remote": remote, "indexing": indexing}, use_bin_type=True
    )


def _served(monkeypatch, request: dict, tmp_path: Path) -> list[dict]:
    """Hand the viewer one asset request and return what it is answered with."""
    monkeypatch.setattr(view, "encode_spec", _packed)
    monkeypatch.setattr(
        view.spec_edit, "apply", lambda *_: pytest.fail("an asset request must not be written to the spec")
    )

    async def run() -> list[dict]:
        viewer = _Viewer()
        reading = asyncio.create_task(view._serve_requests(tmp_path / "task.yaml", 42, viewer))
        viewer.ask(request)
        # The prop list is the last word on any of these, so waiting for one
        # waits for the request to be served through.
        await asyncio.wait_for(_until(lambda: any(a["remote"] for a in viewer.answers)), timeout=5)
        viewer.stdout.feed_eof()
        await asyncio.wait_for(reading, timeout=5)
        return viewer.answers

    return asyncio.run(run())


def test_a_crawl_says_it_started_and_answers_with_the_prop_list(monkeypatch, tmp_path: Path):
    """The button stops taking clicks when the crawl is asked for, not when it
    gets around to running — a minute of crawling is a minute of clicks, and
    each one would queue another crawl behind the first."""
    found = [SimpleNamespace(usd_path="https://bucket/Baskets/basket.usd")]
    fetched: list[list[str]] = []
    monkeypatch.setattr(view.simready, "build", lambda: found)
    monkeypatch.setattr(view.simready, "fetch_thumbnails", lambda usd_paths: fetched.append(usd_paths) or 1)

    answers = _served(monkeypatch, {"op": "reindex"}, tmp_path)
    assert fetched == [["https://bucket/Baskets/basket.usd"]], "the crawl did not fetch what it indexed"
    assert [answer["indexing"] for answer in answers] == [True, False]
    assert [answer["remote"] for answer in answers] == [False, True], "the crawl produced no list"
    # A crawl knows nothing of the last edit, so it must not wipe its refusal
    # or stop the spinner of the solve behind it.
    assert [(answer["errors"], answer["solving"]) for answer in answers] == [(None, None)] * 2


def test_a_failed_crawl_still_says_it_stopped(monkeypatch, tmp_path: Path):
    """Otherwise the spinner outlives the crawl and the button never returns."""

    def refuse() -> list:
        raise OSError("bucket unreachable")

    monkeypatch.setattr(view.simready, "build", refuse)

    answers = _served(monkeypatch, {"op": "reindex"}, tmp_path)
    assert answers[-1]["errors"] == ["OSError: bucket unreachable"]
    assert answers[-1]["indexing"] is False, "the browser is still waiting on the crawl"


def test_an_answer_outlives_a_spec_being_written(monkeypatch, tmp_path: Path):
    """An edit's answer can be dropped when the spec will not parse, because the
    watcher sends another. This one is a request's only reply."""
    attempts: list[int] = []

    def mid_write(spec, errors=None, solving=None, remote=False, indexing=None) -> bytes:
        attempts.append(1)
        if len(attempts) < 3:
            raise ValueError("while scanning a simple key")
        return _packed(spec, errors, solving, remote, indexing)

    monkeypatch.setattr(view, "SETTLE", 0.01)
    monkeypatch.setattr(view.simready, "build", list)
    monkeypatch.setattr(view.simready, "fetch_thumbnails", len)
    monkeypatch.setattr(view, "encode_spec", mid_write)
    monkeypatch.setattr(
        view.spec_edit, "apply", lambda *_: pytest.fail("an asset request must not be written to the spec")
    )

    async def run() -> list[dict]:
        viewer = _Viewer()
        reading = asyncio.create_task(view._serve_requests(tmp_path / "task.yaml", 42, viewer))
        viewer.ask({"op": "reindex"})
        await asyncio.wait_for(_until(lambda: any(a["remote"] for a in viewer.answers)), timeout=5)
        viewer.stdout.feed_eof()
        await asyncio.wait_for(reading, timeout=5)
        return viewer.answers

    answers = asyncio.run(run())
    assert answers[-1]["indexing"] is False, "the crawl's answer was lost with the write"


def test_the_startup_pass_offers_the_registered_assets_alone(monkeypatch, tmp_path: Path):
    """The registered assets are in no index, so nothing but this pass would
    ever fetch their pictures — and they are what the browser opens on. The
    props are left to the reindex button: an index built before its pictures
    were, or by the MCP server, is up to 170 MB nobody asked for here.

    A pass that found nothing answers with nothing: the prop list is a
    megabyte, and on a machine that has fetched them there is nothing to say."""
    offered: list[str] = []

    def fetch(usd_paths, found: list[int]) -> int:
        offered.extend(usd_paths)
        return found.pop()

    monkeypatch.setattr(view, "encode_spec", _packed)
    monkeypatch.setattr(view.catalog, "catalog", lambda: (SimpleNamespace(usd_path="https://bucket/ycb.usd"),))
    monkeypatch.setattr(view.catalog, "remote", lambda: pytest.fail("the props were offered at startup"))

    async def run(found: list[int]) -> list[dict]:
        monkeypatch.setattr(view.simready, "fetch_thumbnails", lambda paths: fetch(paths, found))
        viewer = _Viewer()
        await view._catch_up_on_pictures(tmp_path / "task.yaml", viewer)
        return viewer.answers

    answers = asyncio.run(run([1]))
    assert [(answer["remote"], answer["errors"], answer["solving"]) for answer in answers] == [(True, None, None)]
    assert offered == ["https://bucket/ycb.usd"]
    assert asyncio.run(run([0])) == [], "a pass that fetched nothing still sent a prop list"


def test_a_failed_startup_pass_costs_the_pictures_and_nothing_else(monkeypatch, tmp_path: Path):
    """Its task is awaited when the window closes, so an exception it let
    through would surface there as a traceback in place of an exit status."""

    def unreachable(usd_paths) -> int:
        raise OSError("bucket unreachable")

    monkeypatch.setattr(view.catalog, "catalog", tuple)
    monkeypatch.setattr(view.simready, "fetch_thumbnails", unreachable)

    async def run() -> list[dict]:
        viewer = _Viewer()
        await view._catch_up_on_pictures(tmp_path / "task.yaml", viewer)
        return viewer.answers

    assert asyncio.run(run()) == []

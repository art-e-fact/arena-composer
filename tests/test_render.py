"""One-shot offscreen renders.

Only the part that runs without a GPU. Drawing itself is checked by the test at
the bottom, which needs a packed scene and a working adapter and so is opt-in.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from arena_composer import render

PAYLOAD = os.environ.get("ARENA_COMPOSER_PAYLOAD")


def test_an_unknown_view_is_refused_before_a_process_starts(tmp_path: Path):
    """Naming the views is the whole camera API, so a typo has to say so rather
    than spend a second on Bevy first."""
    with pytest.raises(ValueError, match="topdown"):
        render.render(b"", tmp_path, ["top", "topdown"])
    assert not list(tmp_path.iterdir())


def test_no_views_is_refused(tmp_path: Path):
    with pytest.raises(ValueError, match="no views"):
        render.render(b"", tmp_path, [])


def test_a_failed_render_reports_what_it_did_not_write(tmp_path: Path):
    """An empty payload cannot decode. The error has to name the missing image
    and carry the child's own complaint, or there is nothing to act on."""
    with pytest.raises(RuntimeError, match="top.png"):
        render.render(b"", tmp_path, ["top"])


def test_a_stale_image_is_not_passed_off_as_a_fresh_one(tmp_path: Path):
    """A failed render must not leave the previous run's picture behind, or the
    caller reads an old layout as the current one."""
    (tmp_path / "top.png").write_bytes(b"an earlier render")
    with pytest.raises(RuntimeError):
        render.render(b"", tmp_path, ["top"])
    assert not (tmp_path / "top.png").exists()


def test_an_error_in_the_log_fails_the_render_that_wrote_a_picture(tmp_path: Path, monkeypatch):
    """Bevy draws the scene around an asset it could not load and writes the
    image anyway, so the failure arrives as a good-looking frame with an object
    missing from it. Only the log says which of those it is."""
    log = (
        b"2026-09-07T15:53:02.256096Z  INFO bevy_winit::system: Creating new window\n"
        b"2026-09-07T15:53:02.268252Z ERROR bevy_asset::server: "
        b"Failed to load asset 'apple_01.jpg': invalid image extension: jpg\n"
    )

    def drew_it_anyway(argv, **kwargs):
        (tmp_path / "top.png").write_bytes(b"a scene without its apple")
        return subprocess.CompletedProcess(argv, 0, b"", log)

    monkeypatch.setattr(render.subprocess, "run", drew_it_anyway)
    with pytest.raises(RuntimeError, match="apple_01.jpg"):
        render.render(b"a payload", tmp_path, ["top"])


@pytest.mark.skipif(not PAYLOAD, reason="needs ARENA_COMPOSER_PAYLOAD and a GPU")
def test_every_view_draws_something_different(tmp_path: Path):
    """The whole draw path: four cameras, four images, none of them the empty
    frame that a capture taken before the scene is ready comes back as.

    Make a payload first:
    `arena-resolve-layout <spec>.yaml --payload scene.msgpack`, then
    `ARENA_COMPOSER_PAYLOAD=scene.msgpack pytest arena_composer/tests`.
    """
    paths = render.render(Path(PAYLOAD).read_bytes(), tmp_path, render.VIEWS, size=256)

    assert [path.name for path in paths] == [f"{view}.png" for view in render.VIEWS]
    images = [path.read_bytes() for path in paths]
    assert all(images), "a view came back empty"
    # An identical pair means a camera drew nothing: a blank frame is blank
    # from every angle.
    assert len(set(images)) == len(images), "two views produced the same image"


@pytest.mark.skipif(not PAYLOAD, reason="needs ARENA_COMPOSER_PAYLOAD and a GPU")
def test_a_repeated_view_is_drawn_once(tmp_path: Path):
    """Two of the same view name one file, and the renderer skips a duplicate
    target — so they are collapsed before it sees them."""
    assert len(render.render(Path(PAYLOAD).read_bytes(), tmp_path, ["top", "top"], size=256)) == 1

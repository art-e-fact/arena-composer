"""One-shot offscreen renders, and `arena-render`.

Only the part that runs without a GPU or Arena. Drawing itself is checked by
the tests at the bottom, which need a packed scene and a working adapter and so
are opt-in.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from arena_composer import cli, render, spec_edit

PAYLOAD = os.environ.get("ARENA_COMPOSER_PAYLOAD")


def test_an_unknown_view_is_refused_before_a_process_starts(tmp_path: Path):
    """Naming the views is the whole camera API, so a typo has to say so rather
    than spend a second on Bevy first."""
    with pytest.raises(ValueError, match="topdown"):
        render.render(b"", {"top": tmp_path / "top.png", "topdown": tmp_path / "topdown.png"})
    assert not list(tmp_path.iterdir())


def test_no_views_is_refused(tmp_path: Path):
    with pytest.raises(ValueError, match="no views"):
        render.render(b"", {})


def test_a_failed_render_reports_what_it_did_not_write(tmp_path: Path):
    """An empty payload cannot decode. The error has to name the missing image
    and carry the child's own complaint, or there is nothing to act on."""
    with pytest.raises(RuntimeError, match="top.png"):
        render.render(b"", {"top": tmp_path / "top.png"})


def test_a_stale_image_is_not_passed_off_as_a_fresh_one(tmp_path: Path):
    """A failed render must not leave the previous run's picture behind, or the
    caller reads an old layout as the current one."""
    (tmp_path / "top.png").write_bytes(b"an earlier render")
    with pytest.raises(RuntimeError):
        render.render(b"", {"top": tmp_path / "top.png"})
    assert not (tmp_path / "top.png").exists()


def test_an_error_in_the_log_fails_the_render_that_wrote_a_picture(tmp_path: Path, monkeypatch):
    """Bevy draws the scene around an asset it could not load and writes the
    image anyway, so the failure arrives as a good-looking frame with an object
    missing from it. Only the log says which of those it is, so the picture
    goes too: left on disk, it would answer for the layout to whoever looks."""
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
        render.render(b"a payload", {"top": tmp_path / "top.png"})
    assert not (tmp_path / "top.png").exists()


def test_a_render_that_stops_partway_leaves_no_picture(tmp_path: Path, monkeypatch):
    """A child that dies after its first screenshot wrote one view of two. The
    one it wrote goes with the failure, or it would pass for this layout's."""

    def died_after_one(argv, **kwargs):
        (tmp_path / "top.png").write_bytes(b"half a render")
        return subprocess.CompletedProcess(argv, 101, b"", b"thread 'main' panicked\n")

    monkeypatch.setattr(render.subprocess, "run", died_after_one)
    with pytest.raises(RuntimeError, match="front.png"):
        render.render(b"a payload", {"top": tmp_path / "top.png", "front": tmp_path / "front.png"})
    assert not list(tmp_path.iterdir())


def _spec(path: Path) -> Path:
    """A file `arena-render` takes for a task: it has a `task` key."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("task: {}\n")
    return path


@pytest.mark.parametrize(
    "argv, refusal",
    [
        (["good.yaml", "--view", "topdown"], "invalid choice: 'topdown'"),
        (["missing.yaml"], "no spec at missing.yaml"),
        (["scene.yaml"], "so it is a scene"),
        (["good.yaml", "other/good.yaml"], "more than one spec is named good"),
        (["good.yaml", "--size", "0"], "--size must be at least 1"),
        (["good.yaml", "-o", "afile"], "cannot write to --out-dir afile"),
    ],
)
def test_a_wrong_argument_is_refused_before_arena_is_imported(argv, refusal, tmp_path, monkeypatch, capsys):
    """A typo has to fail in the time it takes to read the files, not after the
    seconds Arena takes to import, and draw nothing on the way."""
    monkeypatch.chdir(tmp_path)
    _spec(tmp_path / "good.yaml")
    _spec(tmp_path / "other" / "good.yaml")
    (tmp_path / "scene.yaml").write_text("objects: []\n")
    (tmp_path / "afile").write_text("not a directory")

    def imported(*args):
        raise AssertionError("Arena was imported before the arguments were checked")

    monkeypatch.setattr(cli, "apply_spec_options", imported)
    with pytest.raises(SystemExit) as exit:
        render.main(argv)
    assert exit.value.code == 2
    assert refusal in capsys.readouterr().err
    assert not list(tmp_path.glob("*.png"))


def test_a_spec_that_does_not_build_is_reported_and_the_rest_are_drawn(tmp_path, monkeypatch, capsys):
    """Several specs share one run, so one broken spec costs its own pictures
    and no one else's: its reasons on stderr, the others' paths on stdout, and
    exit 1. Its picture from an earlier run goes too, or a glob over the
    directory would take it for this run's. A spec named twice, in two spellings,
    is drawn once."""
    good, broken = _spec(tmp_path / "good.yaml"), _spec(tmp_path / "broken.yaml")
    out = tmp_path / "renders"
    out.mkdir()
    (out / "broken-top.png").write_bytes(b"an earlier render")

    def resolve(spec, seed):
        # The solver's progress, which must not land among the paths.
        print("[placement] Validated 1 layout")
        if spec == broken:
            raise spec_edit.SpecError(["objects.0: no file at bowl.usd"])
        return {"env_name": spec.stem}

    def drawn(payload, shots, size):
        for path in shots.values():
            path.write_bytes(payload)

    monkeypatch.setattr(cli, "apply_spec_options", lambda parser, args: None)
    monkeypatch.setitem(
        sys.modules,
        "arena_composer.resolve_layout",
        SimpleNamespace(resolve_layout_from_yaml=resolve, summary=lambda dump: f"{dump['env_name']} solved"),
    )
    monkeypatch.setitem(sys.modules, "arena_composer.payload", SimpleNamespace(encode=lambda dump, remote: b"png"))
    monkeypatch.setattr(render, "render", drawn)

    # `good` again, spelled through `renders/..`: the same file, drawn once.
    again = out / ".." / "good.yaml"
    argv = [str(good), str(broken), str(again), "--view", "top", "--view", "front", "-o", str(out)]
    assert render.main(argv) == 1
    stdout, stderr = capsys.readouterr()
    assert stdout.splitlines() == [str(out / "good-top.png"), str(out / "good-front.png")]
    assert f"{broken}: objects.0: no file at bowl.usd" in stderr
    assert f"{good}: good solved" in stderr
    assert "[placement] Validated 1 layout" in stderr
    assert sorted(path.name for path in out.iterdir()) == ["good-front.png", "good-top.png"]


def test_a_module_that_does_not_import_leaves_the_last_pictures(tmp_path, monkeypatch):
    """Nothing was attempted, so nothing of the last run is stale: only a spec
    this run fails on loses its pictures."""
    spec = _spec(tmp_path / "good.yaml")
    (tmp_path / "good-default.png").write_bytes(b"an earlier render")

    monkeypatch.setattr(cli, "apply_spec_options", lambda parser, args: parser.exit(1, "no my_assets\n"))
    with pytest.raises(SystemExit):
        render.main([str(spec), "-o", str(tmp_path)])
    assert (tmp_path / "good-default.png").read_bytes() == b"an earlier render"


@pytest.mark.skipif(not PAYLOAD, reason="needs ARENA_COMPOSER_PAYLOAD and a GPU")
def test_every_view_draws_something_different(tmp_path: Path):
    """The whole draw path: four cameras, four images, none of them the empty
    frame that a capture taken before the scene is ready comes back as.

    Make a payload first:
    `arena-resolve-layout <spec>.yaml --payload scene.msgpack`, then
    `ARENA_COMPOSER_PAYLOAD=scene.msgpack pytest`.
    """
    shots = {view: tmp_path / f"{view}.png" for view in render.VIEWS}
    render.render(Path(PAYLOAD).read_bytes(), shots, size=256)

    images = [path.read_bytes() for path in shots.values()]
    assert all(images), "a view came back empty"
    # An identical pair means a camera drew nothing: a blank frame is blank
    # from every angle.
    assert len(set(images)) == len(images), "two views produced the same image"

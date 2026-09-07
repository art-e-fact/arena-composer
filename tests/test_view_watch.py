"""The set of files `arena-view` polls for a reload."""

from __future__ import annotations

from pathlib import Path

from arena_composer.view import _watched


def _spec(tmp_path: Path, body: str) -> Path:
    (tmp_path / "tasks").mkdir(exist_ok=True)
    (tmp_path / "scenes").mkdir(exist_ok=True)
    (tmp_path / "scenes" / "kitchen.yaml").write_text("env_name: kitchen\n")
    spec = tmp_path / "tasks" / "task.yaml"
    spec.write_text(body)
    return spec


def test_the_included_scene_is_watched_beside_the_spec(tmp_path: Path):
    spec = _spec(tmp_path, "external_yaml: ../scenes/kitchen.yaml\n")
    assert set(_watched(spec)) == {spec, tmp_path / "scenes" / "kitchen.yaml"}

    stamps = _watched(spec)
    (tmp_path / "scenes" / "kitchen.yaml").write_text("env_name: kitchen2\n")
    assert _watched(spec) != stamps, "a write to the scene must read as a change"


def test_a_spec_that_includes_nothing_watches_itself(tmp_path: Path):
    assert set(_watched(_spec(tmp_path, "env_name: solo\n"))) == {tmp_path / "tasks" / "task.yaml"}


def test_an_unparsable_spec_still_watches_itself(tmp_path: Path):
    # Caught mid-write: the poll must keep running until the file parses again.
    assert set(_watched(_spec(tmp_path, "external_yaml: [\n"))) == {tmp_path / "tasks" / "task.yaml"}

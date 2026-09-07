"""Structured edits to a spec and its included scene.

Everything here goes through the real validator: what makes these edits worth a
tool rather than a text edit is that they are checked, so a test that stubs the
check tests the uninteresting half.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from arena_composer import spec_edit

# Real registered names, because Arena validates both against live registries.
TABLE = "maple_table_robolab"
TASK = """\
# a comment that has to survive
env_name: banana_in_bowl
external_yaml: ../scenes/scene.yaml
task:
  composition: atomic
  description: pick up the banana and place it in the bowl
  subtasks:
  - kind: PickAndPlaceTask
    params:
      pick_up_object: banana
      destination_location: bowl
      background_scene: maple_table_robolab
"""
SCENE = f"""\
embodiment:
  id: droid
  registry_name: droid_abs_joint_pos
  params: {{}}
background:
  id: {TABLE}
  registry_name: {TABLE}
  params: {{}}
objects:
- id: banana
  registry_name: banana_ycb_robolab
  params: {{}}
- id: bowl
  registry_name: bowl_ycb_robolab
  params: {{}}
- id: distractor
  registry_name: apple_02_objaverse_robolab
  params: {{}}
relations:
- kind: is_anchor
  subject: {TABLE}
  reference: null
  params: {{}}
- kind: 'on'
  subject: banana
  reference: {TABLE}
  params: {{}}
- kind: 'on'
  subject: distractor
  reference: {TABLE}
  params: {{}}
"""


@pytest.fixture
def spec(tmp_path: Path) -> Path:
    """A task spec and the scene it includes, laid out as this repo's are."""
    (tmp_path / "tasks").mkdir()
    (tmp_path / "scenes").mkdir()
    (tmp_path / "scenes" / "scene.yaml").write_text(SCENE)
    path = tmp_path / "tasks" / "task.yaml"
    path.write_text(TASK)
    return path


def scene_of(spec: Path) -> Path:
    return spec.parent.parent / "scenes" / "scene.yaml"


def test_an_object_is_written_to_the_file_that_owns_objects(spec: Path):
    """`objects` lives in the scene, so that is the file an object goes in —
    not the spec the edit was addressed to."""
    result = spec_edit.add_object(spec, "apple", "apple_01_objaverse_robolab", on=TABLE)
    assert result.ok, result.errors
    assert result.wrote == [str(scene_of(spec))]
    assert spec.read_text() == TASK, "the task spec was rewritten for a scene edit"

    scene = yaml.safe_load(scene_of(spec).read_text())
    assert [obj["id"] for obj in scene["objects"]] == ["banana", "bowl", "distractor", "apple"]


def test_an_added_on_relation_survives_a_pyyaml_reload(spec: Path):
    """Arena reads these files with PyYAML, i.e. YAML 1.1, where a bare `on` is
    the boolean true. The `on` relation kind has to come back a string."""
    assert spec_edit.add_object(spec, "apple", "apple_01_objaverse_robolab", on=TABLE).ok

    relations = yaml.safe_load(scene_of(spec).read_text())["relations"]
    added = relations[-1]
    assert added["kind"] == "on", f"read back as {added['kind']!r}"
    assert (added["subject"], added["reference"]) == ("apple", TABLE)


def test_an_object_without_a_surface_is_added_unplaced(spec: Path):
    """No `on` means no relation: the object exists but takes no part in
    placement."""
    assert spec_edit.add_object(spec, "apple", "apple_01_objaverse_robolab").ok

    scene = yaml.safe_load(scene_of(spec).read_text())
    assert "apple" in [obj["id"] for obj in scene["objects"]]
    assert not [r for r in scene["relations"] if r["subject"] == "apple"]


def test_removing_an_object_takes_its_relations_with_it(spec: Path):
    """A relation naming a missing asset stops the spec loading at all, so the
    two have to go together."""
    result = spec_edit.remove_object(spec, "distractor")
    assert result.ok, result.errors

    scene = yaml.safe_load(scene_of(spec).read_text())
    assert [obj["id"] for obj in scene["objects"]] == ["banana", "bowl"]
    assert not [r for r in scene["relations"] if "distractor" in (r["subject"], r["reference"])]


def test_removing_an_object_a_task_needs_is_refused(spec: Path):
    """The task still names `banana`, and what to do about that is a decision
    about the task — so the edit stops rather than guessing."""
    before = (spec.read_text(), scene_of(spec).read_text())
    result = spec_edit.remove_object(spec, "banana")

    assert not result.ok
    assert any("banana" in error for error in result.errors), result.errors
    assert (spec.read_text(), scene_of(spec).read_text()) == before, "a refused edit wrote anyway"


def test_an_unregistered_asset_is_refused(spec: Path):
    before = scene_of(spec).read_text()
    result = spec_edit.add_object(spec, "apple", "not_a_real_asset", on=TABLE)

    assert not result.ok
    assert any("not_a_real_asset" in error for error in result.errors), result.errors
    assert scene_of(spec).read_text() == before


def test_a_duplicate_id_is_refused(spec: Path):
    result = spec_edit.add_object(spec, "banana", "apple_01_objaverse_robolab", on=TABLE)
    assert not result.ok
    assert "banana" in result.errors[0]


def test_an_unknown_surface_is_refused_naming_what_there_is(spec: Path):
    result = spec_edit.add_object(spec, "apple", "apple_01_objaverse_robolab", on="sideboard")
    assert not result.ok
    # The ids it could have used are the useful half of the message.
    assert "bowl" in result.errors[0] and "sideboard" in result.errors[0]


def test_an_edit_leaves_the_rest_of_the_file_alone(spec: Path):
    """Comments, key order and quoting are why this uses a round-trip parser at
    all: an agent's edit has to leave a file a human still wants to read."""
    assert spec_edit.add_object(spec, "apple", "apple_01_objaverse_robolab", on=TABLE).ok

    added = scene_of(spec).read_text()
    assert added.startswith(SCENE.split("objects:")[0]), "the untouched prefix changed"
    # `reference: null` is authored, and ruamel writes None as an empty value
    # unless told otherwise.
    assert added.count("reference: null") == 1
    assert spec.read_text().startswith("# a comment that has to survive")


def test_a_spec_without_an_include_edits_itself(tmp_path: Path):
    """One file holding everything is still a spec; the owner of `objects` is
    then the spec itself."""
    path = tmp_path / "solo.yaml"
    path.write_text(TASK.replace("external_yaml: ../scenes/scene.yaml\n", "") + SCENE)

    result = spec_edit.add_object(path, "apple", "apple_01_objaverse_robolab", on=TABLE)
    assert result.ok, result.errors
    assert result.wrote == [str(path)]
    assert "apple" in [obj["id"] for obj in yaml.safe_load(path.read_text())["objects"]]

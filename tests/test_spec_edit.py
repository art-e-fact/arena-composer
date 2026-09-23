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


LONG_DESCRIPTION = "pick up the banana and place it in the bowl, then pick up the apple and put it on the plate"


def test_a_long_description_is_not_refolded(spec: Path):
    """ruamel wraps at 80 columns unless told otherwise, and would refold every
    description longer than that — with a trailing space at the break — the
    first time an edit passed over the file."""
    spec.write_text(TASK.replace("pick up the banana and place it in the bowl", LONG_DESCRIPTION))
    before = spec.read_text()

    result = spec_edit.add_object(spec, "apple", "apple_01_objaverse_robolab", on=TABLE)
    assert result.ok, result.errors
    assert result.wrote == [str(scene_of(spec))]
    assert spec.read_text() == before, "a scene edit refolded the task spec"


def test_an_edit_lands_in_the_file_that_owns_its_section(spec: Path):
    """`apply` addresses the merged spec, so the path says nothing about which
    file holds it — `env_name` is the task's, `relations` the scene's."""
    assert spec_edit.apply(spec, "set", ["env_name"], "renamed").wrote == [str(spec)]

    result = spec_edit.apply(spec, "set", ["relations", 1, "params", "clearance_m"], 0.03)
    assert result.wrote == [str(scene_of(spec))]

    scene = yaml.safe_load(scene_of(spec).read_text())
    assert scene["relations"][1]["params"] == {"clearance_m": 0.03}
    assert yaml.safe_load(spec.read_text())["env_name"] == "renamed"


def test_removing_an_entry_takes_its_relations_with_it(spec: Path):
    """A relation naming an asset that has gone stops the spec loading, so the
    cascade is what keeps a remove valid."""
    result = spec_edit.apply(spec, "remove", ["objects", 2])  # distractor
    assert result.ok, result.errors

    scene = yaml.safe_load(scene_of(spec).read_text())
    assert [obj["id"] for obj in scene["objects"]] == ["banana", "bowl"]
    assert all("distractor" not in (r["subject"], r.get("reference")) for r in scene["relations"])


def test_unsetting_a_parameter_drops_its_key(spec: Path):
    """Unset is the key gone, handing the parameter back to its default. The
    last key of a commented block goes too: ruamel alone writes the emptied
    mapping as a `{}` at column 0, which does not parse."""
    authored = f"reference: {TABLE}\n  params:\n    # hand-tuned\n    clearance_m: 0.03\n    edge_margin_m: 0.02\n"
    scene_of(spec).write_text(SCENE.replace(f"reference: {TABLE}\n  params: {{}}\n", authored, 1))

    for name in ("edge_margin_m", "clearance_m"):
        result = spec_edit.apply(spec, "remove", ["relations", 1, "params", name])
        assert result.ok, result.errors

    text = scene_of(spec).read_text()
    assert yaml.safe_load(text)["relations"][1]["params"] == {}
    assert "# hand-tuned" in text, "the author's comment was dropped"


def test_removing_a_parameter_cascades_nothing(spec: Path):
    """Only a whole entry owns an id. A parameter whose value is a mapping
    carrying one is not an object, and removing it drops no relation."""
    assert spec_edit.apply(spec, "set", ["objects", 0, "params", "probe"], {"id": TABLE}).ok
    before = yaml.safe_load(scene_of(spec).read_text())["relations"]

    assert spec_edit.apply(spec, "remove", ["objects", 0, "params", "probe"]).ok
    assert yaml.safe_load(scene_of(spec).read_text())["relations"] == before


def test_a_path_that_isnt_there_is_refused(spec: Path):
    """The editor can address a stale path — it draws the spec it was last
    sent. That has to come back as an error, not as an exception."""
    before = spec.read_text(), scene_of(spec).read_text()

    result = spec_edit.apply(spec, "set", ["relations", 99, "params", "x"], 1.0)
    assert not result.ok
    assert "relations.99" in result.errors[0]
    assert (spec.read_text(), scene_of(spec).read_text()) == before


def test_a_whole_entry_is_written_in_a_settled_key_order(spec: Path):
    """Changing a relation's kind replaces the entry, because the old kind's
    parameters mean nothing to the new one and whether there is a `reference`
    at all is the kind's to say. The entry arrives over MessagePack, where a
    mapping carries no order, so the written keys get a fixed one rather than
    a fresh one on every edit."""
    entry = {"params": {"side": "positive_y"}, "subject": "banana", "kind": "next_to", "reference": TABLE}
    assert spec_edit.apply(spec, "set", ["relations", 1], entry).ok

    # PyYAML builds a dict in document order, so this is the written order.
    written = yaml.safe_load(scene_of(spec).read_text())["relations"][1]
    assert written == entry
    assert list(written) == ["kind", "subject", "reference", "params"]


def test_renaming_an_object_carries_the_relations_naming_it(spec: Path):
    """Renaming leaves the relations pointing at a name nothing has, which
    stops the spec loading — so the rename has to reach them too."""
    result = spec_edit.apply(spec, "set", ["objects", 2, "id"], "extra")  # distractor
    assert result.ok, result.errors

    scene = yaml.safe_load(scene_of(spec).read_text())
    assert [obj["id"] for obj in scene["objects"]] == ["banana", "bowl", "extra"]
    assert [r["subject"] for r in scene["relations"] if r["subject"] == "extra"] == ["extra"]
    assert not [r for r in scene["relations"] if "distractor" in (r["subject"], r.get("reference"))]


def test_renaming_an_object_carries_the_task_params_naming_it(spec: Path):
    """Arena requires every string-valued task param to name a node, so a param
    reading as the old id is a reference to it — and the task is in the other
    file, which the rename still has to reach."""
    result = spec_edit.apply(spec, "set", ["objects", 0, "id"], "plantain")  # banana
    assert result.ok, result.errors

    params = yaml.safe_load(spec.read_text())["task"]["subtasks"][0]["params"]
    assert params["pick_up_object"] == "plantain"
    assert params["destination_location"] == "bowl", "an unrelated param was rewritten"


SIMREADY_CAN = (
    "https://omniverse-content-production.s3-us-west-2.amazonaws.com/Assets/Isaac/6.0/Isaac/SimReady"
    "/Residential/Kitchen/Food/Canned_Goods/Can_M01/sm_food_beverage_can_m01_01.usd"
)


def test_an_object_short_of_what_its_asset_needs_is_refused(spec: Path):
    """Arena checks that the name exists, not what the asset takes: a SimReady
    prop without its URL is a valid spec that fails to build."""
    before = scene_of(spec).read_text()
    result = spec_edit.add_object(spec, "can", "simready_usd_object", on=TABLE)
    assert not result.ok
    assert "usd_path" in result.errors[0], result.errors
    assert scene_of(spec).read_text() == before


def test_params_are_written_with_the_object(spec: Path):
    result = spec_edit.add_object(spec, "can", "simready_usd_object", on=TABLE, params={"usd_path": SIMREADY_CAN})
    assert result.ok, result.errors
    objects = yaml.safe_load(scene_of(spec).read_text())["objects"]
    assert {"id": "can", "registry_name": "simready_usd_object", "params": {"usd_path": SIMREADY_CAN}} in objects

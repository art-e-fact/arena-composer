"""Structured edits to a spec and its included scene.

Everything here goes through the real validator: what makes these edits worth a
tool rather than a text edit is that they are checked, so a test that stubs the
check tests the uninteresting half.
"""

from __future__ import annotations

import re
import sys
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
    """A task spec and the scene it includes, laid out in sibling directories."""
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


def test_an_edit_on_a_broken_spec_is_refused_only_for_what_it_introduces(spec: Path):
    """The deadlock this exists for: a task naming two objects the scene lacks.
    Arena's cross-reference check asserts and stops at the first, so adding
    `apple` surfaces `basket` — which a before/after diff of Arena's lines
    reads as a new error, and the spec can never be repaired. Comparing the
    dangling slots this module finds instead, adding `apple` lands with
    `basket` as what remains, and adding `basket` finishes the job."""
    spec.write_text(TASK.replace("pick_up_object: banana", "pick_up_object: apple").replace("destination_location: bowl", "destination_location: basket"))
    assert not spec_edit.check(spec).ok

    result = spec_edit.add_object(spec, "apple", "apple_01_objaverse_robolab", on=TABLE)
    assert result.ok, result.errors
    assert result.remaining_errors and all("basket" in line for line in result.remaining_errors), result.remaining_errors
    assert not any("apple" in line for line in result.remaining_errors), result.remaining_errors

    result = spec_edit.add_object(spec, "basket", "bowl_ycb_robolab", on=TABLE)
    assert result.ok and result.remaining_errors == [], result
    assert spec_edit.check(spec).ok


def test_what_an_edit_introduces_is_told_apart_from_what_was_wrong(spec: Path):
    """A refusal on a broken spec names only the new problem, and still reports
    the old one beside it, so the agent sees both without the one blocking
    the other."""
    spec.write_text(TASK.replace("pick_up_object: banana", "pick_up_object: apple"))
    before = scene_of(spec).read_text()

    result = spec_edit.add_object(spec, "can", "simready_usd_object", on=TABLE)
    assert not result.ok
    assert len(result.errors) == 1 and "usd_path" in result.errors[0], result.errors
    assert result.remaining_errors and "apple" in result.remaining_errors[0], result.remaining_errors
    assert scene_of(spec).read_text() == before, "a refused edit wrote anyway"


def test_a_second_entry_wrong_the_same_way_is_still_new(spec: Path):
    """Errors are compared as multisets with list indices normalised: a relation
    inserted above an already-dangling one must not make the old one read as
    new, and a new relation naming the same missing id must."""
    ghost = {"kind": "next_to", "subject": "banana", "reference": "ghost", "params": {"side": "positive_y"}}
    scene_of(spec).write_text(
        SCENE + "- kind: next_to\n  subject: banana\n  reference: ghost\n  params:\n    side: positive_y\n"
    )

    result = spec_edit.apply(spec, "insert", ["relations"], ghost)
    assert not result.ok
    assert result.errors == ["../scenes/scene.yaml: relations.4.reference: 'ghost' is not an id this spec declares"], result.errors

    result = spec_edit.apply(spec, "set", ["relations", 1, "params", "clearance_m"], 0.03)
    assert result.ok, result.errors
    assert result.remaining_errors == ["../scenes/scene.yaml: relations.3.reference: 'ghost' is not an id this spec declares"], result.remaining_errors

    result = spec_edit.apply(spec, "set", ["objects", 0, "params", "probe"], {"id": "x"})
    assert result.ok and len(result.remaining_errors) == 1, result


def test_a_good_spec_is_refused_for_anything_wrong_after(spec: Path):
    """Nothing to compare against: Arena's own lines come back as they are."""
    ghost = {"kind": "next_to", "subject": "banana", "reference": "ghost", "params": {"side": "positive_y"}}
    result = spec_edit.apply(spec, "insert", ["relations"], ghost)
    assert not result.ok and result.remaining_errors == []
    assert any("ghost" in line for line in result.errors), result.errors


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


def test_check_judges_the_bytes_on_disk_and_writes_nothing(spec: Path):
    """The check an edit passes before it lands, on the files as they are: a
    bare `on` left by a hand edit is the boolean true under YAML 1.1, which
    the in-memory document would not show. Nothing is written either way."""
    assert spec_edit.check(spec) == spec_edit.Result(ok=True)

    scene_of(spec).write_text(SCENE.replace("kind: 'on'", "kind: on", 1))
    before = spec.read_text(), scene_of(spec).read_text()
    result = spec_edit.check(spec)
    assert not result.ok
    assert any("relations.1.kind" in error and "string" in error for error in result.errors), result.errors
    assert (spec.read_text(), scene_of(spec).read_text()) == before


def test_check_names_what_a_pair_is_short_of(spec: Path):
    """Three ways a hand edit leaves a spec: a task param naming nothing, a
    scene that does not parse, an include that is not there. Each comes back
    as a result naming the file or the id, never as an exception."""
    spec.write_text(TASK.replace("pick_up_object: banana", "pick_up_object: plantain"))
    assert any("plantain" in error for error in spec_edit.check(spec).errors)

    scene_of(spec).write_text("objects: [\n")
    result = spec_edit.check(spec)
    assert not result.ok and "scene.yaml" in result.errors[0], result.errors

    spec.write_text(TASK.replace("scene.yaml", "gone.yaml"))
    result = spec_edit.check(spec)
    assert not result.ok and "gone.yaml" in result.errors[0], result.errors


def test_a_scene_is_refused_with_a_regex_that_finds_its_task(spec: Path):
    with pytest.raises(ValueError) as refused:
        spec_edit.check_entry(scene_of(spec))
    pattern = str(refused.value).rsplit(maxsplit=1)[-1]

    assert re.search(pattern, spec.read_text())
    assert not re.search(pattern, spec.read_text().replace("scene.yaml", "other_scene.yaml"))
    spec_edit.check_entry(spec)


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


@pytest.mark.parametrize("broken", ["task", "scene"])
def test_an_edit_on_a_spec_that_does_not_read_is_refused(spec: Path, broken: str):
    """A `task: [` left by a hand edit is the normal case mid-session, and has to
    come back as a refusal naming the file and the line, the way a spec that
    does not validate does. An exception ends the loop serving the editor
    panel, and every edit after it goes unanswered."""
    path, key = (spec, "task") if broken == "task" else (scene_of(spec), "objects")
    path.write_text(path.read_text().replace(f"{key}:\n", f"{key}: [\n", 1))
    before = spec.read_text(), scene_of(spec).read_text()

    spec_edit.check_entry(spec)  # not known to be a scene: the edit says what is wrong
    for edit in (
        lambda: spec_edit.apply(spec, "set", ["env_name"], "x"),
        lambda: spec_edit.add_object(spec, "apple", "apple_01_objaverse_robolab", on=TABLE),
        lambda: spec_edit.remove_object(spec, "banana"),
    ):
        result = edit()
        assert not result.ok
        assert "ParserError" in result.errors[0] and path.name in result.errors[0], result.errors
    assert (spec.read_text(), scene_of(spec).read_text()) == before, "a refused edit wrote anyway"


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


def test_renaming_the_background_carries_the_relation_and_task_param_naming_it(spec: Path):
    """The background's id is not in an `_ID_SECTIONS` list — it sits on the
    singleton `background` mapping — but it is still a name the `is_anchor`
    relation and a task param hold, so a rename still has to reach them."""
    result = spec_edit.apply(spec, "set", ["background", "id"], "new_table")  # maple_table_robolab
    assert result.ok, result.errors

    scene = yaml.safe_load(scene_of(spec).read_text())
    assert scene["background"]["id"] == "new_table"
    assert [r["subject"] for r in scene["relations"] if r["kind"] == "is_anchor"] == ["new_table"]

    params = yaml.safe_load(spec.read_text())["task"]["subtasks"][0]["params"]
    assert params["background_scene"] == "new_table"


REFERENCES = """\
object_references:
# anchors the tabletop prim, not the whole table
- id: table_top
  parent_id: maple_table_robolab
  prim_path: '{ENV_REGEX_NS}/maple_table_robolab/table'
  object_type: rigid
  params: {}
- id: distractor_top
  parent_id: distractor
  prim_path: '{ENV_REGEX_NS}/distractor/top'
  object_type: rigid
  params: {}
"""
TOP_RELATION = """\
- kind: next_to
  subject: bowl
  reference: distractor_top
  params: {}
"""
REFERENCED_SCENE = SCENE.replace("relations:\n", REFERENCES + "relations:\n") + TOP_RELATION
OVERRIDE = "cli_override_specs:\n- arg: background\n  target_node_id: maple_table_robolab\ntask:\n"


def test_renaming_carries_every_slot_arena_checks(spec: Path):
    """Arena validates a reference's `parent_id` and a CLI override's target
    against the ids, and refuses the spec when either names one that has gone
    — so a rename has to reach both, in whichever file holds them. The prim
    path of a reference inside the background spells the registry name, which
    a rename of the id leaves alone."""
    spec.write_text(TASK.replace("task:\n", OVERRIDE, 1))
    scene_of(spec).write_text(REFERENCED_SCENE)

    result = spec_edit.apply(spec, "set", ["background", "id"], "bench")
    assert result.ok, result.errors

    scene = yaml.safe_load(scene_of(spec).read_text())
    assert scene["object_references"][0]["parent_id"] == "bench"
    assert scene["object_references"][0]["prim_path"] == "{ENV_REGEX_NS}/maple_table_robolab/table"
    assert yaml.safe_load(spec.read_text())["cli_override_specs"][0]["target_node_id"] == "bench"
    left = (spec.read_text() + scene_of(spec).read_text()).replace(f"registry_name: {TABLE}", "").replace("prim_path: '{ENV_REGEX_NS}/maple_table_robolab/table'", "")
    assert TABLE not in left, "the old id survived somewhere"


def test_renaming_an_object_rewrites_the_prim_path_of_a_reference_inside_it(spec: Path):
    """Arena spawns an asset under its id, so a reference inside an object
    names the object's id in its prim path too; left as it was, the spec
    validates and fails to build."""
    scene_of(spec).write_text(REFERENCED_SCENE)
    result = spec_edit.apply(spec, "set", ["objects", 2, "id"], "decoy")  # distractor
    assert result.ok, result.errors

    reference = yaml.safe_load(scene_of(spec).read_text())["object_references"][1]
    assert (reference["parent_id"], reference["prim_path"]) == ("decoy", "{ENV_REGEX_NS}/decoy/top")


SORT_TASK = TASK.replace(
    """  - kind: PickAndPlaceTask
    params:
      pick_up_object: banana
      destination_location: bowl
""",
    """  - kind: SortMultiObjectTask
    params:
      pick_up_object_list: [banana, distractor]
      destination_location_list: [bowl, bowl]
""",
)


def test_renaming_reaches_inside_a_list_task_param(spec: Path):
    """Arena checks only string params when it validates, but looks every
    listed id up when it builds — so an id in a list is a reference like any
    other, and the rename has to find it."""
    spec.write_text(SORT_TASK)
    result = spec_edit.apply(spec, "set", ["objects", 0, "id"], "plantain")  # banana
    assert result.ok, result.errors

    params = yaml.safe_load(spec.read_text())["task"]["subtasks"][0]["params"]
    assert params["pick_up_object_list"] == ["plantain", "distractor"]
    assert params["destination_location_list"] == ["bowl", "bowl"]


def test_removing_an_object_takes_the_references_inside_it(spec: Path):
    """A reference is a prim of its parent's USD and Arena allows it no other
    parent, so with the object gone there is one thing to do with it — and
    with the relations that named it."""
    scene_of(spec).write_text(REFERENCED_SCENE)
    result = spec_edit.remove_object(spec, "distractor")
    assert result.ok, result.errors
    assert any("distractor_top" in line for line in result.changed), result.changed

    scene = yaml.safe_load(scene_of(spec).read_text())
    assert [reference["id"] for reference in scene["object_references"]] == ["table_top"]
    assert all("distractor" not in (r["subject"], r.get("reference")) for r in scene["relations"])
    assert all("distractor_top" not in (r["subject"], r.get("reference")) for r in scene["relations"])


def test_removing_the_last_entry_of_a_commented_list_still_parses(spec: Path):
    """ruamel writes a list emptied of a commented entry as a `[]` at column 0,
    which does not parse — the list version of the emptied-mapping trap. Both
    ways of emptying one: the card's remove, and a cascade."""
    only_top = REFERENCES.split("- id: distractor_top")[0]
    scene_of(spec).write_text(SCENE.replace("relations:\n", only_top + "relations:\n"))
    result = spec_edit.apply(spec, "remove", ["object_references", 0])
    assert result.ok, result.errors
    assert yaml.safe_load(scene_of(spec).read_text())["object_references"] == []

    scene_of(spec).write_text(SCENE.replace("relations:\n", only_top.replace(TABLE, "distractor", 1).replace("maple_table_robolab/table", "distractor/table") + "relations:\n"))
    result = spec_edit.remove_object(spec, "distractor")
    assert result.ok, result.errors
    assert yaml.safe_load(scene_of(spec).read_text())["object_references"] == []


def test_a_cascade_tolerates_sections_a_hand_edit_left_null(spec: Path):
    """`relations: null` and `object_references: null` are the shape a hand
    edit leaves; the cascade walks them without raising, and the edit is
    answered as a result either way."""
    scene_of(spec).write_text(SCENE.split("relations:")[0] + "object_references: null\nrelations: null\n")
    result = spec_edit.remove_object(spec, "distractor")
    assert isinstance(result, spec_edit.Result)
    assert spec_edit.apply(spec, "set", ["objects", 0, "id"], "plantain").ok is not None


@pytest.mark.parametrize(
    "scene",
    [
        SCENE.replace("embodiment:\n  id: droid\n  registry_name: droid_abs_joint_pos\n  params: {}\n", "embodiment: null\n"),
        SCENE.split("relations:")[0] + "relations: 'on'\n",
    ],
)
def test_an_add_or_remove_on_a_misshapen_spec_is_answered(spec: Path, scene: str):
    """The MCP edit tools see what a hand edit left. A crash reaches the agent
    as a bare `Error executing tool`; a result says what is wrong."""
    assert scene != SCENE
    scene_of(spec).write_text(scene)
    assert isinstance(spec_edit.add_object(spec, "apple", "apple_01_objaverse_robolab", on="banana"), spec_edit.Result)
    assert isinstance(spec_edit.remove_object(spec, "plantain"), spec_edit.Result)


def test_a_blank_reference_names_nothing_as_arena_reads_it(spec: Path):
    """Arena reads `reference: ''` as no reference, so it is not a dangling id
    to report, or to refuse an edit for."""
    broken(spec, "  reference: null", "  reference: ''")
    broken(spec, "pick_up_object: banana", "pick_up_object: plantain", file=spec)
    result = spec_edit.add_object(spec, "apple", "apple_01_objaverse_robolab", on=TABLE)
    assert result.ok and [line for line in result.remaining_errors if "''" in line] == [], result


def test_a_rename_lands_when_arenas_list_helper_fails(spec: Path, monkeypatch):
    """A rename reaches Arena's helper outside the checks' fail-open wrapper,
    so it has to fail open itself."""
    import isaaclab_arena.environment_spec.arena_env_graph_task_conversion_utils as conversion

    def moved(cls):
        raise RuntimeError("moved")

    monkeypatch.setattr(conversion, "find_node_ref_params_in_signature", moved)
    assert spec_edit.apply(spec, "set", ["objects", 0, "id"], "plantain").ok


def test_a_param_called_id_does_not_trigger_a_rename(spec: Path):
    """Only the embodiment's and background's own `id` renames — a param that
    happens to be called `id` a step further down must not. The background's,
    since its constructor takes any param and the embodiment's does not."""
    result = spec_edit.apply(spec, "set", ["background", "params", "id"], TABLE)
    assert result.ok, result.errors
    assert not any("renamed" in line for line in result.changed), result.changed


SECOND_SUBTASK = {
    "kind": "PickAndPlaceTask",
    "params": {"pick_up_object": "distractor", "destination_location": "bowl", "background_scene": TABLE},
}


def test_adding_a_second_subtask_switches_an_atomic_task_to_sequential(spec: Path):
    """`CompositeTaskSpec` requires 'atomic' to hold exactly one subtask, so a
    second one added while it is still atomic would fail validation on its
    own — the edit switches the composition in the same step instead."""
    result = spec_edit.apply(spec, "insert", ["task", "subtasks"], SECOND_SUBTASK)
    assert result.ok, result.errors
    assert any("sequential" in line for line in result.changed), result.changed

    task = yaml.safe_load(spec.read_text())["task"]
    assert task["composition"] == "sequential"
    assert len(task["subtasks"]) == 2


def test_removing_a_subtask_back_to_one_switches_to_atomic(spec: Path):
    """The reverse: dropping back down to a single subtask while still
    'sequential' would fail validation too."""
    assert spec_edit.apply(spec, "insert", ["task", "subtasks"], SECOND_SUBTASK).ok

    result = spec_edit.apply(spec, "remove", ["task", "subtasks", 1])
    assert result.ok, result.errors
    assert any("atomic" in line for line in result.changed), result.changed

    task = yaml.safe_load(spec.read_text())["task"]
    assert task["composition"] == "atomic"
    assert len(task["subtasks"]) == 1


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


def with_embodiment_params(spec: Path, params: str) -> None:
    """Give the scene's DROID the `params` block `params`, indented under it."""
    block = "".join(f"    {line}\n" for line in params.splitlines())
    authored = SCENE.replace("droid_abs_joint_pos\n  params: {}\n", f"droid_abs_joint_pos\n  params:\n{block}", 1)
    scene_of(spec).write_text(authored)


def switch_embodiment(spec: Path, registry_name: str) -> spec_edit.Result:
    return spec_edit.apply(spec, "set", ["embodiment", "registry_name"], registry_name)


def test_switching_the_embodiment_drops_the_params_the_new_one_does_not_take(spec: Path):
    """A DROID's stand is its own constructor's; handed to a Franka it fails the
    build. The id stays, since everything else names the robot by it."""
    with_embodiment_params(spec, "enable_cameras: true\nstand_height_m: 1.2")

    result = switch_embodiment(spec, "franka_ik")
    assert result.ok, result.errors
    assert any("stand_height_m" in line for line in result.changed), result.changed

    embodiment = yaml.safe_load(scene_of(spec).read_text())["embodiment"]
    assert embodiment == {"id": "droid", "registry_name": "franka_ik", "params": {"enable_cameras": True}}


def test_switching_between_droid_variants_keeps_the_stand(spec: Path):
    """The variants differ in how the robot is commanded, not in what it stands
    on, so a param both take survives."""
    with_embodiment_params(spec, "stand_height_m: 1.2")

    assert switch_embodiment(spec, "droid_differential_ik").ok
    assert yaml.safe_load(scene_of(spec).read_text())["embodiment"]["params"] == {"stand_height_m": 1.2}


def test_an_embodiment_emptied_of_commented_params_still_parses(spec: Path):
    """ruamel writes a mapping emptied of a commented key at column 0, which
    does not parse — the same trap unsetting a relation's last param has."""
    with_embodiment_params(spec, "# hand-tuned\nstand_height_m: 1.2")

    assert switch_embodiment(spec, "franka_ik").ok
    assert yaml.safe_load(scene_of(spec).read_text())["embodiment"]["params"] == {}


@pytest.mark.parametrize("registry_name", ["banana_ycb_robolab", TABLE, "not_a_real_robot"])
def test_only_a_robot_is_accepted_as_the_embodiment(spec: Path, registry_name: str):
    """Arena checks that a name is registered, not that it is a robot: a banana
    validates as the embodiment and fails when the scene is built."""
    before = spec.read_text(), scene_of(spec).read_text()

    result = switch_embodiment(spec, registry_name)
    assert not result.ok
    assert any(registry_name in error for error in result.errors), result.errors
    assert (spec.read_text(), scene_of(spec).read_text()) == before


def broken(spec: Path, old: str, new: str, file: Path | None = None) -> list[str]:
    """What `check` says of `spec` once `old` in `file`, the scene by default, reads `new`."""
    file = file or scene_of(spec)
    file.write_text(file.read_text().replace(old, new, 1))
    return spec_edit.check(spec).errors


def test_a_name_nothing_registers_comes_with_the_closest_that_are(spec: Path):
    """And with the file to fix: the scene, spelled as the include spells it."""
    assert broken(spec, "registry_name: banana_ycb_robolab", "registry_name: bananna_ycb_robolab") == [
        "../scenes/scene.yaml: objects.0.registry_name: Unknown asset registry_name 'bananna_ycb_robolab';"
        " did you mean 'banana_ycb_robolab'?"
    ]


def test_a_name_nothing_registered_is_close_to_points_at_the_import(spec: Path):
    (line,) = broken(spec, "registry_name: banana_ycb_robolab", "registry_name: my_private_widget")
    assert line.endswith(
        "nothing registered is close. A module of the project that registers it has to be imported"
        " first: `--import MODULE`, or ARENA_COMPOSER_IMPORTS in bin/arena-mcp"
    ), line


def test_a_kind_and_an_id_come_with_the_closest_too(spec: Path):
    assert broken(spec, "- kind: 'on'\n  subject: distractor", "- kind: nxt_to\n  subject: distractor") == [
        "../scenes/scene.yaml: relations.2: Unknown relation kind 'nxt_to'; did you mean 'next_to'?"
    ]
    scene_of(spec).write_text(SCENE)
    # Arena's line is a sentence, so the hint follows as one; the task file owns `task`.
    assert broken(spec, "pick_up_object: banana", "pick_up_object: banan", file=spec) == [
        "task.yaml: Task 'PickAndPlaceTask' param 'pick_up_object' references unknown node 'banan'."
        " Add it to 'objects' or 'object_references'. Did you mean 'banana'?"
    ]


def test_a_bare_on_is_said_to_need_quoting(spec: Path):
    assert broken(spec, "- kind: 'on'\n  subject: distractor", "- kind: on\n  subject: distractor") == [
        "../scenes/scene.yaml: relations.2.kind: Input should be a valid string;"
        " Arena reads YAML 1.1, where a bare on, off, yes or no is a boolean, so quote it"
    ]


def test_a_small_float_is_written_as_a_float_yaml_1_1_reads(spec: Path):
    """ruamel's `1e-05` is a string to PyYAML, whose floats need a dot."""
    result = spec_edit.apply(spec, "set", ["relations", 1, "params", "clearance_m"], 1e-05)
    assert result.ok, result.errors
    assert "clearance_m: 1.0e-05\n" in scene_of(spec).read_text()
    assert yaml.safe_load(scene_of(spec).read_text())["relations"][1]["params"]["clearance_m"] == 1e-05


def test_a_file_is_replaced_whole_keeping_its_mode(spec: Path):
    """Renamed over, never written into, so a reader finds the old file or the
    new one; and it stays as readable as it was."""
    scene = scene_of(spec)
    scene.chmod(0o640)
    before = scene.stat().st_ino
    assert spec_edit.add_object(spec, "apple", "apple_01_objaverse_robolab", on=TABLE).ok
    assert scene.stat().st_ino != before
    assert scene.stat().st_mode & 0o777 == 0o640
    assert [path.name for path in scene.parent.iterdir()] == ["scene.yaml"], "a temporary file was left behind"


def test_a_write_that_fails_is_a_refusal_and_leaves_the_file(spec: Path, monkeypatch):
    def full(*_):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(spec_edit.os, "replace", full)
    result = spec_edit.add_object(spec, "apple", "apple_01_objaverse_robolab", on=TABLE)
    assert not result.ok and result.errors == ["OSError: [Errno 28] No space left on device"], result
    assert scene_of(spec).read_text() == SCENE
    assert [path.name for path in scene_of(spec).parent.iterdir()] == ["scene.yaml"]


def test_a_subtask_param_its_kind_does_not_take_is_named_with_the_one_meant(spec: Path):
    """Arena validates a subtask's kind, not that its params bind: this spec
    validates, and fails the build with a TypeError."""
    errors = broken(spec, "pick_up_object: banana", "pick_up_objekt: banana", file=spec)
    assert errors == [
        "task.yaml: task.subtasks.0.params: PickAndPlaceTask needs pick_up_object",
        "task.yaml: task.subtasks.0.params.pick_up_objekt: PickAndPlaceTask takes no 'pick_up_objekt';"
        " did you mean 'pick_up_object'?",
    ]


def test_a_relation_param_is_checked_against_its_kind(spec: Path):
    errors = broken(spec, "  subject: banana\n  reference: maple_table_robolab\n  params: {}",
                    "  subject: banana\n  reference: maple_table_robolab\n  params: {clearence_m: 0.01}")
    assert errors == [
        "../scenes/scene.yaml: relations.1.params.clearence_m: on takes no 'clearence_m'; did you mean 'clearance_m'?"
    ]


def test_a_param_arena_passes_itself_is_refused(spec: Path):
    """Arena passes a relation its reference and an atomic task its
    description whatever the params hold, so one there too is a TypeError at
    build. An object's `instance_name` it only defaults, so that one is fine."""
    broken(spec, "registry_name: bowl_ycb_robolab\n  params: {}",
           "registry_name: bowl_ycb_robolab\n  params: {instance_name: my_bowl}")
    broken(spec, "  subject: banana\n  reference: maple_table_robolab\n  params: {}",
           "  subject: banana\n  reference: maple_table_robolab\n  params: {parent: bowl}")
    errors = broken(spec, "      background_scene:", "      task_description: grab it\n      background_scene:", file=spec)
    assert errors == [
        # Arena takes any string task param for an id; the line after says why it is not one.
        "task.yaml: Task 'PickAndPlaceTask' param 'task_description' references unknown node 'grab it'."
        " Add it to 'objects' or 'object_references'.",
        "../scenes/scene.yaml: relations.1.params.parent: Arena passes on its 'parent' itself; remove it",
        "task.yaml: task.subtasks.0.params.task_description: Arena passes PickAndPlaceTask its 'task_description'"
        " itself; remove it",
    ]


def test_the_ids_in_a_list_param_are_checked_where_arena_skips_them(spec: Path):
    sort = TASK.replace(
        "  - kind: PickAndPlaceTask\n    params:\n      pick_up_object: banana\n      destination_location: bowl\n",
        "  - kind: SortMultiObjectTask\n    params:\n      pick_up_object_list: [banana, ghost]\n"
        "      destination_location_list: [bowl]\n",
    )
    spec.write_text(sort)
    assert spec_edit.check(spec).errors == [
        # Arena's builder passes an atomic task its description, which SortMultiObjectTask does not take.
        "task.yaml: task.subtasks.0.kind: SortMultiObjectTask takes no task_description, which Arena passes an"
        " atomic task, so it does not build alone; it builds as one of several sequential or parallel subtasks",
        "task.yaml: task.subtasks.0.params.pick_up_object_list.1: 'ghost' is not an id this spec declares",
        "task.yaml: task.subtasks.0.params: SortMultiObjectTask pairs each pick_up_object_list entry with a"
        " destination_location_list one, and has 2 and 1",
    ]


def test_an_object_reference_is_checked_as_arena_builds_it(spec: Path):
    scene_of(spec).write_text(
        SCENE + "object_references:\n- id: drawer\n  parent_id: maple_table_robolab\n  object_type: base\n"
        "  params: {openable_joint_name: drawer_joint}\n"
    )
    assert spec_edit.check(spec).errors == [
        "../scenes/scene.yaml: object_references.0.prim_path:"
        " a reference is built from the prim it names, and names none",
        "../scenes/scene.yaml: object_references.0.object_type: openable_joint_name makes it an articulation;"
        " set it to articulation",
    ]


def test_a_reference_is_checked_against_the_class_its_affordance_makes_it(spec: Path):
    """A turnable reference short of its levels, or any reference given what
    Arena passes itself, is a `TypeError` once the sim has started."""
    scene_of(spec).write_text(
        SCENE + "object_references:\n"
        f"- id: knob\n  parent_id: {TABLE}\n  prim_path: /knob\n  object_type: articulation\n"
        "  params: {turnable_joint_name: knob_joint}\n"
        f"- id: top\n  parent_id: {TABLE}\n  prim_path: /top\n  object_type: rigid\n"
        "  params: {object_type: rigid}\n"
    )
    assert spec_edit.check(spec).errors == [
        "../scenes/scene.yaml: object_references.0.params: a reference with turnable_joint_name needs"
        " min_level_angle_deg, max_level_angle_deg, num_levels",
        "../scenes/scene.yaml: object_references.1.params.object_type:"
        " Arena passes a reference its 'object_type' itself; remove it",
    ]


def test_a_key_neither_file_has_goes_with_its_kin(spec: Path):
    """A missing `task` is the task file's to fix, not the scene's because the
    scene holds the lists; and a first `cli_override_specs` lands beside the
    task, where the template keeps it."""
    spec.write_text(TASK.split("task:")[0])
    assert spec_edit.check(spec).errors[0].startswith("task.yaml: task:")

    spec.write_text(TASK)
    result = spec_edit.apply(spec, "insert", ["cli_override_specs"], {"arg": "background", "target_node_id": TABLE})
    assert result.ok and result.wrote == [str(spec)], result


def test_an_embodiment_param_it_does_not_take_is_named(spec: Path):
    errors = broken(spec, "registry_name: droid_abs_joint_pos\n  params: {}",
                    "registry_name: droid_abs_joint_pos\n  params: {stand_heigth_m: 0.8}")
    assert errors == [
        "../scenes/scene.yaml: embodiment.params.stand_heigth_m: droid_abs_joint_pos takes no 'stand_heigth_m';"
        " did you mean 'stand_height_m'?"
    ]


def test_a_stale_param_left_by_hand_does_not_block_other_edits(spec: Path):
    """What makes these checks safe to add: a spec that already fails one is
    still edited, the failure carried as `remaining_errors`."""
    broken(spec, "  subject: banana\n  reference: maple_table_robolab\n  params: {}",
           "  subject: banana\n  reference: maple_table_robolab\n  params: {clearence_m: 0.01}")
    result = spec_edit.add_object(spec, "apple", "apple_01_objaverse_robolab", on=TABLE)
    assert result.ok, result.errors
    assert len(result.remaining_errors) == 1 and "clearence_m" in result.remaining_errors[0], result.remaining_errors


def overridden(spec: Path, override: str) -> list[str]:
    spec.write_text(TASK + f"env_cfg_override: {override}\n")
    return spec_edit.check(spec).errors


def test_an_override_arena_would_refuse_after_the_sim_starts_is_refused_now(spec: Path):
    """Arena applies `env_cfg_override` to the env config the sim builds, so a
    misspelled field otherwise surfaces minutes into a run."""
    assert overridden(spec, "{sim: {dt: 0.01}}") == []
    assert overridden(spec, "{sim: {dtt: 0.01}}") == ["task.yaml: env_cfg_override: Unknown config field 'env.sim.dtt'"]
    assert overridden(spec, "{decimation: '${steps}'}") == [
        "task.yaml: env_cfg_override: OmegaConf interpolation is not allowed at 'env.decimation'"
    ]


def test_an_override_naming_a_module_not_yet_loaded_is_not_imported_to_check_it(spec: Path):
    """Resolving a `_target_` imports its module, and one may load Kit, whose
    EULA prompt waits on a terminal. Left unchecked rather than risked."""
    module = "isaaclab.envs.mdp.never_loaded_here"
    assert overridden(spec, f"{{sim: {{physics: {{_target_: {module}.SomeCfg}}}}}}") == []
    assert module not in sys.modules

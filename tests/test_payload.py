"""What the viewer is told about assets, and when.

The registered assets are a fixed ~30 kB that rides every message. The SimReady
props are a megabyte that changes only when the index is rebuilt or a picture is
fetched, so they ride the message that changed them and no other.

The spec rides every message too, so one about a file that no longer reads
carries the spec it last read to. So does what is wrong with it, at the entry
each problem names.
"""

from __future__ import annotations

import math
from pathlib import Path

import msgpack
import numpy as np
import pytest
from isaaclab.utils.assets import _mirror_path
from isaaclab_arena.utils.usd.helpers import compute_local_bounding_box_from_usd_at_joint_pos

from arena_composer import catalog, payload, simready, usd_assets


def test_a_prop_carries_its_picture_once_that_is_on_disk(tmp_path: Path, monkeypatch):
    """The viewer draws from a file and opens no socket, so it is given the
    mirrored path — and nothing at all until there is one to give."""
    # The index lives in the asset cache, so this moves both.
    monkeypatch.setattr(usd_assets, "_cache_dir", tmp_path)
    folder = f"{simready.PREFIX}Residential/Kitchen/Baskets/Plastic_Basket_A01"
    layer, sidecar = f"{folder}/sm_basket_a01.usd", f"{folder}/.thumbs/256x256/sm_basket_a01.usd.png"
    monkeypatch.setattr(simready, "list_keys", lambda: [layer, sidecar])
    monkeypatch.setattr(simready, "central", lambda bucket, known: simready.Central("", [], []))
    simready.build()

    (prop,) = payload._remote()
    assert prop["name"] == "Plastic_Basket_A01"
    assert prop["registry_name"] == "simready_usd_object"
    assert prop["params"] == {"usd_path": f"{simready.BUCKET}/{layer}"}
    assert prop["category"] == "Residential/Kitchen/Baskets", "where the browser's tree files it"
    assert prop["thumbnail"] == "", "a picture nobody has fetched is not a path"

    # Where `view._thumbnails` would have put it. Taken from Isaac Lab's own
    # function, so a mirror layout that moves takes this with it.
    mirrored = Path(_mirror_path(f"{simready.BUCKET}/{sidecar}", str(tmp_path)))
    mirrored.parent.mkdir(parents=True, exist_ok=True)
    mirrored.write_bytes(b"not really a png")

    (prop,) = payload._remote()
    assert prop["thumbnail"] == str(mirrored)
    assert prop["credit"] == "", "a bucket prop asks for none"


def test_a_central_prop_carries_its_credit_and_its_small_picture(tmp_path: Path, monkeypatch):
    """CC-BY-4.0 asks for credit wherever the prop is offered, so the browser
    is given it. The picture is the downscaled copy, not one in the Hugging
    Face cache, which the viewer never reads from."""
    monkeypatch.setattr(usd_assets, "_cache_dir", tmp_path)
    central = simready.RemoteAsset(
        name="paint_supplies_spraygun_b01",
        category="Central/paint",
        key="paint_supplies_spraygun_b01/simready_usd/sm_paint_supplies_spraygun_b01_01.usd",
        revision="faaa6cea782d370b0b73408674507ad9f7835484",
    )
    monkeypatch.setattr(simready, "list_keys", lambda: [])
    monkeypatch.setattr(simready, "central", lambda bucket, known: simready.Central(central.revision, [central], []))
    simready.build()

    (prop,) = payload._remote()
    assert prop["params"] == {"usd_path": central.usd_path}
    assert prop["category"] == "Central/paint"
    assert prop["credit"] == simready.CENTRAL_CREDIT
    assert "central" in prop["tags"], "searchable by where it comes from"
    assert prop["thumbnail"] == ""

    picture = tmp_path / "simready-central" / "sm_paint_supplies_spraygun_b01_01.usd.png"
    picture.parent.mkdir()
    picture.write_bytes(b"not really a png")
    (prop,) = payload._remote()
    assert prop["thumbnail"] == str(picture)


def test_the_props_ride_only_the_message_that_changed_them(tmp_path: Path, monkeypatch):
    """An absent list and an empty one are different answers: absent leaves the
    panel's browser as it is, where empty would blank it."""
    monkeypatch.setattr(usd_assets, "_cache_dir", tmp_path)
    dump = {"assets": [], "spec": {}}

    assert payload.build_payload(dump, remote=False)["remote"] is None
    assert payload.build_payload(dump)["remote"] == [], "no index yet, but the list was asked for"
    # The registered half carries no parameters, which is what tells the panel
    # an entry naming one needs nothing beyond the registry name.
    assert all(asset["params"] == {} for asset in payload._vocabulary()["assets"])


def test_the_vocabulary_carries_the_task_compositions(tmp_path: Path, monkeypatch):
    """The panel offers these as a dropdown without hard-coding the names
    Arena's `CompositeTaskSpec` accepts."""
    monkeypatch.setattr(usd_assets, "_cache_dir", tmp_path)
    assert payload._vocabulary()["compositions"] == ["atomic", "parallel", "sequential"]


def test_a_registered_asset_is_offered_a_picture_too(tmp_path: Path, monkeypatch):
    """A picture is published beside a layer, whoever published it, so nothing
    about asking for one is the props'. What stays theirs is `params`: a pick on
    a registered asset writes an entry carrying the registry name and nothing
    else, so the URL travels beside it and not in it."""
    monkeypatch.setattr(usd_assets, "_cache_dir", tmp_path)
    url = "https://bucket/Props/YCB/Axis_Aligned_Physics/003_cracker_box.usd"
    asset = catalog.Asset(
        registry_name="cracker_box",
        role="object",
        tags=["food"],
        object_type="rigid",
        name="cracker_box",
        usd_path=url,
    )

    encoded = payload._asset(asset)
    assert encoded["usd_path"] == url, "what the panel asks for a picture by"
    assert encoded["params"] == {}, "and what a pick writes, which is nothing"
    assert encoded["thumbnail"] == ""

    sidecar = Path(_mirror_path(simready.thumbnail_url(url), str(tmp_path)))
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_bytes(b"not really a png")
    assert payload._asset(asset)["thumbnail"] == str(sidecar)


def test_an_asset_with_no_published_usd_is_never_asked_about(tmp_path: Path, monkeypatch):
    """Some registered assets compose their USD instead of naming one. There is
    no layer to look beside, so they carry neither field and the panel leaves
    the space blank rather than asking forever."""
    monkeypatch.setattr(usd_assets, "_cache_dir", tmp_path)
    composed = catalog.Asset(
        registry_name="deformable_cube", role="object", tags=[], object_type="deformable",
        name="deformable_cube",
    )
    assert payload._asset(composed)["usd_path"] == ""
    assert payload._asset(composed)["thumbnail"] == ""


def test_an_asset_registered_from_a_local_directory_draws_its_own_sidecar(tmp_path: Path, monkeypatch):
    """Not every registered asset names a URL — some come from a directory
    another SDK has already downloaded. There is nothing to mirror for one of
    those, but the picture beside the layer is as much on disk as a mirrored
    one, and the panel has to be told where it is."""
    monkeypatch.setattr(usd_assets, "_cache_dir", tmp_path / "mirror")
    layer = tmp_path / "sdk" / "Broccoli008" / "Broccoli008.usd"
    sidecar = layer.parent / ".thumbs" / "256x256" / "Broccoli008.usd.png"
    sidecar.parent.mkdir(parents=True)

    asset = catalog.Asset(
        registry_name="broccoli", role="object", tags=[], object_type="rigid",
        name="broccoli", usd_path=str(layer),
    )
    assert payload._asset(asset)["thumbnail"] == "", "no picture beside the layer, so nothing to draw"

    sidecar.write_bytes(b"not really a png")
    assert payload._asset(asset)["thumbnail"] == str(sidecar)


# Real registered names, because Arena validates them against live registries.
SPEC = """\
env_name: from_the_file
embodiment: {id: droid, registry_name: droid_abs_joint_pos, params: {}}
background: {id: maple_table_robolab, registry_name: maple_table_robolab, params: {}}
objects:
- {id: banana, registry_name: banana_ycb_robolab, params: {}}
- {id: bowl, registry_name: bowl_ycb_robolab, params: {}}
relations:
- {kind: is_anchor, subject: maple_table_robolab, reference: null, params: {}}
task:
  composition: atomic
  description: pick up the banana and place it in the bowl
  subtasks:
  - kind: PickAndPlaceTask
    params: {pick_up_object: banana, destination_location: bowl, background_scene: maple_table_robolab}
"""


def test_a_spec_that_no_longer_reads_still_carries_the_reason(tmp_path: Path, monkeypatch):
    """A hand edit that is not YAML, or that Arena will not validate, has no
    editor half of its own. The reason goes out on the one the panel holds —
    the first layout's, then whichever the file last read to — rather than
    being dropped with the message. A message with nothing to say still is."""
    monkeypatch.setattr(usd_assets, "_cache_dir", tmp_path)
    spec = tmp_path / "task.yaml"
    spec.write_text(SPEC)

    def carried(errors: list[str]) -> str:
        message = msgpack.unpackb(payload.encode_spec(spec, errors, solving=False))
        assert message["errors"] == errors
        return message["spec"]["env_name"]

    payload.build_payload({"assets": [], "spec": {"env_name": "opened"}, "spec_path": str(spec)}, remote=False)
    spec.write_text("env_name: [\n")
    assert carried(["ParserError"]) == "opened", "the panel opens on the first layout's spec"
    with pytest.raises(Exception):
        payload.encode_spec(spec, [])

    spec.write_text(SPEC)
    assert carried([]) == "from_the_file"
    # A later layout's spec was read when its solve began, and may be behind.
    payload.build_payload({"assets": [], "spec": {"env_name": "solved"}, "spec_path": str(spec)}, remote=False)
    spec.write_text(SPEC.replace("banana_ycb_robolab", "no_such_asset"))
    assert carried(["ValidationError"]) == "from_the_file"


def test_a_problem_goes_to_the_card_of_the_entry_it_names():
    """By path, after the file a line names first where the spec is two, or by
    id, as the solve's warnings name theirs. A line naming no entry the panel
    draws a card for has nowhere to go. A file named after a key, `task.yaml`,
    is still a file."""
    spec = {
        "embodiment": {"id": "droid"},
        "background": {"id": "table"},
        "objects": [{"id": "banana"}, {"id": "bowl"}],
        "object_sets": None,
    }
    errors = [
        "../scenes/scene.yaml: relations.12.params.foo: on takes no 'foo'",
        "task.yaml: task.subtasks.1.params.bogus: PickAndPlaceTask takes no 'bogus'",
        "task.subtasks: atomic takes exactly one subtask",
        "task.yaml: placer_params.enabled_checks.0: no check is called 'nope'",
        "TypeError: object.__init__() takes exactly one argument",
    ]
    warnings = [
        "bowl: no relation places it and it has no initial_pose, so it spawns at the origin",
        "droid: its origin, (0.00, 0.00, 0.00), is inside table's box, so the robot spawns inside table",
    ]

    found = [(problem["path"], problem["warning"]) for problem in payload._attributed(spec, errors, warnings)]
    assert found == [
        (["relations", 12], False),
        (["task", "subtasks", 1], False),
        (["task"], False),
        (["objects", 1], True),
        (["embodiment"], True),
    ]


def test_a_message_carries_what_is_wrong_with_its_spec(tmp_path: Path, monkeypatch):
    """What `check_spec` would say rides every message, and what the last solve
    warned of rides while the file still reads to the spec it solved: a warning
    names its entry by position, which an edit since may have handed on."""
    from isaaclab_arena.environment_spec.arena_env_graph_spec import ArenaEnvGraphSpec

    monkeypatch.setattr(usd_assets, "_cache_dir", tmp_path)
    spec = tmp_path / "task.yaml"
    # A param `on` does not take: the validator passes it, the build would not.
    planted = "relations:\n- {kind: 'on', subject: bowl, reference: maple_table_robolab, params: {foo: 1}}\n"
    spec.write_text(SPEC.replace("relations:\n", planted))

    def problems() -> list[tuple[list, bool]]:
        message = msgpack.unpackb(payload.encode_spec(spec, []))
        return [(problem["path"], problem["warning"]) for problem in message["problems"]]

    assert problems() == [(["relations", 0], False)]

    solved = ArenaEnvGraphSpec.from_yaml(spec).model_dump(mode="json")
    warned = "bowl: no relation places it and it has no initial_pose, so it spawns at the origin"
    dump = {"assets": [], "spec": solved, "spec_path": str(spec), "warnings": [warned]}
    assert len(payload.build_payload(dump, remote=False)["problems"]) == 2, "the panel opens without them"
    assert problems() == [(["relations", 0], False), (["objects", 1], True)]

    # Fixed by hand, and not solved since.
    spec.write_text(SPEC)
    assert problems() == [], "a warning outlived the spec it was solved from"


# A reduction of the KUKA-Allegro's kuka.usd: links are sibling Xforms under the
# default prim, the base is fixed to the world, and a joint names the bodies it joins.
ARM = """\
#usda 1.0
(
    defaultPrim = "kuka"
    metersPerUnit = 1
    upAxis = "Z"
)

def Xform "kuka" (prepend apiSchemas = ["PhysicsArticulationRootAPI"])
{
    def Xform "iiwa7_link_0" (prepend apiSchemas = ["PhysicsRigidBodyAPI"])
    {
        def Cube "visuals"
        {
            double size = 0.2
            double3 xformOp:translate = (0, 0, 0.1)
            uniform token[] xformOpOrder = ["xformOp:translate"]
        }
    }

    def Xform "iiwa7_link_1" (prepend apiSchemas = ["PhysicsRigidBodyAPI"])
    {
        double3 xformOp:translate = (0, 0, 0.2)
        uniform token[] xformOpOrder = ["xformOp:translate"]

        def Cube "visuals"
        {
            double size = 1
            double3 xformOp:translate = (0, 0, 0.5)
            float3 xformOp:scale = (0.1, 0.1, 1)
            uniform token[] xformOpOrder = ["xformOp:translate", "xformOp:scale"]
        }
    }

    def PhysicsFixedJoint "world_joint"
    {
        rel physics:body1 = </kuka/iiwa7_link_0>
    }

    def PhysicsRevoluteJoint "iiwa7_joint_1"
    {
        rel physics:body0 = </kuka/iiwa7_link_0>
        rel physics:body1 = </kuka/iiwa7_link_1>
        point3f physics:localPos0 = (0, 0, 0.2)
        uniform token physics:axis = "Y"
    }
}
"""


def test_each_layout_rides_the_payload_and_shares_what_it_draws():
    """A solve's samples stand the same pieces in other places, so each costs
    the payload a pose per asset and no geometry, but for an object set's
    member that no other layout drew. The first is `assets`, all a render
    draws, and each carries its own verdict."""

    def placed(node_id: str, x: float, top: float = 1.0) -> dict:
        """An asset with no USD, drawn as the box it was placed by."""
        return {
            "id": node_id, "role": "object", "position_xyz": [x, 0, 0], "rotation_xyzw": [0, 0, 0, 1],
            "bbox_min": [0, 0, 0], "bbox_max": [1, 1, top],
        }

    first = [placed("crate", 0.0), placed("snack", 2.0, top=0.2)]
    dump = {
        "placement": {"seed": 7, "success": True, "failed_checks": []},
        "assets": first,
        "layouts": [
            {"success": True, "failed_checks": [], "assets": first},
            # The set's other member, a taller one, in the second env.
            {"success": False, "failed_checks": ["no_overlap"], "assets": [placed("crate", 1.0), placed("snack", 3.0)]},
        ],
    }

    built = payload.build_payload(dump, remote=False)
    one, two = built["layouts"]
    assert built["assets"] == one["assets"]
    assert two["assets"][0]["position_xyz"] == [1.0, 0.0, 0.0]
    assert one["assets"][0]["geometry"] == two["assets"][0]["geometry"], "the crate was packed twice"
    assert one["assets"][1]["geometry"] != two["assets"][1]["geometry"], "the set drew one member in both"
    assert len(built["geometries"]) == 3
    assert [(layout["success"], layout["failed_checks"]) for layout in built["layouts"]] == [
        (True, []),
        (False, ["no_overlap"]),
    ]
    assert built["seed"] == 7

    # One sample is `assets` alone, its verdict in the summary.
    alone = {"placement": {"seed": 7, "success": False, "failed_checks": ["on_relation"]}, "assets": first}
    built = payload.build_payload(alone, remote=False)
    assert [(layout["success"], layout["failed_checks"]) for layout in built["layouts"]] == [(False, ["on_relation"])]
    assert built["layouts"][0]["assets"] == built["assets"]


def test_a_robot_is_drawn_at_the_start_pose_arena_places_it_by(tmp_path: Path):
    """Arena bounds a robot at its `init_state.joint_pos`, and props keep clear of
    that box. Drawn at the pose its file authors, the arm stands a metre tall where
    it was placed lying down. The key is a regex, as KUKA_ALLEGRO_CFG writes them."""
    usda = tmp_path / "kuka.usda"
    usda.write_text(ARM)
    joint_pos = {"iiwa7_joint_(1|2|7)": math.pi / 2}
    robot = {
        "id": "robot", "role": "embodiment", "local_usd_path": str(usda), "joint_pos": joint_pos,
        "position_xyz": [0, 0, 0], "rotation_xyzw": [0, 0, 0, 1], "bbox_min": [0, 0, 0], "bbox_max": [1, 1, 1],
    }

    built = payload.build_payload({"assets": [robot]}, remote=False)
    nodes = built["geometries"][built["assets"][0]["geometry"]]
    positions = np.vstack([np.frombuffer(node["positions"], dtype="<f4").reshape(-1, 3) for node in nodes])
    box = compute_local_bounding_box_from_usd_at_joint_pos(str(usda), joint_pos)
    np.testing.assert_allclose(positions.min(axis=0), np.asarray(box.min_point).reshape(-1), atol=1e-6)
    np.testing.assert_allclose(positions.max(axis=0), np.asarray(box.max_point).reshape(-1), atol=1e-6)
    assert positions[:, 2].max() < 0.3, "lying down, as Arena bounds it"

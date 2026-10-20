"""The checks a resolve makes before Arena's solver sees a spec: on a `params.usd_path`, and on the
prim an object reference names; what it says of the layout after: the placement summary, the world
boxes, and the warnings for what passes and cannot work. Against files authored here, so no asset is
downloaded and no solve runs."""

from __future__ import annotations

import math
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace

import pytest

from arena_composer.resolve_layout import (
    SpecError,
    _asset_entry,
    _falling_root,
    _placement_summary,
    check_reference_boxes,
    check_usd_params,
    warnings,
)

SIMREADY = "simready_usd_object"


def prop(path: Path, bodies: int) -> str:
    """A USD whose default prim holds `bodies` rigid bodies."""
    from pxr import Usd, UsdGeom, UsdPhysics

    stage = Usd.Stage.CreateNew(str(path))
    root = UsdGeom.Xform.Define(stage, "/Asset")
    stage.SetDefaultPrim(root.GetPrim())
    for index in range(bodies):
        cube = UsdGeom.Cube.Define(stage, f"/Asset/body_{index}")
        UsdPhysics.RigidBodyAPI.Apply(cube.GetPrim())
    stage.Save()
    return str(path)


def entry(node_id: str, usd_path: str, registry_name: str = SIMREADY) -> SimpleNamespace:
    return SimpleNamespace(id=node_id, registry_name=registry_name, params={"usd_path": usd_path})


def test_a_single_rigid_body_passes(tmp_path: Path):
    check_usd_params([entry("can", prop(tmp_path / "one.usda", bodies=1))])


def test_the_wrong_number_of_rigid_bodies_is_named_per_object(tmp_path: Path):
    with pytest.raises(SpecError) as raised:
        check_usd_params(
            [
                entry("shelf", prop(tmp_path / "none.usda", bodies=0)),
                entry("can", prop(tmp_path / "one.usda", bodies=1)),
                entry("pair", prop(tmp_path / "two.usda", bodies=2)),
            ]
        )
    assert [line.split(":")[0] for line in raised.value.errors] == ["shelf", "pair"]
    assert "0 rigid bodies" in raised.value.errors[0] and "2 rigid bodies" in raised.value.errors[1]


def test_a_file_that_is_not_there_is_named(tmp_path: Path):
    with pytest.raises(SpecError, match="can: no file at"):
        check_usd_params([entry("can", str(tmp_path / "missing.usd"))])


def test_an_entry_without_a_usd_path_is_not_checked():
    check_usd_params([SimpleNamespace(id="bowl", registry_name="bowl_ycb_robolab", params={})])


def test_a_reference_the_solve_places_by_needs_geometry_arena_bounds(tmp_path: Path):
    """Isaac's Seattle lab table, `table`, draws its top in one mesh with the robot mount, and its
    only collider, `Collisions/Cube`, is proxy purpose. Arena bounds default-purpose geometry alone,
    so a reference to the collider has an empty box, and the solver stopped on "Non-finite solver
    output" for one a relation names, or on an interval assert for one it avoids as an obstacle."""
    from isaaclab_arena.assets.object import Object
    from isaaclab_arena.assets.object_type import ObjectType
    from isaaclab_arena.environment_spec.arena_env_graph_conversion_utils import _instantiate_object_reference
    from isaaclab_arena.environment_spec.arena_env_graph_types import ObjectReferenceSpec
    from pxr import Usd, UsdGeom, UsdPhysics

    path = tmp_path / "table.usda"
    stage = Usd.Stage.CreateNew(str(path))
    stage.SetDefaultPrim(UsdGeom.Xform.Define(stage, "/DemoTable").GetPrim())
    UsdGeom.Cube.Define(stage, "/DemoTable/Visuals/TableGeom")
    collider = UsdGeom.Cube.Define(stage, "/DemoTable/Collisions/Cube")
    collider.CreatePurposeAttr(UsdGeom.Tokens.proxy)
    UsdPhysics.CollisionAPI.Apply(collider.GetPrim())
    UsdGeom.Xform.Define(stage, "/DemoTable/Empty")
    stage.Save()

    table = Object(name="table", usd_path=str(path), object_type=ObjectType.BASE)
    references = [
        ObjectReferenceSpec(id=node_id, parent_id="table", prim_path=prim_path, object_type="base")
        for node_id, prim_path in (("top", "Collisions/Cube"), ("legs", "Visuals/TableGeom"), ("none", "Empty"))
    ]
    assets = {ref.id: _instantiate_object_reference(ref, parent_asset=table) for ref in references}

    def refused(relations: list[str], obstacles: list[str] = ()) -> list[str]:
        spec = SimpleNamespace(
            object_references=references,
            relations=[SimpleNamespace(subject=node_id, reference=None) for node_id in relations],
        )
        try:
            check_reference_boxes(spec, assets, [assets[node_id] for node_id in obstacles])
        except SpecError as error:
            return error.errors
        return []

    assert refused(["top", "legs", "none"]) == [
        "object_references.0.prim_path: Collisions/Cube holds only proxy-purpose geometry, and Arena bounds only"
        " default-purpose geometry, so top has no box to place by",
        "object_references.2.prim_path: Empty holds no geometry, so none has no box to place by",
    ]
    assert refused([], obstacles=["top"]) == refused(["top"]) != []
    assert refused([]) == [], "a box the solve never sees is no problem"


Asset = namedtuple("Asset", "name")


def sample(banana_x: float, failed: list[str]) -> SimpleNamespace:
    """A solved layout with a banana at `banana_x`, failing `failed`."""
    banana = Asset("banana")
    return SimpleNamespace(
        positions={banana: (banana_x, 0.0, 0.8)},
        orientations={banana: 0.5},
        success=not failed,
        final_loss=0.0,
        attempts=1,
        validation_results=SimpleNamespace(
            validation_results={"no_overlap": "no_overlap" not in failed}, get_failed_validation_check_names=failed
        ),
    )


def test_the_summary_names_the_checks_a_dry_solve_cannot_run():
    """A pass here is necessary, not sufficient: physics settling and IK reach
    are the simulator's to decide, and the summary has to say they were not."""
    from isaaclab_arena.relations.validation.types import PlacementCheck

    ran = {check: True for check in PlacementCheck if check not in (PlacementCheck.PHYSICS_SETTLED, PlacementCheck.IK_REACHABLE)}
    layout = sample(0.1, [])
    layout.validation_results.validation_results = ran
    every_kind = {"on", "clutter_on", "next_to", "not_next_to", "face_to"}
    summary = _placement_summary([layout], seed=7, samples=1, kinds=every_kind)
    assert summary["checks"]["on_relation"] is True
    assert summary["not_run"] == ["physics_settled", "ik_reachable"]


def test_nothing_to_place_is_every_sample_passed_with_nothing_checked():
    """A scene of fixed props — no relation — is done as it is, so the skill's
    `passed` of `samples` has to read as done; and `not_run` has to say that
    no check ran, overlap included."""
    summary = _placement_summary([], seed=7, samples=8, kinds=set())
    assert "success" not in summary and summary["solved_assets"] == 0
    assert (summary["samples"], summary["passed"], summary["distinct_layouts"]) == (8, 8, 1)
    assert "no_overlap" in summary["not_run"] and summary["check_failures"] == {}


def test_the_summary_counts_the_samples_that_passed_and_the_layouts_they_were():
    """The first sample is the one described; the others are the layouts Arena
    would draw on as many resets. Two within a millimetre are one layout."""
    summary = _placement_summary(
        [sample(0.1, []), sample(0.3, ["no_overlap"]), sample(0.1002, [])],
        seed=7,
        samples=3,
        kinds=set(),
    )
    assert summary["success"] is True
    assert {key: summary[key] for key in ("samples", "passed", "failed_samples", "distinct_layouts")} == {
        "samples": 3,
        "passed": 2,
        "failed_samples": [1],
        "distinct_layouts": 2,
    }
    assert summary["check_failures"] == {"no_overlap": 1}


def test_a_check_for_a_relation_kind_the_spec_does_not_use_did_not_run():
    """Arena runs every check it has and passes one with nothing to test, so `next_to` read true on
    a spec with no `next_to`. It is reported as not run, as the 0.3.0 changelog says it is."""
    from isaaclab_arena.relations.validation.types import PlacementCheck

    layout = sample(0.1, [])
    sim_only = (PlacementCheck.PHYSICS_SETTLED, PlacementCheck.IK_REACHABLE)
    layout.validation_results.validation_results = {check: True for check in PlacementCheck if check not in sim_only}
    summary = _placement_summary([layout], seed=7, samples=1, kinds={"is_anchor", "on"})
    assert summary["checks"] == {"no_overlap": True, "on_relation": True}
    assert summary["not_run"] == [
        "clutter_on_relation",
        "next_to",
        "not_next_to",
        "face_to",
        "physics_settled",
        "ik_reachable",
    ]


def test_the_world_box_turns_with_the_asset_as_the_solver_turns_it():
    """A quarter turn about z takes local +x to world +y, so a 1 x 2 x 3 box at (10, 0, 0) spans x
    8..10 and y 0..1 in the world. `position_xyz` stays the origin, and the local box is the one the
    viewer draws."""
    from isaaclab_arena.utils.bounding_box import AxisAlignedBoundingBox
    from isaaclab_arena.utils.pose import Pose

    turn = math.sin(math.pi / 4), math.cos(math.pi / 4)
    pose = Pose(position_xyz=(10.0, 0.0, 0.0), rotation_xyzw=(0.0, 0.0, *turn))
    box = AxisAlignedBoundingBox(min_point=(0.0, 0.0, 0.0), max_point=(1.0, 2.0, 3.0))
    crate = SimpleNamespace(
        name="crate", usd_path=None, scale=(1.0, 1.0, 1.0), get_initial_pose=lambda: pose, get_bounding_box=lambda: box
    )
    entry = _asset_entry("crate", crate, {"role": "object"}, layout=None)
    assert entry["world_bbox_min"] == pytest.approx([8.0, 0.0, 0.0], abs=1e-6)
    assert entry["world_bbox_max"] == pytest.approx([10.0, 1.0, 3.0], abs=1e-6)
    assert entry["yaw_rad"] == pytest.approx(math.pi / 2)
    assert entry["position_xyz"] == [10.0, 0.0, 0.0]
    assert (entry["bbox_min"], entry["bbox_max"]) == ([0.0, 0.0, 0.0], [1.0, 2.0, 3.0])


# table_oak_robolab's fixtures/table_oak.usd, reduced. Its default prim is the table itself, a rigid
# body left at the API's defaults, enabled and not kinematic, and its only ground is a sibling of it,
# which Isaac Lab never spawns. `{root}` is what a case authors on the root.
TABLE_OAK = """#usda 1.0
(
    defaultPrim = "Root"
)

def Xform "Root" (
    prepend apiSchemas = ["PhysicsRigidBodyAPI"]
)
{{
    {root}
    def Cube "top" (
        prepend apiSchemas = ["PhysicsCollisionAPI"]
    )
    {{
    }}
}}

def Xform "GroundPlane"
{{
    def Plane "CollisionPlane" (
        prepend apiSchemas = ["PhysicsCollisionAPI"]
    )
    {{
    }}
}}
"""

# maple_table_robolab's scenes/maple_table.usda, reduced: the same rigid table one level down, in a
# scene whose root has no physics and holds a ground beside the table.
MAPLE_TABLE = """#usda 1.0
(
    defaultPrim = "World"
)

def Xform "World"
{
    def Xform "table" (
        prepend apiSchemas = ["PhysicsRigidBodyAPI"]
    )
    {
        def Cube "top" (
            prepend apiSchemas = ["PhysicsCollisionAPI"]
        )
        {
        }
    }

    def Xform "GroundPlane"
    {
        def Plane "CollisionPlane" (
            prepend apiSchemas = ["PhysicsCollisionAPI"]
        )
        {
        }
    }
}
"""


@pytest.mark.parametrize(
    ("root", "falls"),
    [("", "/Root"), ("bool physics:kinematicEnabled = 1", None), ("bool physics:rigidBodyEnabled = 0", None)],
)
def test_a_background_whose_root_is_a_free_rigid_body_falls(tmp_path: Path, root: str, falls: str | None):
    path = tmp_path / "table_oak.usda"
    path.write_text(TABLE_OAK.format(root=root))
    assert _falling_root(str(path)) == falls


def test_a_scene_holding_a_rigid_table_and_a_ground_does_not_fall(tmp_path: Path):
    path = tmp_path / "maple_table.usda"
    path.write_text(MAPLE_TABLE)
    assert _falling_root(str(path)) is None


def scene(tmp_path: Path, table_box: tuple, background: str = TABLE_OAK.format(root=""), ground: bool = False):
    """table_oak_robolab as the trial met it: a DROID at the origin, the table's centre, and on the
    table a bowl, a banana no relation places, a plate fixed by its initial pose, and a pin on the
    table, the anchor. `table_box` is the table's world box; `ground` adds a ground plane."""
    from isaaclab_arena.assets.object_library import GroundPlane
    from isaaclab_arena.utils.pose import Pose

    usd = tmp_path / "background.usda"
    usd.write_text(background)
    relations = [
        SimpleNamespace(kind="is_anchor", subject="table"),
        SimpleNamespace(kind="at_position", subject="table"),
        SimpleNamespace(kind="on", subject="bowl"),
    ]

    def node(relations=(), initial_pose=None, anchor=False, usd_path=None):
        return SimpleNamespace(
            get_relations=lambda: list(relations),
            get_initial_pose=lambda: initial_pose,
            is_anchor=anchor,
            usd_path=usd_path,
        )

    assets = {
        "droid": node(),
        "table": node(relations[:2], Pose.identity(), anchor=True, usd_path=str(usd)),
        "bowl": node(relations[2:]),
        "banana": node(),
        "plate": node(initial_pose=Pose(position_xyz=(0.3, 0.0, 0.4))),
        **({"ground": GroundPlane()} if ground else {}),
    }
    spec = SimpleNamespace(
        embodiment=SimpleNamespace(id="droid"),
        background=SimpleNamespace(id="table"),
        objects=[SimpleNamespace(id=node_id) for node_id in assets if node_id not in ("droid", "table")],
        object_sets=None,
        relations=relations,
    )
    # The DROID's posed box, as Arena bounds it.
    robot = {"id": "droid", "position_xyz": [0.0] * 3, "world_bbox_min": [-1.02, -0.46, -1.35]}
    robot["world_bbox_max"] = [0.44, 0.46, 0.78]
    table = {"id": "table", "position_xyz": [0.0] * 3, "world_bbox_min": table_box[0], "world_bbox_max": table_box[1]}
    return spec, assets, [robot, table]


def test_the_warnings_name_what_passes_every_check_and_cannot_work(tmp_path: Path):
    """Every sample of this passed on 2026-10-20, and in the sim no layout settled."""
    table = ([-0.35, -0.5, -0.35], [0.35, 0.5, 0.35])
    assert warnings(*scene(tmp_path, table)) == [
        "banana: no relation places it and it has no initial_pose, so it spawns at the origin",
        "relations.1: at_position on table is ignored: table is an anchor, which stays where it is configured"
        " whatever its other relations say",
        "droid: its origin, (0.00, 0.00, 0.00), is inside table's box, so the robot spawns inside table;"
        " give one of them an initial_pose that clears the other",
        "table: /Root, its USD's default prim, is a dynamic rigid body, enabled and not kinematic, and nothing"
        " in the spec holds it up, so in the sim it falls and nothing on it settles",
    ]


def test_a_room_round_the_robot_and_a_ground_under_the_background_are_no_warning(tmp_path: Path):
    """A kitchen's box holds the robot standing in it, as a table's cannot. A ground plane in the
    spec holds a rigid background up, and so does one in the background's own default prim. The
    banana and the pin are still wrong."""
    room = ([-5.0, -5.0, -1.4], [5.0, 5.0, 3.0])
    for case in ({"ground": True}, {"background": MAPLE_TABLE}):
        assert [line.split(":")[0] for line in warnings(*scene(tmp_path, room, **case))] == ["banana", "relations.1"]

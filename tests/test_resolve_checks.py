"""The checks a resolve makes on a `params.usd_path` before Arena sees it.
Against files authored here, so no asset is downloaded and no solve runs."""

from __future__ import annotations

from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace

import pytest

from arena_composer.resolve_layout import SpecError, _placement_summary, check_usd_params

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
    from isaaclab_arena.relations.placement_validation import PlacementCheck

    ran = {check: True for check in PlacementCheck if check not in (PlacementCheck.PHYSICS_SETTLED, PlacementCheck.IK_REACHABLE)}
    layout = sample(0.1, [])
    layout.validation_results.validation_results = ran
    summary = _placement_summary([layout], seed=7, samples=1)
    assert summary["checks"]["on_relation"] is True
    assert summary["not_run"] == ["physics_settled", "ik_reachable"]


def test_nothing_to_place_is_every_sample_passed_with_nothing_checked():
    """A scene of fixed props — no relation — is done as it is, so the skill's
    `passed` of `samples` has to read as done; and `not_run` has to say that
    no check ran, overlap included."""
    summary = _placement_summary([], seed=7, samples=8)
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
    )
    assert summary["success"] is True
    assert {key: summary[key] for key in ("samples", "passed", "failed_samples", "distinct_layouts")} == {
        "samples": 3,
        "passed": 2,
        "failed_samples": [1],
        "distinct_layouts": 2,
    }
    assert summary["check_failures"] == {"no_overlap": 1}

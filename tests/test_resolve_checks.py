"""The checks a resolve makes on a `params.usd_path` before Arena sees it.
Against files authored here, so no asset is downloaded and no solve runs."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from arena_composer.resolve_layout import SpecError, check_usd_params

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

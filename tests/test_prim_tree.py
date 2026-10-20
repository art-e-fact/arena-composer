"""`get_prim_tree`: the prims an object reference can name, written as the reference writes them.

Against a USD built here — a collision cube, a visual-only Xform and a jointed cabinet — served as
every asset's USD, so nothing is downloaded.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from isaaclab_arena.assets.registries import AssetRegistry
from isaaclab_arena.environment_spec.arena_env_graph_conversion_utils import _instantiate_object_reference
from isaaclab_arena.environment_spec.arena_env_graph_types import AssetSpec, ObjectReferenceSpec
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from pxr import Usd, UsdGeom, UsdPhysics

from arena_composer import mcp_server

TABLE = "maple_table_robolab"
SPEC = f"""\
env_name: prims
embodiment: {{id: droid, registry_name: droid_abs_joint_pos}}
background: {{id: {TABLE}, registry_name: {TABLE}}}
objects:
- {{id: cab, registry_name: banana_ycb_robolab}}
relations:
- {{kind: is_anchor, subject: {TABLE}}}
task:
  composition: atomic
  description: nothing to do
  subtasks:
  - kind: NoTask
"""
CABINET = {"prim_path": "cabinet", "object_type": "articulation", "joint_names": ["hinge"]}
BODY, DOOR = ({"prim_path": f"cabinet/{part}", "object_type": "rigid"} for part in ("body", "door"))
CUBE = {"prim_path": "cube", "object_type": "base"}


@pytest.fixture
def usd(tmp_path: Path) -> Path:
    path = tmp_path / "asset.usda"
    stage = Usd.Stage.CreateNew(str(path))
    stage.SetDefaultPrim(UsdGeom.Xform.Define(stage, "/World").GetPrim())
    UsdPhysics.CollisionAPI.Apply(UsdGeom.Cube.Define(stage, "/World/cube").GetPrim())
    UsdGeom.Cube.Define(stage, "/World/decor/mesh")
    UsdPhysics.ArticulationRootAPI.Apply(UsdGeom.Xform.Define(stage, "/World/cabinet").GetPrim())
    for part in ("body", "door"):
        prim = UsdGeom.Cube.Define(stage, f"/World/cabinet/{part}").GetPrim()
        UsdPhysics.RigidBodyAPI.Apply(prim)
        UsdPhysics.CollisionAPI.Apply(prim)
    UsdPhysics.RevoluteJoint.Define(stage, "/World/cabinet/hinge")
    stage.Save()
    return path


@pytest.fixture
def spec(tmp_path: Path, usd: Path, monkeypatch) -> Path:
    monkeypatch.setattr(AssetSpec, "resolve_usd_path", lambda self: str(usd))
    path = tmp_path / "task.yaml"
    path.write_text(SPEC)
    return path


def _tree(spec: Path, **arguments) -> dict:
    result = asyncio.run(mcp_server.mcp.call_tool("get_prim_tree", {"spec": str(spec), **arguments}))
    return json.loads(result.content[0].text)


def _refusal(spec: Path, **arguments) -> str:
    with pytest.raises(ToolError) as refused:
        _tree(spec, **arguments)
    assert not isinstance(refused.value, UnexpectedToolError), f"the reason was withheld: {refused.value}"
    return str(refused.value)


def test_under_the_background_a_prim_is_named_relative_to_it(spec: Path):
    """The visual-only Xform is not there: nothing could reference it."""
    assert _tree(spec, node_id=TABLE) == {"total": 4, "matched": 4, "prims": [CABINET, BODY, DOOR, CUBE]}


def test_under_an_object_the_path_is_relative_too_and_arena_builds_a_reference_from_it(spec: Path, usd: Path):
    (prim,) = _tree(spec, node_id="cab", object_type="articulation")["prims"]
    assert prim == CABINET

    parent = AssetRegistry().get_asset_by_name("simready_usd_object")(usd_path=str(usd), instance_name="cab")
    ref = ObjectReferenceSpec(id="door", parent_id="cab", prim_path=prim["prim_path"], object_type=prim["object_type"])
    # Arena expands it under the object's prim path, {ENV_REGEX_NS}/cab.
    assert _instantiate_object_reference(ref, parent_asset=parent).prim_path_in_parent_usd == "/World/cabinet"


def test_the_filters_and_the_limit_say_what_they_cut(spec: Path):
    assert _tree(spec, node_id=TABLE, query="CAB", limit=2) == {"total": 4, "matched": 3, "prims": [CABINET, BODY]}
    assert _tree(spec, node_id=TABLE, object_type="rigid") == {"total": 4, "matched": 2, "prims": [BODY, DOOR]}


def test_what_is_not_a_parent_or_not_a_filter_is_refused(spec: Path):
    assert "'droid' is not the background or an object" in _refusal(spec, node_id="droid")
    assert "'base', 'rigid' or 'articulation'" in _refusal(spec, node_id=TABLE, object_type="deformable")
    assert "greater than or equal to 1" in _refusal(spec, node_id=TABLE, limit=0)

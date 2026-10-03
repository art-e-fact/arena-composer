"""An asset that composes its mesh in code names no USD. It resolves all the
same, placed by a bounding box computed from what it spawns, and is drawn as that box.
A light or a ground plane has no box to give: it is placed nowhere and drawn as nothing."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest
from isaaclab_arena.assets.registries import AssetRegistry
from isaaclab_arena.relations.passive_collision_objects import get_passive_collision_objects

from arena_composer import payload
from arena_composer.resolve_layout import _asset_entry, bound_spawned_shapes
from arena_composer.spec_edit import SpecError


def test_a_deformable_cube_is_drawn_as_its_bounding_box():
    # One of the entries the asset browser offers; its spawner is a `MeshCuboidCfg`.
    cube = AssetRegistry().get_asset_by_name("deformable_cube")()
    entry = _asset_entry("cube", cube, {"role": "object"}, layout=None)
    assert entry["usd_path"] is None

    built = payload.build_payload({"assets": [entry]}, remote=False)
    (node,) = built["geometries"][built["assets"][0]["geometry"]]
    positions = np.frombuffer(node["positions"], dtype="<f4").reshape(-1, 3)
    np.testing.assert_allclose(positions.min(axis=0), entry["bbox_min"], atol=1e-6)
    np.testing.assert_allclose(positions.max(axis=0), entry["bbox_max"], atol=1e-6)


@pytest.mark.parametrize(
    "registry_name, half_size",
    # `sphere` is offered by the asset browser; `procedural_cube` is not, but a spec may name it.
    [("procedural_cube", [0.025, 0.05, 0.05]), ("sphere", [0.1, 0.1, 0.1])],
)
def test_an_asset_spawned_from_a_shape_is_bounded_by_that_shape(registry_name, half_size):
    assets = {"shape": AssetRegistry().get_asset_by_name(registry_name)()}
    bound_spawned_shapes(assets)
    entry = _asset_entry("shape", assets["shape"], {"role": "object"}, layout=None)
    np.testing.assert_allclose(entry["bbox_min"], np.negative(half_size))
    np.testing.assert_allclose(entry["bbox_max"], half_size)


def test_a_light_and_a_ground_plane_are_placed_nowhere_and_drawn_as_nothing():
    assets = {name: AssetRegistry().get_asset_by_name(name)() for name in ("light", "ground_plane")}
    bound_spawned_shapes(assets)
    # An unset usd_path is what Arena skips as an obstacle, so it has to survive.
    assert get_passive_collision_objects(assets.values()) == []

    entries = [_asset_entry(name, asset, {"role": "object"}, layout=None) for name, asset in assets.items()]
    assert [(entry["solved"], entry["bbox_min"], entry["bbox_max"]) for entry in entries] == [
        (False, [0.0, 0.0, 0.0], [0.0, 0.0, 0.0])
    ] * 2
    built = payload.build_payload({"assets": entries, "lighting": {"light_assets": ["light"]}}, remote=False)
    assert [asset["geometry"] for asset in built["assets"]] == [None, None]
    assert built["geometries"] == []
    # The spec's own light is not drawn, so the viewer's stand-in lights the scene in its place.
    assert built["lighting"]["auto_injected_dome_light"]


def test_a_relation_naming_a_light_is_refused_by_its_index():
    assets = {"lamp": AssetRegistry().get_asset_by_name("light")()}
    relations = [
        SimpleNamespace(kind="is_anchor", subject="table", reference=None),
        SimpleNamespace(kind="on", subject="cube", reference="lamp"),
    ]
    with pytest.raises(SpecError) as refused:
        bound_spawned_shapes(assets, relations)
    assert refused.value.errors == ["relations.1: on names 'lamp', a DomeLightCfg with nothing to place; it takes no relation"]

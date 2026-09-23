"""An asset that composes its mesh in code names no USD. It resolves all the
same, placed by a bounding box computed from what it spawns, and is drawn as that box."""

from __future__ import annotations

import numpy as np
import pytest
from isaaclab_arena.assets.registries import AssetRegistry

from arena_composer import payload
from arena_composer.resolve_layout import _asset_entry, bound_spawned_shapes


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

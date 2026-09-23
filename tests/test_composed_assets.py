"""An asset that composes its mesh in code names no USD. It resolves all the
same, placed by the bounding box Arena computes for it, and is drawn as that box."""

from __future__ import annotations

import numpy as np
from isaaclab_arena.assets.registries import AssetRegistry

from arena_composer import payload
from arena_composer.resolve_layout import _asset_entry


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

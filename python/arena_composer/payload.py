"""The viewer payload: a resolved layout plus the geometry to draw it with.

MessagePack rather than JSON, because the geometry is megabytes of float arrays
and JSON spends most of a second formatting them as decimal text. The arrays
travel as msgpack ``bin`` blobs of little-endian ``f32``/``u32``, which the
viewer casts back in place; everything around them stays an ordinary map, so the
payload keeps JSON's habit of ignoring unknown keys and defaulting absent ones.

The layout half is the same dump ``arena_composer.resolve_layout`` produces —
that stays the human- and agent-readable artifact, and this only picks out the
fields a renderer needs.
"""

from __future__ import annotations

from typing import Any

import msgpack
import numpy as np

from arena_composer.usd import Node, extract, open_stage

# Roles whose USD is drawn by somebody else. An object reference is a prim
# *inside* its parent's stage, so drawing it too would double the geometry.
_UNDRAWN_ROLES = {"object_reference"}


def build_payload(dump: dict[str, Any]) -> dict[str, Any]:
    """Turn a resolved-layout dump into the structure the viewer decodes."""
    geometries: list[list[dict[str, Any]]] = []
    by_usd: dict[str, int] = {}

    def geometry_index(asset: dict[str, Any]) -> int | None:
        """Extract the asset's USD once, however many assets share it."""
        path = asset.get("local_usd_path")
        if not path or asset.get("role") in _UNDRAWN_ROLES:
            return None
        if path not in by_usd:
            by_usd[path] = len(geometries)
            geometries.append([_encode_node(node) for node in extract(open_stage(path))])
        return by_usd[path]

    return {
        "env_name": dump.get("env_name", ""),
        "assets": [
            {
                "id": asset["id"],
                "role": asset.get("role", ""),
                "scale": _floats(asset.get("scale", [1.0, 1.0, 1.0])),
                "position_xyz": _floats(asset["position_xyz"]),
                "rotation_xyzw": _floats(asset["rotation_xyzw"]),
                "bbox_min": _floats(asset["bbox_min"]),
                "bbox_max": _floats(asset["bbox_max"]),
                "geometry": geometry_index(asset),
            }
            for asset in dump["assets"]
        ],
        "geometries": geometries,
        "lighting": {"auto_injected_dome_light": bool(dump.get("lighting", {}).get("auto_injected_dome_light"))},
        "viewer": _viewer(dump.get("viewer")),
    }


def encode(dump: dict[str, Any]) -> bytes:
    """Build and serialize the payload."""
    return msgpack.packb(build_payload(dump), use_bin_type=True)


def _encode_node(node: Node) -> dict[str, Any]:
    surface, material = node.surface, node.material
    return {
        "path": node.path,
        "positions": _blob(surface.positions, np.float32),
        "normals": _blob(surface.normals, np.float32),
        "uvs": _blob(surface.uvs, np.float32),
        "indices": _blob(surface.triangles, np.uint32),
        "base_color": _floats(material.base_color),
        "opacity": float(material.opacity),
        "metallic": float(material.metallic),
        "roughness": float(material.roughness),
        "emissive": _floats(material.emissive),
        "double_sided": bool(material.double_sided),
        "textures": dict(material.textures),
    }


def _blob(array: np.ndarray | None, dtype: Any) -> bytes | None:
    """A C-contiguous little-endian view of an array, as raw bytes."""
    if array is None or array.size == 0:
        return None
    return np.ascontiguousarray(array, dtype=np.dtype(dtype).newbyteorder("<")).tobytes()


def _floats(values: Any) -> list[float]:
    return [float(value) for value in values]


def _viewer(viewer: dict[str, Any] | None) -> dict[str, list[float]] | None:
    if not viewer:
        return None
    return {"eye": _floats(viewer["eye"]), "lookat": _floats(viewer["lookat"])}

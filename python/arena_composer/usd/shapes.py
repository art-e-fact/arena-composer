"""Tessellating USD's analytic gprims — `Cube`, `Sphere`, `Cylinder`, `Cone`,
`Capsule`, `Plane` — into the same triangles a `Mesh` produces.

Not an optional corner of the spec for these scenes: the maple table is authored
as a `Cube` top on four `Cylinder` legs, so a reader that only handles `Mesh`
draws an empty room.

The four round shapes are all surfaces of revolution, so one profile-revolving
helper covers them; caps get their own vertices so the rim stays sharp. Normals
are accumulated from the triangles rather than derived analytically per shape,
which is both shorter and automatically flat where vertices are not shared.
"""

from __future__ import annotations

from typing import Any, Callable

import numpy as np
from pxr import UsdGeom

from .mesh import Surface

_SEGMENTS = 32  # around the axis of revolution
_RINGS = 16  # along a hemisphere's profile


def read_shape(prim: Any) -> Surface | None:
    """Tessellate ``prim`` if it is a shape gprim, else None."""
    builder = _BUILDERS.get(prim.GetTypeName())
    if builder is None:
        return None
    positions, triangles, uvs = builder(prim)
    return _surface(_orient(positions, _axis(prim)), triangles, uvs)


def box(low: Any, high: Any) -> Surface:
    """An axis-aligned box between two corners — what an asset with no USD to read
    is drawn as."""
    low, high = np.asarray(low, dtype=np.float64), np.asarray(high, dtype=np.float64)
    positions, triangles, uvs = _box(*(high - low) / 2.0)
    return _surface(positions + (low + high) / 2.0, triangles, uvs)


def _surface(positions: np.ndarray, triangles: np.ndarray, uvs: np.ndarray) -> Surface:
    return Surface(
        positions=np.ascontiguousarray(positions, dtype=np.float32),
        triangles=np.ascontiguousarray(triangles, dtype=np.uint32),
        normals=_vertex_normals(positions, triangles),
        uvs=np.ascontiguousarray(uvs, dtype=np.float32),
        face_of_triangle=np.arange(len(triangles), dtype=np.int64),
    )


def _attr(prim: Any, name: str, default: float) -> float:
    value = prim.GetAttribute(name).Get()
    return float(value) if value is not None else default


def _axis(prim: Any) -> str:
    """The gprim's axis of symmetry. USD's fallback is Z, which is also ours."""
    value = prim.GetAttribute("axis").Get()
    return str(value) if value else "Z"


def _cube(prim: Any) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    half = _attr(prim, "size", 2.0) / 2.0
    return _box(half, half, half)


def _plane(prim: Any) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """A single quad. `width` spans the axis's first cross direction, `length` the second."""
    width, length = _attr(prim, "width", 2.0) / 2.0, _attr(prim, "length", 2.0) / 2.0
    positions = np.array(
        [[-width, -length, 0.0], [width, -length, 0.0], [width, length, 0.0], [-width, length, 0.0]]
    )
    triangles = np.array([[0, 1, 2], [0, 2, 3]])
    uvs = np.array([[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]])
    return positions, triangles, uvs


def _box(x: float, y: float, z: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Six independent quads, so each face keeps its own flat normal."""
    corners = np.array([[-x, -y, -z], [x, -y, -z], [x, y, -z], [-x, y, -z],
                        [-x, -y, z], [x, -y, z], [x, y, z], [-x, y, z]])
    faces = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (2, 3, 7, 6), (1, 2, 6, 5), (3, 0, 4, 7)]
    positions, triangles, uvs = [], [], []
    for i, face in enumerate(faces):
        positions.append(corners[list(face)])
        base = 4 * i
        triangles += [[base, base + 1, base + 2], [base, base + 2, base + 3]]
        uvs.append(np.array([[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]))
    return np.concatenate(positions), np.array(triangles), np.concatenate(uvs)


def _sphere(prim: Any) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    radius = _attr(prim, "radius", 1.0)
    angles = np.linspace(np.pi, 0.0, 2 * _RINGS + 1)  # south pole to north
    profile = np.stack([radius * np.sin(angles), radius * np.cos(angles)], axis=1)
    return _revolve(profile)


def _cylinder(prim: Any) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    radius, half = _attr(prim, "radius", 1.0), _attr(prim, "height", 2.0) / 2.0
    return _revolve(np.array([[radius, -half], [radius, half]]), cap_bottom=True, cap_top=True)


def _cone(prim: Any) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    radius, half = _attr(prim, "radius", 1.0), _attr(prim, "height", 2.0) / 2.0
    return _revolve(np.array([[radius, -half], [0.0, half]]), cap_bottom=True)


def _capsule(prim: Any) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """A cylinder of ``height`` closed by two hemispheres of ``radius``."""
    radius, half = _attr(prim, "radius", 0.5), _attr(prim, "height", 1.0) / 2.0
    down = np.linspace(np.pi, np.pi / 2, _RINGS + 1)
    up = np.linspace(np.pi / 2, 0.0, _RINGS + 1)
    profile = np.concatenate([
        np.stack([radius * np.sin(down), radius * np.cos(down) - half], axis=1),
        np.stack([radius * np.sin(up), radius * np.cos(up) + half], axis=1),
    ])
    return _revolve(profile)


def _revolve(
    profile: np.ndarray, cap_bottom: bool = False, cap_top: bool = False
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Sweep a (radius, z) profile around +Z, bottom ring first.

    The seam is duplicated — the last column repeats the first position with
    u = 1 — so textures wrap without the last quad running backwards across
    the whole atlas.
    """
    theta = np.linspace(0.0, 2 * np.pi, _SEGMENTS + 1)
    radius, height = profile[:, 0][:, None], profile[:, 1][:, None]
    positions = np.stack(
        [radius * np.cos(theta), radius * np.sin(theta), np.broadcast_to(height, (len(profile), len(theta)))],
        axis=-1,
    ).reshape(-1, 3)
    uvs = np.stack(
        [np.broadcast_to(theta / (2 * np.pi), (len(profile), len(theta))),
         np.broadcast_to(np.linspace(0.0, 1.0, len(profile))[:, None], (len(profile), len(theta)))],
        axis=-1,
    ).reshape(-1, 2)

    stride = len(theta)
    rows, columns = np.meshgrid(np.arange(len(profile) - 1), np.arange(_SEGMENTS), indexing="ij")
    lower, upper = (rows * stride + columns).ravel(), ((rows + 1) * stride + columns).ravel()
    triangles = np.concatenate([
        np.stack([lower, lower + 1, upper + 1], axis=1),
        np.stack([lower, upper + 1, upper], axis=1),
    ])

    parts = [(positions, triangles, uvs)]
    if cap_bottom:
        parts.append(_cap(profile[0], theta, upward=False))
    if cap_top:
        parts.append(_cap(profile[-1], theta, upward=True))
    return _join(parts)


def _cap(rim: np.ndarray, theta: np.ndarray, upward: bool) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """A triangle fan closing one end, with vertices of its own for a sharp rim."""
    radius, z = float(rim[0]), float(rim[1])
    positions = np.concatenate([
        [[0.0, 0.0, z]],
        np.stack([radius * np.cos(theta), radius * np.sin(theta), np.full(len(theta), z)], axis=1),
    ])
    spokes = np.arange(_SEGMENTS) + 1
    triangles = np.stack([np.zeros(_SEGMENTS, dtype=int), spokes, spokes + 1], axis=1)
    if not upward:
        triangles = triangles[:, ::-1]
    uvs = np.concatenate([[[0.5, 0.5]], np.stack([np.cos(theta), np.sin(theta)], axis=1) * 0.5 + 0.5])
    return positions, triangles, uvs


def _join(parts: list[tuple[np.ndarray, np.ndarray, np.ndarray]]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    positions, triangles, uvs, offset = [], [], [], 0
    for part_positions, part_triangles, part_uvs in parts:
        positions.append(part_positions)
        triangles.append(part_triangles + offset)
        uvs.append(part_uvs)
        offset += len(part_positions)
    return np.concatenate(positions), np.concatenate(triangles), np.concatenate(uvs)


def _orient(positions: np.ndarray, axis: str) -> np.ndarray:
    """Rotate +Z-canonical geometry onto the gprim's declared axis."""
    if axis == "X":
        return positions[:, [2, 1, 0]] * [1, 1, -1]
    if axis == "Y":
        return positions[:, [0, 2, 1]] * [1, 1, -1]
    return positions


def _vertex_normals(positions: np.ndarray, triangles: np.ndarray) -> np.ndarray:
    """Area-weighted normals. Flat wherever the shape gave a face its own vertices."""
    normals = np.zeros_like(positions, dtype=np.float64)
    corners = [positions[triangles[:, i]] for i in range(3)]
    face = np.cross(corners[1] - corners[0], corners[2] - corners[0])
    for i in range(3):
        np.add.at(normals, triangles[:, i], face)
    length = np.linalg.norm(normals, axis=1, keepdims=True)
    return np.ascontiguousarray(normals / np.where(length > 0, length, 1.0), dtype=np.float32)


_BUILDERS: dict[str, Callable[[Any], tuple[np.ndarray, np.ndarray, np.ndarray]]] = {
    "Cube": _cube,
    "Sphere": _sphere,
    "Cylinder": _cylinder,
    "Cone": _cone,
    "Capsule": _capsule,
    "Plane": _plane,
}

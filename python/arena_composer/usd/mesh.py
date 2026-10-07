"""Reading a `UsdGeom.Mesh` into triangles a renderer can upload.

Two decisions worth knowing about:

* **Everything is flattened to face corners.** A USD may interpolate normals and
  UVs per vertex, per face, or per corner, and the three cannot share an index
  buffer. Expanding all of them to one value per corner costs duplicated
  positions but removes every interpolation branch downstream — and most Isaac
  assets author face-varying UVs anyway, so the duplication would happen regardless.
* **Triangulation is a fan, vectorised.** The face → triangle map it produces is
  what `GeomSubset` splitting needs, so it is returned rather than recomputed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from pxr import UsdGeom


@dataclass(slots=True)
class Surface:
    """Triangles plus their vertex attributes. Indices address ``positions``."""

    positions: np.ndarray  # (V, 3) float32
    triangles: np.ndarray  # (T, 3) uint32
    normals: np.ndarray | None = None  # (V, 3) float32
    uvs: np.ndarray | None = None  # (V, 2) float32
    face_of_triangle: np.ndarray | None = None  # (T,) int64, authored face index for GeomSubset splitting
    face_count: int = 0  # authored faces in the USD mesh, including any too degenerate to triangulate

    def select_faces(self, faces: np.ndarray) -> "Surface":
        """The triangles belonging to ``faces``, with the vertices they use.

        Compacting matters here in a way it would not in-process: each subset is
        serialized on its own, so carrying the parent mesh's whole vertex array
        would repeat it once per subset — six times over for a Franka link.
        """
        keep = np.isin(self.face_of_triangle, faces)
        used, remapped = np.unique(self.triangles[keep], return_inverse=True)
        return Surface(
            positions=self.positions[used],
            triangles=remapped.reshape(-1, 3).astype(np.uint32),
            normals=None if self.normals is None else self.normals[used],
            uvs=None if self.uvs is None else self.uvs[used],
        )


def read_mesh(prim: Any) -> Surface | None:
    """Flatten a mesh prim to face-corner triangles, or None if it has no geometry."""
    mesh = UsdGeom.Mesh(prim)
    points = _array(mesh.GetPointsAttr().Get())
    counts = _array(mesh.GetFaceVertexCountsAttr().Get(), dtype=np.int64)
    indices = _array(mesh.GetFaceVertexIndicesAttr().Get(), dtype=np.int64)
    if points is None or counts is None or indices is None or len(indices) != counts.sum():
        return None

    triangles, face_of_triangle = _fan_triangulate(counts)
    if triangles.size == 0:
        return None
    # USD's default winding is counter-clockwise, same as Bevy's; the other one
    # has to be flipped or every face is backwards.
    if mesh.GetOrientationAttr().Get() == UsdGeom.Tokens.leftHanded:
        triangles = triangles[:, ::-1]

    normals = _corner_values(_normals(prim, mesh), counts, indices, width=3)
    if normals is None and mesh.GetSubdivisionSchemeAttr().Get() != UsdGeom.Tokens.none:
        normals = _smooth_normals(points, indices, triangles)

    return Surface(
        positions=points[indices].astype(np.float32),
        triangles=triangles.astype(np.uint32),
        normals=normals,
        uvs=_corner_values(_uvs(prim), counts, indices, width=2),
        face_of_triangle=face_of_triangle,
        face_count=len(counts),
    )


def face_subsets(prim: Any) -> list[UsdGeom.Subset]:
    """The prim's face-partitioning `GeomSubset` children.

    Isaac's robots bind a material per subset rather than per mesh, so a reader
    that only looks at the mesh's own binding leaves every link untextured.
    """
    return [
        subset
        for subset in UsdGeom.Subset.GetAllGeomSubsets(UsdGeom.Imageable(prim))
        if subset.GetElementTypeAttr().Get() == UsdGeom.Tokens.face
    ]


def subset_faces(subset: UsdGeom.Subset) -> np.ndarray | None:
    """The faces a subset claims, or None when it claims none."""
    return _array(subset.GetIndicesAttr().Get(), dtype=np.int64)


def unassigned_faces(subsets: list[UsdGeom.Subset], face_count: int) -> np.ndarray:
    """Faces no subset covers — they keep the mesh's own material binding.

    OpenUSD's own family arithmetic, which knows about partition families and
    duplicate indices in a way an `isin` over the union does not.
    """
    return np.asarray(
        UsdGeom.Subset.GetUnassignedIndices(subsets, face_count), dtype=np.int64
    )


def _fan_triangulate(counts: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Fan-triangulate polygons given their corner counts, over corner indices.

    Returns the triangles and, for each, the face it came from. Faces with fewer
    than three corners contribute nothing.
    """
    per_face = np.maximum(counts - 2, 0)
    total = int(per_face.sum())
    if total == 0:
        return np.zeros((0, 3), dtype=np.int64), np.zeros(0, dtype=np.int64)

    face_of_triangle = np.repeat(np.arange(len(counts)), per_face)
    # Corner index where each face starts, and each triangle's rank within its face.
    face_start = np.concatenate(([0], np.cumsum(counts)[:-1]))[face_of_triangle]
    rank = np.arange(total) - np.repeat(np.cumsum(per_face) - per_face, per_face) + 1
    triangles = np.stack([face_start, face_start + rank, face_start + rank + 1], axis=1)
    return triangles, face_of_triangle


def _corner_values(
    source: tuple[Any, str] | None, counts: np.ndarray, indices: np.ndarray, width: int
) -> np.ndarray | None:
    """Expand a (values, interpolation) pair to one value per face corner."""
    if source is None:
        return None
    values, interpolation = source
    values = _array(values)
    if values is None or values.ndim != 2 or values.shape[1] < width:
        return None
    values = values[:, :width]
    corners = len(indices)

    if interpolation == UsdGeom.Tokens.constant or len(values) == 1:
        expanded = np.broadcast_to(values[0], (corners, width))
    elif interpolation == UsdGeom.Tokens.uniform and len(values) == len(counts):
        expanded = np.repeat(values, counts, axis=0)
    elif interpolation == UsdGeom.Tokens.faceVarying and len(values) == corners:
        expanded = values
    elif len(values) > int(indices.max()):  # vertex or varying
        expanded = values[indices]
    else:
        # Authored length matches no interpolation we can honour; a wrong
        # normal is worse than none, since Bevy will compute flat ones.
        return None
    # Authored data is occasionally non-finite (a packing table in the Arena
    # library carries 16 NaN UVs). NaNs spread through tangent generation and
    # texture sampling, so they are neutralised here rather than passed on.
    return np.nan_to_num(
        np.ascontiguousarray(expanded, dtype=np.float32), nan=0.0, posinf=0.0, neginf=0.0
    )


def _smooth_normals(points: np.ndarray, indices: np.ndarray, triangles: np.ndarray) -> np.ndarray:
    """Area-weighted vertex normals, one per face corner, for a mesh that authors none.

    A subdivision surface (USD's default scheme, ``catmullClark``) with no normals
    is drawn smooth by Hydra, which is what Isaac Sim shows. Left to the renderer,
    the corner-flattened mesh gets one flat normal per triangle: Fourier's GR1,
    which authors none, comes out as blocky light and dark patches.
    """
    corners = points[indices].astype(np.float64)
    a, b, c = corners[triangles[:, 0]], corners[triangles[:, 1]], corners[triangles[:, 2]]
    face_normals = np.cross(b - a, c - a)  # its length is twice the triangle's area, which is the weight
    summed = np.zeros(points.shape, dtype=np.float64)
    for corner in range(3):
        np.add.at(summed, indices[triangles[:, corner]], face_normals)
    length = np.linalg.norm(summed, axis=1, keepdims=True)
    return (summed / np.maximum(length, 1e-12))[indices].astype(np.float32)


def _normals(prim: Any, mesh: UsdGeom.Mesh) -> tuple[Any, str] | None:
    """Normals, preferring ``primvars:normals`` over the ``normals`` attribute.

    USD gives the primvar precedence when both are authored, and some exporters
    write only the primvar. Reading just the attribute loses those normals
    entirely, and Bevy then computes flat ones — a faceted surface where a
    smooth one was authored.
    """
    if (source := _primvar(prim, ("normals",))) is not None:
        return source
    attribute = mesh.GetNormalsAttr()
    if not attribute.HasAuthoredValue():
        return None
    return attribute.Get(), mesh.GetNormalsInterpolation()


def _uvs(prim: Any) -> tuple[Any, str] | None:
    """The first texture-coordinate primvar.

    ``st`` is the near-universal name; Omniverse exports occasionally use
    ``st_0`` or ``UVMap`` instead.
    """
    return _primvar(prim, ("st", "st_0", "UVMap", "map1"))


def _primvar(prim: Any, names: tuple[str, ...]) -> tuple[Any, str] | None:
    """The first authored *numeric* primvar among ``names``, flattened through its indices.

    The type check is not paranoia: a kitchen scene in the Arena library authors
    ``primvars:normals`` as a ``token[]`` holding the string ``"faceVarying"`` —
    an exporter that wrote the interpolation into the value. Skipping it lets the
    caller fall back to the real attribute rather than losing the normals.
    """
    primvars = UsdGeom.PrimvarsAPI(prim)
    for name in names:
        primvar = primvars.GetPrimvar(name)
        if not (primvar and primvar.HasValue()):
            continue
        if (values := _array(primvar.ComputeFlattened())) is not None:
            return values, primvar.GetInterpolation()
    return None


def _array(values: Any, dtype: Any = np.float32) -> np.ndarray | None:
    """A contiguous numpy view of a USD array attribute, or None when it is empty
    or not numeric — assets do author a ``token[]`` where a float array belongs."""
    if values is None:
        return None
    try:
        array = np.asarray(values, dtype=dtype)
    except (TypeError, ValueError):
        return None
    return array if array.size else None

"""Projecting one asset's USD stage into flat, drawable pieces.

The contract with the viewer is deliberately narrow: a list of triangle meshes
whose vertices are already in the **asset's own frame** — Arena's frame, Z-up
and metric — each with a material. Every USD convention is resolved here, on the
side that has OpenUSD to resolve it with, so the renderer needs no notion of up
axes, unit scales, xform-op ordering or instancing.

Baking the transforms into the vertices rather than shipping a matrix per node
is what makes that possible without a shared matrix convention: USD composes
row-vector transforms, glam composes column-vector ones, and a decomposed
translate/rotate/scale cannot represent the shear a rotate between two
non-uniform scales produces. The cost is that a mesh repeated inside one asset
is emitted twice; assets that share a USD are still extracted once (see
:mod:`arena_composer.payload`). Revisit when something needs to move at runtime.

Three departures from a general-purpose USD reader, all matching what Isaac Lab
does when it spawns an asset:

* Only the **default prim's** subtree is drawn. Referencing a USD without a prim
  path targets its default prim, so anything beside it — Kit's viewport cameras
  and render settings, and in a couple of object assets a 50 m collision ground
  plane — is authoring the file kept to itself.
* The default prim's own ``xformOp:scale`` is **unbaked**, because Isaac Lab's
  spawner ignores it. The resolver computes its bounding boxes the same way, so
  leaving it in would make the geometry and the solved layout disagree.
* An **unauthored** ``upAxis`` or ``metersPerUnit`` means Z-up metres, not USD's
  Y-up centimetres. Arena composes robot USDs on the fly and authors neither;
  under USD's own fallbacks the robot would lie on its side at 1/100 scale.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator

import numpy as np
from pxr import Gf, Usd, UsdGeom, UsdUtils

_log = logging.getLogger(__name__)

from .material import Material, read_material
from .mesh import Surface, face_subsets, read_mesh, subset_faces, unassigned_faces
from .shapes import read_shape

# Purposes a render-oriented viewer skips: `guide` is annotation, `proxy` is the
# stand-in for a render subtree that is present in these assets anyway.
_SKIPPED_PURPOSES = {UsdGeom.Tokens.guide, UsdGeom.Tokens.proxy}


@dataclass(slots=True)
class Node:
    """One drawable piece: triangles in the asset's frame, and how to shade them."""

    path: str
    surface: Surface
    material: Material


def extract(stage: Usd.Stage) -> list[Node]:
    """Every drawable piece of ``stage``'s default prim, in the asset's frame."""
    root = stage.GetDefaultPrim()
    nodes = []
    with _quiet():
        to_asset = _asset_frame(stage, root)
        for prim in _drawable(stage, root):
            surface = read_mesh(prim) if prim.IsA(UsdGeom.Mesh) else read_shape(prim)
            if surface is None or surface.triangles.size == 0:
                continue
            matrix = to_asset(prim)
            for piece, material in _split(prim, surface):
                nodes.append(Node(str(prim.GetPath()), _place(piece, matrix), material))
    return nodes


@contextmanager
def _quiet() -> Iterator[None]:
    """Collect OpenUSD's diagnostics instead of letting them print.

    Isaac assets bind materials without applying `MaterialBindingAPI`, which is
    legal but deprecated, so OpenUSD warns once per bound prim — several hundred
    lines for one robot, not one of them actionable here.
    """
    delegate = UsdUtils.CoalescingDiagnosticDelegate()
    try:
        yield
    finally:
        suppressed = delegate.TakeUncoalescedDiagnostics()
        if suppressed:
            _log.debug("suppressed %d USD diagnostics, first: %s", len(suppressed), suppressed[0].commentary)


def _drawable(stage: Usd.Stage, root: Usd.Prim | None) -> Iterator[Usd.Prim]:
    """Visible, non-guide prims of the default prim's subtree, instances included.

    Descending into instance proxies is not optional: Isaac assets author their
    geometry as `instanceable` prims referencing a shared library, so the composed
    prim has no children of its own and a traversal that stops at instances walks
    the whole skeleton without finding a single mesh.
    """
    predicate = Usd.TraverseInstanceProxies(Usd.PrimDefaultPredicate)
    walk = iter(Usd.PrimRange(root, predicate) if root else stage.Traverse(predicate))
    for prim in walk:
        # Pruning is what makes the local opinion equivalent to the inherited
        # one — nothing below an invisible prim is reached.
        if prim.GetAttribute("visibility").Get() == UsdGeom.Tokens.invisible:
            walk.PruneChildren()
            continue
        if prim.GetAttribute("purpose").Get() in _SKIPPED_PURPOSES:
            walk.PruneChildren()
            continue
        yield prim


def _split(prim: Usd.Prim, surface: Surface) -> Iterator[tuple[Surface, Material]]:
    """One piece per bound `GeomSubset`, plus whatever faces no subset claimed.

    Isaac's robots bind a material per face subset rather than per mesh, so a
    reader that only asks the gprim for its binding leaves every link untextured.
    """
    partitions = face_subsets(prim)
    if not partitions:
        yield surface, read_material(prim)
        return
    for subset in partitions:
        faces = subset_faces(subset)
        if faces is None:
            continue
        piece = surface.select_faces(faces)
        if piece.triangles.size:
            yield piece, read_material(subset.GetPrim(), fallback=prim)
    rest = unassigned_faces(partitions, surface.face_count)
    if rest.size:
        piece = surface.select_faces(rest)
        if piece.triangles.size:
            yield piece, read_material(prim)


def _asset_frame(stage: Usd.Stage, root: Usd.Prim | None) -> Callable[[Usd.Prim], np.ndarray]:
    """Build ``prim -> 4x4`` taking prim-local points into the asset's frame.

    USD's row-vector convention means the chain reads left to right:
    ``p · localToWorld · unbake · convention``.
    """
    cache = UsdGeom.XformCache()
    convention = _convention(stage)
    unbake = Gf.Matrix4d(1)
    if root:
        world = cache.GetLocalToWorldTransform(root)
        # Strip the default prim's whole contribution, then put back everything
        # but its scale. Doing it as a matrix pair sidesteps having to know the
        # order its xform ops were authored in.
        unbake = world.GetInverse() * _without_scale(world)
    tail = np.array(unbake * convention)
    return lambda prim: np.array(cache.GetLocalToWorldTransform(prim)) @ tail


def _convention(stage: Usd.Stage) -> Gf.Matrix4d:
    """The stage's axis and unit convention, expressed in Arena's frame."""
    matrix = Gf.Matrix4d(1)
    if stage.HasAuthoredMetadata("upAxis") and stage.GetMetadata("upAxis") == "Y":
        matrix = Gf.Matrix4d().SetRotate(Gf.Rotation(Gf.Vec3d(1, 0, 0), 90))
    if stage.HasAuthoredMetadata("metersPerUnit"):
        meters = float(stage.GetMetadata("metersPerUnit"))
        if meters > 0:
            matrix = matrix * Gf.Matrix4d().SetScale(meters)
    return matrix


def _without_scale(matrix: Gf.Matrix4d) -> Gf.Matrix4d:
    decomposed = Gf.Transform(matrix)
    stripped = Gf.Transform()
    stripped.SetTranslation(decomposed.GetTranslation())
    stripped.SetRotation(decomposed.GetRotation())
    return stripped.GetMatrix()


def _place(surface: Surface, matrix: np.ndarray) -> Surface:
    """Move a surface into the payload's conventions: the asset's frame, and V down.

    USD's `primvars:st` puts (0, 0) at the texture's **bottom** left; glTF, wgpu
    and so Bevy put it at the top left. Flipping V here rather than in the
    renderer keeps every convention on the side that has OpenUSD — and without
    it a photogrammetry atlas samples the wrong band entirely, which reads as
    black patches and radial smearing rather than as an upside-down texture.
    """
    linear = matrix[:3, :3]
    surface.positions = np.ascontiguousarray(
        surface.positions @ linear + matrix[3, :3], dtype=np.float32
    )
    if surface.normals is not None:
        # Normals follow the inverse transpose, or non-uniform scale shears them
        # off the surface. Row vectors put the transpose on the other side.
        normals = surface.normals @ np.linalg.inv(linear).T
        length = np.linalg.norm(normals, axis=1, keepdims=True)
        surface.normals = np.ascontiguousarray(
            normals / np.where(length > 0, length, 1.0), dtype=np.float32
        )
    if surface.uvs is not None:
        surface.uvs = np.ascontiguousarray(
            np.column_stack([surface.uvs[:, 0], 1.0 - surface.uvs[:, 1]]), dtype=np.float32
        )
    return surface


def open_stage(path: str) -> Usd.Stage:
    """Open a USD, raising rather than returning an invalid stage."""
    stage = Usd.Stage.Open(path)
    if stage is None:
        raise OSError(f"cannot open USD stage: {path}")
    return stage

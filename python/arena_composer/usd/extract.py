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

Four departures from a general-purpose USD reader, to draw an asset as Isaac Lab
spawns it and as Arena bounds it to place it:

* Only the **default prim's** subtree is drawn. Referencing a USD without a prim
  path targets its default prim, so anything beside it — Kit's viewport cameras
  and render settings, and in a couple of object assets a 50 m collision ground
  plane — is authoring the file kept to itself.
* The default prim's own translate and scale are **unbaked** and its rotation
  is kept, because that is the frame Arena bounds an asset in: from the default
  prim's origin, with its authored rotation, and the spawn scale replacing its
  own, which the viewer applies. Anything else draws the geometry off the
  solved layout. Isaac Lab's spawner replaces the rotation too, with the
  asset's initial pose, so on an asset whose root authors one, Arena's box and
  the sim disagree; this follows the box the layout was solved by.
* ``upAxis`` and ``metersPerUnit`` are **ignored**, authored or not: a stage is
  read as Z-up metres. Isaac Lab references a USD without converting it and
  Arena bounds it as authored, so honouring them draws what neither does — the
  factory peg and hole author Y-up over Z-up geometry and would lie on their
  side, and Arena's robots, composed on the fly, author neither, so USD's
  fallbacks would lay them down at 1/100 scale.
* A robot is **posed** at the joint positions it is reset to, which Arena
  bounds it by, rather than drawn at the pose its file authors.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterator, Mapping

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


def extract(stage: Usd.Stage, joint_pos: Mapping[str, float] | None = None) -> list[Node]:
    """Every drawable piece of ``stage``'s default prim, in the asset's frame.

    `joint_pos` poses an articulation first: a robot's start pose, keyed by joint name or by
    Isaac Lab's regex, revolute in radians. See :func:`_posing`.
    """
    root = stage.GetDefaultPrim()
    nodes = []
    with _quiet():
        to_asset = _asset_frame(stage, root, joint_pos)
        for prim in _drawable(stage, root):
            surface = read_mesh(prim) if prim.IsA(UsdGeom.Mesh) else read_shape(prim)
            if surface is None or surface.triangles.size == 0:
                continue
            matrix = to_asset(prim)
            for piece, material in _split(prim, surface):
                for tile, tile_material in _tiles(piece, material):
                    nodes.append(Node(str(prim.GetPath()), _place(tile, matrix), tile_material))
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


def _tiles(surface: Surface, material: Material) -> Iterator[tuple[Surface, Material]]:
    """One piece per UDIM tile the material's textures are split into, each with its own tile's images.

    A UDIM texture is an image per unit square of UV space, numbered from 1001 at the origin, on by one
    along U and by ten up V: Fourier's GR1T2 has nine along U. A material samples one image, so the
    mesh is split by the tile each triangle falls in, and each piece's UVs are moved into the unit
    square. A tile with no image of a slot draws without it. USD's `st`, before `_place` flips V.
    """
    if surface.uvs is None or not any("<UDIM>" in path for path in material.textures.values()):
        yield surface, material
        return
    tiles = np.floor(surface.uvs[surface.triangles].mean(axis=1)).astype(np.int64)
    numbers = 1001 + tiles[:, 0] + 10 * tiles[:, 1]
    for number in np.unique(numbers):
        keep = numbers == number
        piece = surface.select_triangles(keep)
        piece.uvs = piece.uvs - tiles[keep][0].astype(np.float32)
        textures = {slot: path.replace("<UDIM>", str(number)) for slot, path in material.textures.items()}
        yield piece, replace(material, textures={slot: path for slot, path in textures.items() if os.path.isfile(path)})


def _asset_frame(
    stage: Usd.Stage, root: Usd.Prim | None, joint_pos: Mapping[str, float] | None
) -> Callable[[Usd.Prim], np.ndarray]:
    """Build ``prim -> 4x4`` taking prim-local points into the asset's frame.

    USD's row-vector convention means the chain reads left to right:
    ``p · localToWorld · pose · unbake``, where ``pose`` is the identity for a
    prim no joint moves.
    """
    cache = UsdGeom.XformCache()
    unbake = Gf.Matrix4d(1)
    if root:
        world = cache.GetLocalToWorldTransform(root)
        # Strip the default prim's whole contribution, then put back its
        # rotation. Doing it as a matrix pair sidesteps having to know the
        # order its xform ops were authored in.
        unbake = world.GetInverse() * Gf.Matrix4d().SetRotate(Gf.Transform(world).GetRotation())
    tail = np.array(unbake)
    pose = _posing(stage, root, joint_pos) if root and joint_pos else lambda path: None

    def to_asset(prim: Usd.Prim) -> np.ndarray:
        world = np.array(cache.GetLocalToWorldTransform(prim))
        delta = pose(str(prim.GetPath()))
        return (world if delta is None else world @ delta) @ tail

    return to_asset


def _posing(
    stage: Usd.Stage, root: Usd.Prim, joint_pos: Mapping[str, float]
) -> Callable[[str], np.ndarray | None]:
    """Build ``prim path -> 4x4`` moving a prim's world points from where the file
    authors its link to where `joint_pos` puts it, or None where no joint moves it.

    Arena's own posing, the one it bounds a robot by (``EmbodimentBase.get_bounding_box``),
    imported rather than copied so the robot is drawn filling the box it was placed by.
    Walking out from the root, each link is placed from its parent through the joint's
    two frames, not where the file authors it: a revolute joint turns by its value, a
    prismatic one slides by it, and a fixed, spherical or D6 joint holds at zero. A key
    is a regex Isaac Lab full-matches against joint names; one matching nothing is
    dropped, and a joint no key names stays at zero.

    Imported here rather than at the top because only this needs Arena: reading an
    asset unposed takes `usd-core` alone.
    """
    from isaaclab_arena.utils.usd.articulation import (
        articulation_joint_prims,
        compute_posed_prim_world_deltas,
        resolve_joint_pos_patterns,
        resolve_prim_world_delta,
    )

    joints = resolve_joint_pos_patterns(articulation_joint_prims(root), joint_pos)
    deltas = compute_posed_prim_world_deltas(stage, str(root.GetPath()), joints)
    return lambda path: resolve_prim_world_delta(path, deltas)


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
        # Normals follow the cofactor matrix, or non-uniform scale shears them
        # off the surface. It is the inverse transpose times the determinant,
        # but needs no inverse: Lightwheel's kitchens author a panel as a `Cube`
        # scaled to zero thickness, and its two faces still need their normals.
        # It also keeps each normal on the side its triangle's winding faces,
        # which is what culling goes by; under a mirroring transform the
        # inverse transpose points the other way.
        normals = surface.normals @ np.cross(linear[[1, 2, 0]], linear[[2, 0, 1]])
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

"""Dry-resolve an ArenaEnvGraphSpec into what a viewer needs — no SimulationApp.

Deliberately skips ``ArenaEnvGraphSpec.to_arena_env()``: that builds tasks, which construct
Isaac Lab cfg objects nothing here needs. Instead it instantiates the assets, attaches the
spatial relations, and runs the placement solver directly. USD reads go through the local
asset cache (see :mod:`arena_composer.usd_assets`), so no Kit resolver is required either.

Two conventions worth matching in a viewer:

* Bounding boxes are local and Arena's: measured from the USD default prim's origin with its
  authored rotation kept, and only the reported ``scale`` applied, the frame
  :mod:`arena_composer.usd.extract` draws in. ``world_bbox_min``/``world_bbox_max`` are the box
  Arena checks the layout with: the local one turned by the asset's rotation and moved to its
  position.
* The layout is *one* seeded sample. At runtime Arena redraws a different valid layout from
  its pool on every reset, so pin ``--seed`` if the viewer should stay put between runs.
  ``--samples N`` solves N at once, one per env as Arena does, to say how many pass; the first
  is the one described.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from isaaclab.sim import GroundPlaneCfg, LightCfg
from isaaclab_arena.assets.background import Background
from isaaclab_arena.assets.deformable_object import DeformableObject
from isaaclab_arena.assets.object import Object
from isaaclab_arena.assets.object_reference import ObjectReference
from isaaclab_arena.assets.object_set import RigidObjectSet
from isaaclab_arena.assets.registries import AssetRegistry
from isaaclab_arena.embodiments.embodiment_base import EmbodimentBase
from isaaclab_arena.environment_spec.arena_env_graph_conversion_utils import (
    _attach_spatial_relations_to_assets,
    instantiate_assets_from_spec,
)
from isaaclab_arena.environment_spec.arena_env_graph_spec import ArenaEnvGraphSpec
from isaaclab_arena.environment_spec.placer_params_cfg_override import build_placer_params_from_override
from isaaclab_arena.relations.object_placer import ObjectPlacer
from isaaclab_arena.relations.passive_collision_objects import get_placement_collision_objects
from isaaclab_arena.relations.placement_poses import get_pose_from_layout
from isaaclab_arena.relations.placement_result import PlacementResult
from isaaclab_arena.relations.relations import ClutterOn, FaceTo, NextTo, NotNextTo, On
from isaaclab_arena.relations.validation.types import PlacementCheck
from isaaclab_arena.utils.bounding_box import AxisAlignedBoundingBox
from isaaclab_arena.utils.pose import Pose, PosePerEnv, PoseRange
from isaaclab_arena.utils.usd.prim_tree import load_usd_prim_tree
from isaaclab_arena.utils.yaw import yaw_from_quat_xyzw
from pxr import Usd, UsdGeom, UsdPhysics

from arena_composer import cli, spec_edit
from arena_composer.spec_edit import DEFAULT_SEED, SpecError
from arena_composer.usd_assets import localize, use_local_asset_cache


def resolve_layout(spec: ArenaEnvGraphSpec, seed: int | None = None, samples: int = 1) -> dict[str, Any]:
    """Return a JSON-serializable scene description: assets, USD paths, transforms, bounds.

    `samples` layouts are solved in one batch, an env each; the first is the
    one described, and the placement summary counts how many passed. `seed`
    wins over the spec's ``placer_params.placement_seed``, as Arena's
    ``--placement_seed`` does, and with neither the seed is ``DEFAULT_SEED``.

    Raises :class:`SpecError` for an object whose ``params.usd_path`` cannot be
    used, or that is a light or a ground plane a relation names, and for an
    object reference the solve would place by whose prim has nothing Arena
    bounds; anything else Arena raises passes through. A light or a ground
    plane no relation names is left where it is configured, with an empty box,
    as the sim leaves it out of placement too.
    """
    if samples < 1:
        raise ValueError(f"samples must be at least 1, got {samples}")
    use_local_asset_cache()
    check_usd_params(spec.objects)
    assets = instantiate_assets_from_spec(spec, AssetRegistry())
    check_placeable(assets, spec.relations)
    _attach_spatial_relations_to_assets(spec.relations, assets)

    params = build_placer_params_from_override(spec.placer_params)
    if seed is None:
        seed = DEFAULT_SEED if params.placement_seed is None else params.placement_seed
    params.placement_seed = seed
    # Assets without relations keep their configured pose; the rest are solved together.
    placeable = [asset for asset in assets.values() if asset.get_relations()]
    layouts: list[PlacementResult] = []
    if placeable:
        # The fixed obstacles the solver avoids, picked as ArenaEnvBuilder picks them.
        obstacles = get_placement_collision_objects(placeable, assets.values(), params.solver_params.collision_mode)
        check_reference_boxes(spec, assets, obstacles)
        layouts = ObjectPlacer(params).place(placeable, num_envs=samples, collision_objects=obstacles)
    layout = layouts[0] if layouts else None
    entries = [_asset_entry(node_id, assets[node_id], meta, layout) for node_id, meta in _node_meta(spec).items()]

    return {
        "env_name": spec.env_name,
        "placement": _placement_summary(layouts, seed, samples, {relation.kind for relation in spec.relations}),
        "assets": entries,
        "warnings": warnings(spec, assets, entries),
        # The spec as authored, beside the layout it resolved to. An editor
        # needs both: the layout says where the solver put things, this says
        # what it was asked for, and only this one can be written back.
        "spec": spec.model_dump(mode="json"),
        "relations": [relation.model_dump(mode="json") for relation in spec.relations],
        "task": spec.task.model_dump(mode="json"),
        "lighting": _lighting(spec, assets),
        "viewer": _viewer(assets[spec.background.id]),
    }


def check_usd_params(objects: Iterable[Any]) -> None:
    """Fail early, naming the object, where a ``params.usd_path`` cannot be used.

    A URL is the one input Arena does not vet: the spec validates with any
    string there, and a file that is not on the server, or a prop that is not
    the single rigid body the sim will spawn it as, would otherwise surface
    from inside the solver, or not until the sim. The download this costs is
    the one the resolve was about to make anyway.

    `objects` are the spec's object entries — anything with an `id`, a
    `registry_name` and `params`.
    """
    from isaaclab_arena.assets.simready_constants import SIMREADY_PHYSICS_VARIANTS, SIMREADY_USD_OBJECT_REGISTRY_NAME
    from isaaclab_arena.utils.usd.rigid_bodies import read_asset_rigid_body_paths

    errors = []
    for obj in objects:
        usd_path = obj.params.get("usd_path")
        if not usd_path:
            continue
        if "://" not in usd_path and not Path(usd_path).is_file():
            errors.append(f"{obj.id}: no file at {usd_path}")
            continue
        try:
            local = localize(usd_path)
        except Exception as error:
            errors.append(f"{obj.id}: {error}")
            continue
        if obj.registry_name == SIMREADY_USD_OBJECT_REGISTRY_NAME:
            bodies = read_asset_rigid_body_paths(local, SIMREADY_PHYSICS_VARIANTS)
            if len(bodies) != 1:
                errors.append(f"{obj.id}: {usd_path} has {len(bodies)} rigid bodies; the sim spawns it as exactly one")
    if errors:
        raise SpecError(errors)


def check_placeable(assets: dict[str, Any], relations: Iterable[Any] = ()) -> None:
    """Refuse a relation that names a light or a ground plane, by index.

    Neither has a box to place it by: Arena bounds an object by its USD or its
    primitive shape, and would stop on an assert inside the solver.
    """
    unplaceable = {node_id: spawn for node_id, asset in assets.items() if (spawn := _unplaceable(asset))}
    errors = [
        f"relations.{index}: {relation.kind} names {node_id!r}, a {unplaceable[node_id]} with nothing to place;"
        " it takes no relation"
        for index, relation in enumerate(relations)
        for node_id in (relation.subject, relation.reference)
        if node_id in unplaceable
    ]
    if errors:
        raise SpecError(errors)


def check_reference_boxes(spec: ArenaEnvGraphSpec, assets: dict[str, Any], obstacles: Iterable[Any] = ()) -> None:
    """Refuse an object reference the solve places by whose prim has nothing Arena bounds, by index.

    Arena bounds a prim by its default-purpose geometry alone, so a reference to a collision proxy
    or a guide has an empty box, and the solver stops on an assert that names neither. The solve
    places by a reference's box when a relation names it, and as a fixed obstacle, one of
    `obstacles`, when none does.
    """
    named = {node_id for relation in spec.relations for node_id in (relation.subject, relation.reference)}
    obstacles = list(obstacles)
    errors = [
        f"object_references.{index}.prim_path: {reference.prim_path} {_unbounded(assets[reference.id])},"
        f" so {reference.id} has no box to place by"
        for index, reference in enumerate(spec.object_references or [])
        if (reference.id in named or assets[reference.id] in obstacles)
        and assets[reference.id].get_bounding_box().is_empty.any()
    ]
    if errors:
        raise SpecError(errors)


def _unbounded(reference: ObjectReference) -> str:
    """What the referenced prim holds in place of the default-purpose geometry Arena bounds."""
    stage = Usd.Stage.Open(localize(reference.parent_asset.usd_path))
    prims = Usd.PrimRange(stage.GetPrimAtPath(reference.prim_path_in_parent_usd), Usd.TraverseInstanceProxies())
    purposes = {UsdGeom.Imageable(prim).ComputePurpose() for prim in prims if prim.IsA(UsdGeom.Gprim)}
    purposes -= {UsdGeom.Tokens.default_}
    if not purposes:
        return "holds no geometry"
    only = " and ".join(sorted(purposes))
    return f"holds only {only}-purpose geometry, and Arena bounds only default-purpose geometry"


def _unplaceable(asset: Any) -> str | None:
    """The spawner of a light or a ground plane, which has no bounds to place or draw it by."""
    spawn = asset.object_cfg.spawn if isinstance(asset, Object) else None
    return type(spawn).__name__ if isinstance(spawn, (LightCfg, GroundPlaneCfg)) else None


def warnings(spec: ArenaEnvGraphSpec, assets: dict[str, Any], entries: list[dict[str, Any]]) -> list[str]:
    """What passes every check a dry solve runs and still cannot work in the sim, a line each.

    `entries` are the dump's assets. None of these refuses the spec: Arena builds each of them.
    """
    lines = [
        f"{node_id}: no relation places it and it has no initial_pose, so it spawns at the origin"
        for node_id in [obj.id for obj in spec.objects] + [members.id for members in spec.object_sets or []]
        if not assets[node_id].get_relations()
        and assets[node_id].get_initial_pose() is None
        and not _unplaceable(assets[node_id])
    ]
    # The solver moves only what is not an anchor, and reads only its relations; Arena applies no
    # marker to an anchor either.
    anchors = {node_id for node_id, asset in assets.items() if getattr(asset, "is_anchor", False)}
    lines += [
        f"relations.{index}: {relation.kind} on {relation.subject} is ignored: {relation.subject} is an"
        " anchor, which stays where it is configured whatever its other relations say"
        for index, relation in enumerate(spec.relations)
        if relation.subject in anchors and relation.kind != "is_anchor"
    ]
    boxes = {entry["id"]: entry for entry in entries}
    robot = boxes[spec.embodiment.id]
    for node_id in sorted(anchors):
        low, high = boxes[node_id]["world_bbox_min"], boxes[node_id]["world_bbox_max"]
        # A room's box holds the whole robot, as it should; a table's cannot.
        holds_robot = all(a <= b for a, b in zip(low, robot["world_bbox_min"])) and all(
            a >= b for a, b in zip(high, robot["world_bbox_max"])
        )
        if all(a < b < c for a, b, c in zip(low, robot["position_xyz"], high)) and not holds_robot:
            origin = ", ".join(f"{value:.2f}" for value in robot["position_xyz"])
            lines.append(
                f"{spec.embodiment.id}: its origin, ({origin}), is inside {node_id}'s box, so the robot spawns"
                f" inside {node_id}; give one of them an initial_pose that clears the other"
            )
    background = assets[spec.background.id]
    grounded = any(_unplaceable(asset) == GroundPlaneCfg.__name__ for asset in assets.values())
    if background.usd_path and not grounded and (body := _falling_root(background.usd_path)):
        lines.append(
            f"{spec.background.id}: {body}, its USD's default prim, is a dynamic rigid body, enabled and not"
            " kinematic, and nothing in the spec holds it up, so in the sim it falls and nothing on it settles"
        )
    return lines


def _falling_root(usd_path: str) -> str | None:
    """The default prim's path where it is a dynamic rigid body, as Isaac Lab spawns it, else None.

    Arena takes a background's root to be non-physical: a rigid one spawns as authored and falls,
    with whatever ground the file has beside it left out, since only the default prim is spawned.
    `table_oak_robolab`'s fixture does both; `maple_table_robolab` wraps the same table in a scene
    whose root holds the table and a ground.
    """
    stage = Usd.Stage.Open(localize(usd_path))  # held: a prim of a stage that is gone is invalid
    root = stage.GetDefaultPrim()
    body = UsdPhysics.RigidBodyAPI(root)
    if root and root.HasAPI(UsdPhysics.RigidBodyAPI) and body.GetRigidBodyEnabledAttr().Get():
        return None if body.GetKinematicEnabledAttr().Get() else str(root.GetPath())
    return None


def resolve_layout_from_yaml(path: str | Path, seed: int | None = None, samples: int = 1) -> dict[str, Any]:
    """Load an env graph spec YAML and resolve it."""
    spec_edit.check_entry(path)
    spec = ArenaEnvGraphSpec.from_yaml(path)
    return {"spec_path": str(Path(path).resolve()), **resolve_layout(spec, seed=seed, samples=samples)}


def prim_tree(path: str | Path, node_id: str) -> list[dict[str, Any]]:
    """The prims of `node_id`'s USD an object reference can name: those in physics or collision, and
    their ancestors, less those with nothing Arena bounds (`check_reference_boxes`). Each `prim_path`
    is what the reference writes: relative, which Arena expands under the parent's prim path."""
    spec_edit.check_entry(path)
    spec = ArenaEnvGraphSpec.from_yaml(path)
    parents = {spec.background.id: spec.background} | {obj.id: obj for obj in spec.objects}
    if node_id not in parents:
        raise SpecError(
            [f"{node_id!r} is not the background or an object, the parents a reference can have: {', '.join(parents)}"]
        )
    use_local_asset_cache()
    usd_path = localize(parents[node_id].resolve_usd_path())
    stage = Usd.Stage.Open(usd_path)
    root = stage.GetDefaultPrim().GetPath()
    # Bounded as Arena bounds a reference. A Lightwheel kitchen's colliders are guides, half the prims listed.
    bounds = UsdGeom.BBoxCache(Usd.TimeCode.Default(), includedPurposes=[UsdGeom.Tokens.default_])
    return [
        {
            "prim_path": record.relative_path,
            "object_type": record.object_type.value,
            **({"joint_names": list(record.joint_names)} if record.joint_names else {}),
        }
        for record in load_usd_prim_tree(usd_path)
        if not bounds.ComputeWorldBound(stage.GetPrimAtPath(root.AppendPath(record.relative_path)))
        .ComputeAlignedRange()
        .IsEmpty()
    ]


def _node_meta(spec: ArenaEnvGraphSpec) -> dict[str, dict[str, Any]]:
    """Spec node id → role and spec-only fields, in the order the dump lists them."""
    meta: dict[str, dict[str, Any]] = {
        spec.embodiment.id: {"role": "embodiment", "registry_name": spec.embodiment.registry_name},
        spec.background.id: {"role": "background", "registry_name": spec.background.registry_name},
    }
    for obj in spec.objects:
        meta[obj.id] = {"role": "object", "registry_name": obj.registry_name}
    for object_set in spec.object_sets or []:
        meta[object_set.id] = {"role": "object_set", "members": object_set.members}
    for ref in spec.object_references or []:
        meta[ref.id] = {"role": "object_reference", "parent_id": ref.parent_id}
    return meta


def _asset_entry(node_id: str, asset: Any, meta: dict[str, Any], layout: PlacementResult | None) -> dict[str, Any]:
    """Describe one asset: identity, geometry source, transform, and local and world bounds."""
    pose = _pose(asset, layout)
    bbox = _bounding_box(asset)
    # Turned as the solver turns a box, by the whole rotation; an empty box's corners would turn to NaN.
    world = bbox if bbox.is_empty.any() else bbox.rotated_by_quat(pose.rotation_xyzw)
    world = world.translated(pose.position_xyz)
    return {
        "id": node_id,
        **meta,
        "name": asset.name,
        "prim_path": getattr(asset, "prim_path", None),
        "object_type": _enum_value(getattr(asset, "object_type", None)),
        **_geometry(asset),
        "is_anchor": bool(getattr(asset, "is_anchor", False)),
        "solved": layout is not None and asset in layout.positions,
        "position_xyz": list(pose.position_xyz),
        "rotation_xyzw": list(pose.rotation_xyzw),
        "yaw_rad": yaw_from_quat_xyzw(pose.rotation_xyzw),
        "bbox_min": _xyz(bbox.min_point),
        "bbox_max": _xyz(bbox.max_point),
        "world_bbox_min": _xyz(world.min_point),
        "world_bbox_max": _xyz(world.max_point),
    }


def _geometry(asset: Any) -> dict[str, Any]:
    """Return the USD the asset spawns from, plus what is needed to draw it."""
    if isinstance(asset, EmbodimentBase):
        # The robot USD is composed (arm + stand) at construction; joints must be posed to match.
        source = asset.get_placement_geometry_source()
        return {
            "prim_path": asset.scene_config.robot.prim_path,
            **_usd_paths(source.usd_path),
            "scale": list(source.scale),
            "joint_pos": dict(source.joint_pos),
        }
    if isinstance(asset, ObjectReference):
        # A reference has no USD of its own — it is a prim inside its parent's stage.
        parent = asset.parent_asset
        return {
            **_usd_paths(parent.usd_path),
            "scale": list(parent.scale),
            "prim_path_in_parent_usd": asset.prim_path_in_parent_usd,
        }
    if isinstance(asset, RigidObjectSet):
        # One member per env, at its own scale; env 0's is what a single-env viewer shows.
        member = asset.objects[asset.variant_indices_by_env[0] if asset.variant_indices_by_env else 0]
        return {
            **_usd_paths(member.usd_path or None),
            "scale": list(member.scale),
            "member_usd_paths": [obj.usd_path for obj in asset.objects],
        }
    if isinstance(asset, DeformableObject):
        # Not an Object: it spawns from a spawner config and has no usd_path of its
        # own, so the payload draws its bounding box.
        # ponytail: the teddy bear's spawner does load a USD, but a centimetre one the
        # extractor would draw at 1/100 size (usd/AGENTS.md, Known gaps). Read
        # spawner_cfg.usd_path and .scale here once the extractor agrees with Arena.
        return {**_usd_paths(None), "scale": [1.0, 1.0, 1.0]}
    # Object and Background. usd_path is unset for one spawned from a shape,
    # and the payload draws its bounding box.
    return {**_usd_paths(asset.usd_path or None), "scale": list(asset.scale)}


def _bounding_box(asset: Any) -> Any:
    """Local bounds, using the variant this env actually spawns for an object set."""
    if isinstance(asset, RigidObjectSet) and asset.variant_indices_by_env:
        # Env 0's variant, the one the described layout was solved with. Asked
        # for every env assigned, since Arena asserts the count matches.
        per_env = asset.get_bounding_box_per_env(num_envs=len(asset.variant_indices_by_env))
        return AxisAlignedBoundingBox(min_point=per_env.min_point[:1], max_point=per_env.max_point[:1])
    if _unplaceable(asset):
        # A light or a ground plane (check_placeable): an empty box, drawn as nothing.
        return AxisAlignedBoundingBox(min_point=(0.0, 0.0, 0.0), max_point=(0.0, 0.0, 0.0))
    return asset.get_bounding_box()


def _usd_paths(usd_path: str | None) -> dict[str, str | None]:
    """Report the authored USD path and the local copy a viewer can open."""
    return {"usd_path": usd_path, "local_usd_path": localize(usd_path) if usd_path else None}


def _pose(asset: Any, layout: PlacementResult | None) -> Pose:
    """Solved pose when the asset took part in placement, else its configured pose."""
    if layout is not None and asset in layout.positions:
        return get_pose_from_layout(asset, layout)
    pose = asset.get_initial_pose()
    if isinstance(pose, PosePerEnv):
        return pose.poses[0]
    if isinstance(pose, PoseRange):
        return pose.get_midpoint()
    return pose or Pose.identity()


#: The relation kind each of Arena's relation checks tests. Arena runs every check it has and
#: passes one with nothing to test, so a check for a kind the spec does not use goes in `not_run`.
_CHECKED_KIND = {
    PlacementCheck.ON_RELATION: On.name,
    PlacementCheck.CLUTTER_ON_RELATION: ClutterOn.name,
    PlacementCheck.NEXT_TO: NextTo.name,
    PlacementCheck.NOT_NEXT_TO: NotNextTo.name,
    PlacementCheck.FACE_TO: FaceTo.name,
}


def _placement_summary(layouts: list[PlacementResult], seed: int, samples: int, kinds: set[str]) -> dict[str, Any]:
    """Whether the first sample passed the checks a dry solve can run, which it
    could not, and how the rest fared.

    A pass here is necessary for the sim to accept the layout, not sufficient,
    since physics settling and IK reach are the simulator's to decide and
    `not_run` says so, as it does a check for a relation kind not in `kinds`,
    the ones the spec uses. The samples are the layouts Arena would draw on as
    many resets; they can converge on the same one, so `distinct_layouts` counts
    them rather than taking `samples` for it.

    With nothing to place — no relation — every sample is the configured poses,
    no check ran and none failed: `passed` of `samples`, since a scene of fixed
    props is done as it is, and no `success`, which is the first sample's
    verdict, where there is none.
    """
    if not layouts:
        return {
            "seed": seed,
            "solved_assets": 0,
            "checks": {},
            "failed_checks": [],
            "not_run": [str(check) for check in PlacementCheck],
            "samples": samples,
            "passed": samples,
            "failed_samples": [],
            "distinct_layouts": 1,
            "check_failures": {},
        }
    layout = layouts[0]
    checks = {
        check: passed
        for check, passed in layout.validation_results.validation_results.items()
        if _CHECKED_KIND.get(check) in kinds | {None}
    }
    failed = [index for index, sample in enumerate(layouts) if not sample.success]
    return {
        "seed": seed,
        "solved_assets": len(layout.positions),
        "success": layout.success,
        "final_loss": layout.final_loss,
        "attempts": layout.attempts,
        "checks": {str(check): passed for check, passed in checks.items()},
        "failed_checks": layout.validation_results.get_failed_validation_check_names,
        "not_run": [str(check) for check in PlacementCheck if check not in checks],
        "samples": len(layouts),
        "passed": len(layouts) - len(failed),
        "failed_samples": failed,
        "distinct_layouts": len({_rounded(sample) for sample in layouts}),
        "check_failures": dict(
            Counter(name for sample in layouts for name in sample.validation_results.get_failed_validation_check_names)
        ),
    }


def _rounded(layout: PlacementResult) -> frozenset[tuple[Any, ...]]:
    """A layout to the millimetre and the milliradian: two closer than that are one."""
    return frozenset(
        (
            asset.name,
            tuple(round(float(value), 3) for value in position),
            round(float(layout.orientations.get(asset, 0.0)), 3),
        )
        for asset, position in layout.positions.items()
    )


def _lighting(spec: ArenaEnvGraphSpec, assets: dict[str, Any]) -> dict[str, Any]:
    """Report where light comes from — the graph spec has no lighting fields of its own.

    Arena injects a dome light at build time unless a light asset or a light baked into the
    background USD is found, so a viewer has to do the same to match what the sim renders.
    """
    from isaaclab_arena.utils.usd.helpers import has_light, open_stage

    light_assets = [node_id for node_id, asset in assets.items() if "light" in (asset.tags or [])]
    background_usd = assets[spec.background.id].usd_path
    background_has_light = False
    if not light_assets and background_usd:
        with open_stage(localize(background_usd)) as stage:
            background_has_light = has_light(stage)
    return {
        "light_assets": light_assets,
        "background_usd_has_light": background_has_light,
        # What build_arena_env_from_graph_spec() would add on top of the spec.
        "auto_injected_dome_light": not light_assets and not background_has_light,
    }


def _viewer(background: Any) -> dict[str, list[float]] | None:
    """The background's suggested camera framing, or None when it leaves framing to the viewer."""
    viewer_cfg = background.get_viewer_cfg() if isinstance(background, Background) else None
    if viewer_cfg is None:
        return None
    return {"eye": list(viewer_cfg.eye), "lookat": list(viewer_cfg.lookat)}


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _xyz(point: Any) -> list[float]:
    """Flatten a (1, 3) bounding-box tensor to a plain [x, y, z]."""
    return [float(coordinate) for coordinate in point.flatten().tolist()]


def summary(dump: dict[str, Any]) -> str:
    """The dump's first line: the env, the seed and the placement verdict."""
    placement = dump["placement"]
    if "success" not in placement:
        status = "nothing to solve"
    elif placement["success"]:
        status = "valid layout"
    else:
        status = "INVALID layout, failed: " + ", ".join(placement["failed_checks"])
    if placement.get("samples", 1) > 1:
        status += f"; {placement['passed']}/{placement['samples']} samples passed"
        status += f", {placement['distinct_layouts']} distinct"
    if placement.get("not_run"):
        # Not "not run dry": a check for a relation kind the spec does not use
        # has nothing to check, in the sim either.
        status += "; not run: " + ", ".join(placement["not_run"])
    return f"{dump['env_name']}  (seed {placement['seed']}, {status})"


def _format_text(dump: dict[str, Any]) -> str:
    """Render the dump as a terminal-readable summary."""
    lines = [summary(dump), *(f"warning: {line}" for line in dump["warnings"])]
    for asset in dump["assets"]:
        position = ", ".join(f"{value:+.3f}" for value in asset["position_xyz"])
        rotation = ", ".join(f"{value:+.3f}" for value in asset["rotation_xyzw"])
        anchor = " [anchor]" if asset["is_anchor"] else ""
        lines.append(f"\n{asset['id']} ({asset['role']}){anchor}")
        size = [high - low for high, low in zip(asset["bbox_max"], asset["bbox_min"])]
        lines.append(f"  pos    ({position})   quat_xyzw ({rotation})")
        lines.append(f"  bbox   {_format_xyz(asset['bbox_min'])} .. {_format_xyz(asset['bbox_max'])}")
        lines.append(f"  size   {_format_xyz(size)}")
        lines.append(f"  usd    {asset['usd_path']}")
        if asset["local_usd_path"] != asset["usd_path"]:
            lines.append(f"  local  {asset['local_usd_path']}")
    if dump["viewer"]:
        eye, lookat = _format_xyz(dump["viewer"]["eye"]), _format_xyz(dump["viewer"]["lookat"])
        lines.append(f"\ncamera   eye {eye} → lookat {lookat}")
    if dump["lighting"]["auto_injected_dome_light"]:
        lines.append("\nno light in the spec or background USD — Arena injects a default dome light")
    return "\n".join(lines)


def _format_xyz(values: list[float]) -> str:
    return "(" + ", ".join(f"{value:+.3f}" for value in values) + ")"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("spec", type=Path, help="Path to an ArenaEnvGraphSpec YAML")
    cli.add_spec_options(parser)
    parser.add_argument(
        "--samples",
        type=int,
        default=1,
        help="Solve this many layouts at once, an env each, and count how many pass; the first is described",
    )
    parser.add_argument("--format", choices=("json", "text"), default="json", help="Output format (default: json)")
    parser.add_argument("--output", type=Path, help="Write to this file instead of stdout")
    parser.add_argument(
        "--payload",
        type=Path,
        help="Also pack the layout and its USD geometry here, for `arena-viewer`",
    )
    args = parser.parse_args(argv)

    cli.apply_spec_options(parser, args)
    # Arena and the solver print progress to stdout; keep it off the machine-readable output.
    with contextlib.redirect_stdout(sys.stderr):
        dump = resolve_layout_from_yaml(args.spec, seed=args.seed, samples=args.samples)

    text = json.dumps(dump, indent=2) if args.format == "json" else _format_text(dump)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)

    if args.payload:
        # Imported here so the dump does not pay for numpy and the extractor.
        from arena_composer.payload import encode

        with contextlib.redirect_stdout(sys.stderr):
            args.payload.write_bytes(encode(dump))
    return 0


if __name__ == "__main__":
    sys.exit(main())

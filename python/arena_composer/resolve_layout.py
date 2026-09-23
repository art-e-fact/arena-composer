"""Dry-resolve an ArenaEnvGraphSpec into what a viewer needs — no SimulationApp.

Deliberately skips ``ArenaEnvGraphSpec.to_arena_env()``: that builds tasks, which construct
Isaac Lab cfg objects nothing here needs. Instead it instantiates the assets, attaches the
spatial relations, and runs the placement solver directly. USD reads go through the local
asset cache (see :mod:`arena_composer.usd_assets`), so no Kit resolver is required either.

Two conventions worth matching in a viewer:

* Bounding boxes are local (root-relative) and follow Isaac Lab's spawn convention — the USD
  default prim's own ``xformOp:scale`` is unbaked, only the reported ``scale`` applies.
* The layout is *one* seeded sample. At runtime Arena redraws a different valid layout from
  its pool on every reset, so pin ``--seed`` if the viewer should stay put between runs.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path
from typing import Any

from isaaclab.sim import CuboidCfg, SphereCfg
from isaaclab_arena.assets.background import Background
from isaaclab_arena.assets.deformable_object import DeformableObject
from isaaclab_arena.assets.object import Object
from isaaclab_arena.assets.object_reference import ObjectReference
from isaaclab_arena.assets.object_set import RigidObjectSet
from isaaclab_arena.assets.registries import AssetRegistry
from isaaclab_arena.embodiments.embodiment_base import EmbodimentBase
from isaaclab_arena.environment_spec.arena_env_graph_conversion_utils import (
    _attach_spatial_relations_to_assets,
    build_checks_for_placer_params,
    instantiate_assets_from_spec,
)
from isaaclab_arena.environment_spec.arena_env_graph_spec import ArenaEnvGraphSpec
from isaaclab_arena.environments.relation_solver_interface import _should_include_background_mesh
from isaaclab_arena.relations.object_placer import ObjectPlacer
from isaaclab_arena.relations.object_placer_params import ObjectPlacerParams
from isaaclab_arena.relations.passive_collision_objects import get_passive_collision_objects
from isaaclab_arena.relations.placement_events import get_pose_from_layout
from isaaclab_arena.relations.placement_result import PlacementResult
from isaaclab_arena.relations.relations import get_anchor_objects
from isaaclab_arena.utils.bounding_box import AxisAlignedBoundingBox
from isaaclab_arena.utils.pose import Pose, PosePerEnv, PoseRange

from arena_composer.usd_assets import localize, use_local_asset_cache

DEFAULT_SEED = 42


class SpecError(Exception):
    """A spec that validates but names an asset the sim could not use. One
    line per asset, since a scene wrong in one place is often wrong in two."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("\n".join(errors))
        self.errors = errors


def resolve_layout(spec: ArenaEnvGraphSpec, seed: int = DEFAULT_SEED) -> dict[str, Any]:
    """Return a JSON-serializable scene description: assets, USD paths, transforms, bounds.

    Raises :class:`SpecError` for an object whose ``params.usd_path`` cannot be
    used, or that spawns neither a USD nor a shape to bound it by; anything
    else Arena raises passes through.
    """
    use_local_asset_cache()
    check_usd_params(spec.objects)
    assets = instantiate_assets_from_spec(spec, AssetRegistry())
    bound_spawned_shapes(assets)
    _attach_spatial_relations_to_assets(spec.relations, assets)

    # Assets without relations keep their configured pose; the rest are solved together.
    placeable = [asset for asset in assets.values() if asset.get_relations()]
    layout: PlacementResult | None = None
    if placeable:
        params = replace(build_checks_for_placer_params(spec), placement_seed=seed)
        layout = ObjectPlacer(params).place(
            placeable,
            num_envs=1,
            collision_objects=_collision_objects(assets, placeable, params),
        )[0]

    return {
        "env_name": spec.env_name,
        "placement": _placement_summary(layout, seed),
        "assets": [_asset_entry(node_id, assets[node_id], meta, layout) for node_id, meta in _node_meta(spec).items()],
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


def bound_spawned_shapes(assets: dict[str, Any]) -> None:
    """Give each object spawned from a shape, not a USD, the bounding box of that shape.

    Arena's ``Object.get_bounding_box`` reads the box from ``usd_path``, which
    these leave unset, and returns one already set as it is. So the solver
    places them by this box, and the payload draws it.
    """
    errors = []
    for node_id, asset in assets.items():
        # An object set names no USD of its own either, but bounds itself by its members'.
        if not isinstance(asset, Object) or isinstance(asset, RigidObjectSet) or asset.usd_path:
            continue
        spawn = asset.object_cfg.spawn
        if isinstance(spawn, CuboidCfg):
            half_size = [size / 2 for size in spawn.size]
        elif isinstance(spawn, SphereCfg):
            half_size = [spawn.radius] * 3
        else:
            errors.append(f"{node_id}: spawns a {type(spawn).__name__}, which has no bounding box to place it by")
            continue
        asset.bounding_box = AxisAlignedBoundingBox(
            min_point=tuple(-half for half in half_size), max_point=tuple(half_size)
        )
        # get_bounding_box asserts usd_path is not None before reading the box, and an object
        # built from a spawner_cfg (`sphere`) has None where the procedural ones have "".
        asset.usd_path = ""
    if errors:
        raise SpecError(errors)


def resolve_layout_from_yaml(path: str | Path, seed: int = DEFAULT_SEED) -> dict[str, Any]:
    """Load an env graph spec YAML and resolve it."""
    spec = ArenaEnvGraphSpec.from_yaml(path)
    return {"spec_path": str(Path(path).resolve()), **resolve_layout(spec, seed=seed)}


def _collision_objects(assets: dict[str, Any], placeable: list[Any], params: ObjectPlacerParams) -> list[Any]:
    """Fixed obstacles the solver avoids, selected the way ArenaEnvBuilder selects them."""
    scene_assets = list(assets.values())
    collision_mode = params.solver_params.collision_mode
    return get_passive_collision_objects(
        scene_assets,
        include_background=_should_include_background_mesh(placeable, scene_assets, collision_mode),
        # An anchored reference is placed against, so its subtree is cut out of the parent mesh.
        background_mesh_exclusions=[a for a in get_anchor_objects(placeable) if isinstance(a, ObjectReference)],
    )


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
    """Describe one asset: identity, geometry source, transform, and local bounds."""
    pose = _pose(asset, layout)
    bbox = _bounding_box(asset)
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
        "bbox_min": _xyz(bbox.min_point),
        "bbox_max": _xyz(bbox.max_point),
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
        # One member per env; env 0 is what a single-env viewer shows.
        return {
            **_usd_paths(asset.object_usd_paths[0]),
            "scale": [1.0, 1.0, 1.0],
            "member_usd_paths": asset.member_usd_paths,
        }
    if isinstance(asset, DeformableObject):
        # Not an Object: it spawns from a spawner config and has no usd_path of its
        # own, so the payload draws its bounding box.
        # ponytail: the teddy bear's spawner does load a USD, but a centimetre one the
        # extractor would draw at 1/100 size (usd/AGENTS.md, Known gaps). Read
        # spawner_cfg.usd_path and .scale here once the extractor agrees with Arena.
        return {**_usd_paths(None), "scale": [1.0, 1.0, 1.0]}
    # Object and Background. usd_path is empty for one spawned from a shape
    # (bound_spawned_shapes), and the payload draws its bounding box.
    return {**_usd_paths(asset.usd_path or None), "scale": list(asset.scale)}


def _bounding_box(asset: Any) -> Any:
    """Local bounds, using the variant this env actually spawns for an object set."""
    if isinstance(asset, RigidObjectSet) and asset.variant_indices_by_env:
        return asset.get_bounding_box_per_env(num_envs=1)
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


def _placement_summary(layout: PlacementResult | None, seed: int) -> dict[str, Any]:
    """Whether this sample is a layout the sim would accept, and which checks decided that."""
    if layout is None:
        return {"seed": seed, "solved_assets": 0}
    return {
        "seed": seed,
        "solved_assets": len(layout.positions),
        "success": layout.success,
        "final_loss": layout.final_loss,
        "attempts": layout.attempts,
        "checks": {str(check): passed for check, passed in layout.validation_results.validation_results.items()},
        "failed_checks": layout.validation_results.get_failed_validation_check_names,
    }


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


def _format_text(dump: dict[str, Any]) -> str:
    """Render the dump as a terminal-readable summary."""
    placement = dump["placement"]
    if "success" not in placement:
        status = "nothing to solve"
    elif placement["success"]:
        status = "valid layout"
    else:
        status = "INVALID layout, failed: " + ", ".join(placement["failed_checks"])
    lines = [f"{dump['env_name']}  (seed {placement['seed']}, {status})"]
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
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help=f"Placement seed (default: {DEFAULT_SEED})")
    parser.add_argument("--format", choices=("json", "text"), default="json", help="Output format (default: json)")
    parser.add_argument("--output", type=Path, help="Write to this file instead of stdout")
    parser.add_argument(
        "--payload",
        type=Path,
        help="Also pack the layout and its USD geometry here, for `arena-viewer`",
    )
    parser.add_argument(
        "--asset-cache",
        type=Path,
        help="Directory for downloaded USD assets (default: $ARENA_COMPOSER_ASSET_CACHE, else ~/.cache/arena-composer)",
    )
    args = parser.parse_args(argv)

    use_local_asset_cache(args.asset_cache)
    # Arena and the solver print progress to stdout; keep it off the machine-readable output.
    with contextlib.redirect_stdout(sys.stderr):
        dump = resolve_layout_from_yaml(args.spec, seed=args.seed)

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

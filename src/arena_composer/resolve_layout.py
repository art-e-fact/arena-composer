"""Dry-resolve an ArenaEnvGraphSpec for visualization — no SimulationApp.

Does not call ``to_arena_env()`` (that constructs tasks / Isaac Lab env cfgs).
Placement is one seeded sample of the layout pool, not “the” environment.
Local AABBs match Isaac Lab spawn size (USD root ``xformOp:scale`` is unbaked).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from isaaclab_arena.assets.registries import AssetRegistry
from isaaclab_arena.environment_spec.arena_env_graph_conversion_utils import (
    _attach_spatial_relations_to_assets,
    instantiate_assets_from_spec,
)
from isaaclab_arena.environment_spec.arena_env_graph_spec import ArenaEnvGraphSpec
from isaaclab_arena.relations.object_placer import ObjectPlacer
from isaaclab_arena.relations.object_placer_params import ObjectPlacerParams
from isaaclab_arena.relations.passive_collision_objects import get_passive_collision_objects
from isaaclab_arena.relations.placement_events import get_pose_from_layout
from isaaclab_arena.relations.placement_result import PlacementResult
from isaaclab_arena.utils.pose import Pose, PosePerEnv, PoseRange

DEFAULT_SEED = 42


def resolve_layout(spec: ArenaEnvGraphSpec, *, seed: int = DEFAULT_SEED) -> dict[str, Any]:
    """Instantiate assets, solve placement, and return a JSON-serializable dump."""
    assets = instantiate_assets_from_spec(spec, AssetRegistry())
    _attach_spatial_relations_to_assets(spec.relations, assets)

    placeable = [asset for asset in assets.values() if asset.get_relations()]
    result: PlacementResult | None = None
    if placeable:
        placer = ObjectPlacer(ObjectPlacerParams(placement_seed=seed))
        result = placer.place(
            placeable,
            num_envs=1,
            collision_objects=get_passive_collision_objects(assets.values()),
        )[0]

    dump: dict[str, Any] = {
        "env_name": spec.env_name,
        "placement_seed": seed,
        "assets": [
            _serialize_asset(node_id, assets[node_id], meta, result)
            for node_id, meta in _node_index(spec).items()
        ],
        "relations": [
            {"kind": rel.kind, "subject": rel.subject, "reference": rel.reference, "params": rel.params}
            for rel in spec.relations
        ],
    }
    if result is not None:
        dump.update(
            success=result.success,
            final_loss=result.final_loss,
            attempts=result.attempts,
            failed_checks=result.validation_results.get_failed_validation_check_names,
            validation_results=dict(result.validation_results.validation_results),
        )
    return dump


def resolve_layout_from_yaml(path: str | Path, *, seed: int = DEFAULT_SEED) -> dict[str, Any]:
    path = Path(path)
    dump = resolve_layout(ArenaEnvGraphSpec.from_yaml(path), seed=seed)
    dump["spec_path"] = str(path.resolve())
    return dump


def _node_index(spec: ArenaEnvGraphSpec) -> dict[str, dict[str, Any]]:
    """Spec node-id → role metadata, in dump order."""
    nodes: dict[str, dict[str, Any]] = {
        spec.embodiment.id: {"role": "embodiment", "registry_name": spec.embodiment.registry_name},
        spec.background.id: {"role": "background", "registry_name": spec.background.registry_name},
    }
    for obj in spec.objects:
        nodes[obj.id] = {"role": "object", "registry_name": obj.registry_name}
    for object_set in spec.object_sets or []:
        nodes[object_set.id] = {"role": "object_set", "members": object_set.members}
    for ref in spec.object_references or []:
        nodes[ref.id] = {"role": "object_reference", "parent_id": ref.parent_id}
    return nodes


def _serialize_asset(node_id: str, asset: Any, meta: dict[str, Any], layout: PlacementResult | None) -> dict[str, Any]:
    usd_path, scale = _usd_and_scale(asset)
    pose = _pose(asset, layout)
    bbox = asset.get_bounding_box()
    entry: dict[str, Any] = {
        "id": node_id,
        "role": meta["role"],
        "name": asset.name,
        "usd_path": usd_path,
        "scale": scale,
        "is_anchor": bool(getattr(asset, "is_anchor", False)),
        "position_xyz": list(pose.position_xyz),
        "rotation_xyzw": list(pose.rotation_xyzw),
        # Local AABB (object origin). Viewer applies pose; do not use get_world_bounding_box().
        "bbox_min": _xyz(bbox.min_point),
        "bbox_max": _xyz(bbox.max_point),
    }
    for key in ("registry_name", "members", "parent_id"):
        if key in meta:
            entry[key] = meta[key]
    prim_path = getattr(asset, "prim_path", None)
    if prim_path:
        entry["prim_path"] = prim_path
    if meta["role"] == "object_reference":
        entry["prim_path_in_parent_usd"] = getattr(asset, "prim_path_in_parent_usd", None)
    object_type = getattr(asset, "object_type", None)
    if object_type is not None:
        entry["object_type"] = getattr(object_type, "value", str(object_type))
    return entry


def _usd_and_scale(asset: Any) -> tuple[str | None, list[float]]:
    if hasattr(asset, "get_placement_geometry_source"):
        source = asset.get_placement_geometry_source()
        return str(source.usd_path), [float(c) for c in source.scale]
    parent = getattr(asset, "parent_asset", None)
    if parent is not None:
        usd = getattr(parent, "usd_path", None)
        scale = getattr(parent, "scale", (1.0, 1.0, 1.0))
        return (str(usd) if usd else None), [float(c) for c in scale]
    paths = getattr(asset, "object_usd_paths", None) or getattr(asset, "member_usd_paths", None)
    if paths:
        return str(paths[0]), [1.0, 1.0, 1.0]
    usd = getattr(asset, "usd_path", None) or None
    scale = getattr(asset, "scale", (1.0, 1.0, 1.0))
    return (str(usd) if usd else None), [float(c) for c in scale]


def _pose(asset: Any, layout: PlacementResult | None) -> Pose:
    if layout is not None and asset in layout.positions:
        return get_pose_from_layout(asset, layout)
    pose = asset.get_initial_pose()
    if pose is None:
        return Pose.identity()
    if isinstance(pose, PosePerEnv):
        return pose.poses[0]
    if isinstance(pose, PoseRange):
        return pose.get_midpoint()
    return pose


def _xyz(value: Any) -> list[float]:
    coords = value.tolist() if hasattr(value, "tolist") else list(value)
    if coords and isinstance(coords[0], list):
        coords = coords[0]
    return [float(c) for c in coords]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path, help="Path to an ArenaEnvGraphSpec YAML")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="Placement seed (default: 42)")
    parser.add_argument("--format", choices=("json", "yaml"), default="json")
    args = parser.parse_args(argv)

    dump = resolve_layout_from_yaml(args.spec, seed=args.seed)
    if args.format == "yaml":
        import yaml

        print(yaml.safe_dump(dump, sort_keys=False))
    else:
        print(json.dumps(dump, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

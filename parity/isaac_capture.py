"""Isaac Sim's half of the comparison: build a spec, reset once, report where everything is, render.

    python parity/isaac_capture.py --env_spec SPEC --cams CAMS.json --out DIR [--placement_seed N]

`compare.py` runs this in a process of its own: it starts Isaac Sim, which nothing else here does.
It builds the spec as Arena's own runners do, resets once and steps physics once more, so USD has
caught up with the reset. It writes `sim_state.json`, every asset's pose (and the robot's joints)
relative to the env's origin, and the cameras of CAMS.json placed at those poses
(`cameras.place`), then `isaac_<camera>.png` per camera, each a 45° pinhole camera rendered by RTX.
"""

import json
import math
import sys
from pathlib import Path

from isaaclab_arena.cli.isaaclab_arena_cli import get_isaaclab_arena_cli_parser
from isaaclab_arena.utils.isaaclab_utils.simulation_app import SimulationAppContext

#: RTX accumulates its path-traced samples over frames; fewer than this leaves visible noise.
FRAMES = 16


def main() -> None:
    parser = get_isaaclab_arena_cli_parser()
    parser.add_argument("--cams", required=True, help="Camera recipes, as compare.py writes them")
    parser.add_argument("--out", required=True)
    parser.add_argument("--size", type=int, default=768)
    parser.set_defaults(device="cpu", headless=True, enable_cameras=True)
    argv = sys.argv[1:]
    args, _ = parser.parse_known_args(argv)
    with SimulationAppContext(args):
        # Each of these imports Isaac Sim, so none can come before the app has started.
        import isaaclab.sim as sim_utils
        import numpy as np
        import torch
        from isaaclab.sensors.camera import CameraCfg
        from isaaclab_arena_environments.cli import (
            get_arena_builder_from_cli,
            get_isaaclab_arena_environments_cli_parser,
        )
        from PIL import Image
        from pxr import Gf, UsdGeom

        sys.path.insert(0, str(Path(__file__).parent))
        import cameras

        args, hydra = get_isaaclab_arena_environments_cli_parser(parser).parse_known_args(argv)
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        spec = json.loads(Path(args.cams).read_text())
        # The cameras are this script's own; an embodiment that carries none (kuka_allegro) refuses the flag.
        args.enable_cameras = False
        builder = get_arena_builder_from_cli(args, hydra_overrides=hydra)
        env_cfg, env_kwargs = builder.build_env_cfg()
        aperture = 20.955
        focal = aperture / 2 / math.tan(cameras.HALF_FOV)
        for i in range(len(spec["cams"])):
            camera = CameraCfg(
                prim_path=f"{{ENV_REGEX_NS}}/CompareCam{i}",
                update_period=0,
                height=args.size,
                width=args.size,
                data_types=["rgb"],
                spawn=sim_utils.PinholeCameraCfg(
                    focal_length=focal, horizontal_aperture=aperture, clipping_range=(0.01, 1.0e4)
                ),
            )
            setattr(env_cfg.scene, f"compare_cam_{i}", camera)
        env_cfg.recorders = {}
        env = builder.make_registered(env_cfg, env_kwargs)
        env.reset()
        sim = env.unwrapped
        sim.sim.step(render=False)
        sim.scene.update(sim.physics_dt)

        origin = sim.scene.env_origins[0].cpu().tolist()

        def pose(data) -> dict:
            p = data.root_pose_w.torch[0].cpu().tolist()
            return {"pos": [a - b for a, b in zip(p[:3], origin)], "quat_xyzw": p[3:]}

        state = {"origin": origin, "rigid": {}, "articulations": {}, "usd": {}}
        for name, rigid in sim.scene.rigid_objects.items():
            state["rigid"][name] = pose(rigid.data)
        for name, articulation in sim.scene.articulations.items():
            joints = dict(zip(articulation.joint_names, articulation.data.joint_pos.torch[0].cpu().tolist()))
            state["articulations"][name] = {**pose(articulation.data), "joints": joints}
        # The rest, the background among them, where the spawner put them, which is not always where
        # the composer did: a root rotation the USD authors is replaced by the asset's initial pose.
        env_root = sim_utils.get_current_stage().GetPrimAtPath("/World/envs/env_0")
        for prim in env_root.GetChildren() if env_root else []:
            if prim.IsA(UsdGeom.Xformable):
                xform = Gf.Transform(UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(0))
                t, q = xform.GetTranslation(), xform.GetRotation().GetQuat()
                position = [t[k] - origin[k] for k in range(3)]
                state["usd"][prim.GetName()] = {"pos": position, "quat_xyzw": [*q.GetImaginary(), q.GetReal()]}

        poses = {}
        for i, box in spec["boxes"].items():
            settled = (
                state["articulations"].get("robot")
                if box["role"] == "embodiment"
                else state["rigid"].get(box["name"]) or state["articulations"].get(box["name"])
            )
            poses[i] = (settled["pos"], settled["quat_xyzw"]) if settled else (box["pos"], box["quat"])
        state["cams"] = cameras.place(spec, poses)

        sensors = [sim.scene[f"compare_cam_{i}"] for i in range(len(state["cams"]))]
        for sensor, cam in zip(sensors, state["cams"]):
            eye, lookat = ([[c + o for c, o in zip(cam[key], origin)]] for key in ("eye", "lookat"))
            sensor.set_world_poses_from_view(
                torch.tensor(eye, dtype=torch.float32, device=sim.device),
                torch.tensor(lookat, dtype=torch.float32, device=sim.device),
            )
        for _ in range(FRAMES):
            sim.sim.render()
            for sensor in sensors:
                sensor.update(0.0, force_recompute=True)
        for sensor, cam in zip(sensors, state["cams"]):
            rgb = sensor.data.output["rgb"]
            rgb = getattr(rgb, "torch", rgb)[0].cpu().numpy()
            Image.fromarray(rgb[..., :3].astype(np.uint8)).save(out / f"isaac_{cam['name']}.png")
        (out / "sim_state.json").write_text(json.dumps(state, indent=1))
        print(f"[isaac_capture] {len(sensors)} cameras into {out}", flush=True)


if __name__ == "__main__":
    main()

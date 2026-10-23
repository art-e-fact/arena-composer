"""Where the comparison's cameras stand: framed on the composer's boxes, at the poses the sim settled.

Shared by both sides, so numpy is all it needs: `compare.py` writes the recipes from the composer's
solve, and `isaac_capture.py` places them inside Isaac Sim once physics has moved things, so a prop
that rolled is still in its close-up. The composer then draws from the cameras Isaac reports.
"""

import math

import numpy as np

HALF_FOV = math.radians(45) / 2
CLOSE_UPS = 12


def recipes(dump: dict) -> dict:
    """What to frame: the objects, the robot, both together, the background's own view (or the whole
    scene, for a background with none), and a close-up of each of the first `CLOSE_UPS` objects. Each
    fits boxes from ``dump["assets"]`` along a direction."""
    boxes = {
        e["id"]: {"name": e["name"], "role": e["role"], "lo": e["bbox_min"], "hi": e["bbox_max"],
                  "pos": e["position_xyz"], "quat": e["rotation_xyzw"]}
        for e in dump["assets"]
        if np.all(np.isfinite(e["bbox_min"])) and np.any(np.array(e["bbox_max"]) != np.array(e["bbox_min"]))
    }  # fmt: skip
    objects = [e["id"] for e in dump["assets"] if e["role"] in ("object", "object_set") and e["id"] in boxes]
    robot = [e["id"] for e in dump["assets"] if e["role"] == "embodiment" and e["id"] in boxes]
    cams = []
    if objects:
        cams.append({"name": "objects", "fit": objects, "dir": [0.45, -0.75, 0.85]})
    if robot:
        cams.append({"name": "robot", "fit": robot, "dir": [0.8, -0.6, 0.45]})
    if objects or robot:
        # The window's own three-quarter direction, in Arena's frame.
        cams.append({"name": "overview", "fit": objects + robot, "dir": [0.7, -1.0, 0.55]})
    if dump.get("viewer"):
        cams.append({"name": "background", **dump["viewer"]})
    elif boxes:
        cams.append({"name": "scene", "fit": list(boxes), "dir": [0.7, -1.0, 0.55]})
    for i in objects[:CLOSE_UPS]:
        cams.append({"name": f"obj_{i}", "fit": [i], "dir": [0.5, -0.8, 0.6], "min_radius": 0.06})
    return {"boxes": boxes, "cams": cams}


def place(spec: dict, poses: dict) -> list[dict]:
    """``spec``'s cameras as eye and look-at, each fitted to its boxes at ``poses``: id -> (pos, quat_xyzw)."""
    cams = []
    for cam in spec["cams"]:
        if "fit" not in cam:
            cams.append(cam)
            continue
        corners = [_world_box(spec["boxes"][i], *poses[i]) for i in cam["fit"]]
        lo, hi = np.min([c[0] for c in corners], 0), np.max([c[1] for c in corners], 0)
        centre, radius = (lo + hi) / 2, max(np.linalg.norm(hi - lo) / 2, cam.get("min_radius", 0.15))
        direction = np.array(cam["dir"], float) / np.linalg.norm(cam["dir"])
        eye = centre + direction * radius / math.sin(HALF_FOV) * 1.05
        cams.append({"name": cam["name"], "eye": eye.tolist(), "lookat": centre.tolist()})
    return cams


def _world_box(box: dict, pos, quat) -> tuple[np.ndarray, np.ndarray]:
    lo, hi = box["lo"], box["hi"]
    corners = np.array([[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])])
    x, y, z, w = quat
    rotation = np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ])  # fmt: skip
    points = corners @ rotation.T + np.array(pos)
    return points.min(0), points.max(0)

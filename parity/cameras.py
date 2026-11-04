"""Where the comparison's cameras stand: framed on the composer's boxes, at the poses the sim settled.

Shared by both sides, so numpy is all it needs: `compare.py` writes the recipes from the composer's
solve, and `isaac_capture.py` places them inside Isaac Sim once physics has moved things, so a prop
that rolled is still in its close-up, and a close-up another object blocks turns until it sees its
own. The composer then draws from the cameras Isaac reports.
"""

import math

import numpy as np

HALF_FOV = math.radians(45) / 2
CLOSE_UPS = 12
#: The turns about the vertical a blocked close-up tries, in degrees, in order: 0 first, so a clear one stays put.
TURNS = (0, 45, -45, 90, -90, 135, -135, 180)


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
    """``spec``'s cameras as eye and look-at, each fitted to its boxes at ``poses``: id -> (pos, quat_xyzw).

    A close-up another object blocks turns about the vertical, by the first of `TURNS` whose eye
    stands outside every other object's box and whose sight line, up to where it enters its own
    object's box, crosses none of them. One no turn clears keeps its direction. The background's
    box holds everything and the robot's is mostly air, so neither blocks."""
    world = {i: _world_box(box, *poses[i]) for i, box in spec["boxes"].items()}
    cams = []
    for cam in spec["cams"]:
        if "fit" not in cam:
            cams.append(cam)
            continue
        lo, hi = np.min([world[i][0] for i in cam["fit"]], 0), np.max([world[i][1] for i in cam["fit"]], 0)
        centre, radius = (lo + hi) / 2, max(np.linalg.norm(hi - lo) / 2, cam.get("min_radius", 0.15))
        direction = np.array(cam["dir"], float) / np.linalg.norm(cam["dir"])
        distance = radius / math.sin(HALF_FOV) * 1.05
        eye = centre + direction * distance
        if cam["name"].startswith("obj_"):
            others = [world[i] for i, box in spec["boxes"].items()
                      if i not in cam["fit"] and box["role"] in ("object", "object_set")]  # fmt: skip
            turned = (centre + _turned(direction, degrees) * distance for degrees in TURNS)
            eye = next((e for e in turned if _sees(e, lo, hi, others)), eye)
        cams.append({"name": cam["name"], "eye": eye.tolist(), "lookat": centre.tolist()})
    return cams


def _sees(eye: np.ndarray, lo: np.ndarray, hi: np.ndarray, others: list) -> bool:
    """Whether ``eye`` sees the box ``lo``-``hi`` past ``others``: it stands outside each, and its sight
    line to the box's centre, up to where it enters the box, crosses none."""
    near = eye + ((lo + hi) / 2 - eye) * _enters(lo, hi, eye, (lo + hi) / 2)
    return all(_enters(*box, eye, near) is None for box in others)


def _turned(v: np.ndarray, degrees: float) -> np.ndarray:
    """``v`` turned about Arena's vertical, Z."""
    c, s = math.cos(math.radians(degrees)), math.sin(math.radians(degrees))
    return np.array([c * v[0] - s * v[1], s * v[0] + c * v[1], v[2]])


def _enters(lo, hi, a, b) -> float | None:
    """How far along ``a`` → ``b``, 0-1, the segment enters the box ``lo``-``hi``; None if it misses."""
    near, far = 0.0, 1.0
    for k in range(3):
        if b[k] == a[k]:
            if not lo[k] <= a[k] <= hi[k]:
                return None
            continue
        u, v = (lo[k] - a[k]) / (b[k] - a[k]), (hi[k] - a[k]) / (b[k] - a[k])
        near, far = max(near, min(u, v)), min(far, max(u, v))
    return near if near <= far else None


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

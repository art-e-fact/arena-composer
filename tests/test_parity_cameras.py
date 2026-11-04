"""The Isaac Sim comparison's close-ups turn away from an object in the way, and only then."""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parents[1] / "parity"))
import cameras  # noqa: E402

DEFAULT = np.array([0.5, -0.8, 0.6]) / np.linalg.norm([0.5, -0.8, 0.6])


def close_up(*assets: tuple) -> np.ndarray:
    """The direction the first asset's close-up looks from, ``assets`` being (role, bbox_min, bbox_max)."""
    dump = {"assets": [{"id": str(n), "name": str(n), "role": role, "bbox_min": lo, "bbox_max": hi,
                        "position_xyz": [0, 0, 0], "rotation_xyzw": [0, 0, 0, 1]}
                       for n, (role, lo, hi) in enumerate(assets)]}  # fmt: skip
    spec = cameras.recipes(dump)
    cam = next(c for c in cameras.place(spec, {i: (b["pos"], b["quat"]) for i, b in spec["boxes"].items()})
               if c["name"] == "obj_0")  # fmt: skip
    eye = np.subtract(cam["eye"], cam["lookat"])
    return eye / np.linalg.norm(eye)


def test_a_clear_close_up_keeps_its_direction():
    # Another object, out of the way.
    direction = close_up(("object", [-0.05] * 3, [0.05] * 3), ("object", [-0.5, 0.2, 0], [-0.3, 0.4, 0.2]))
    assert np.allclose(direction, DEFAULT)


def test_a_blocked_close_up_turns():
    # A crate on the default direction, holding the eye. The background's and the robot's boxes hold
    # every eye, and block none.
    direction = close_up(("object", [-0.05] * 3, [0.05] * 3), ("object", [0.06, -0.3, 0.06], [0.2, -0.06, 0.2]),
                         ("background", [-5] * 3, [5] * 3), ("embodiment", [-1] * 3, [1] * 3))  # fmt: skip
    assert np.isclose(direction[2], DEFAULT[2]), "it turns about the vertical"
    cos = np.dot(direction[:2], DEFAULT[:2]) / np.dot(DEFAULT[:2], DEFAULT[:2])
    assert np.isclose(np.degrees(np.arccos(cos)), 45), "by the first turn that clears the crate"

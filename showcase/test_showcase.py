"""A gallery of environments, drawn from every view `arena-render` has, for a
person to look over.

Not part of `pytest`, which collects `tests/` only. Run it on its own, in Arena's
venv on a machine that can draw (a GPU, or lavapipe as `arena-render --help`
says):

    pytest showcase

Each case is one spec, solved and drawn as `<case>-<view>.png`. The pictures, the
specs the themes generated and `junit.xml` go to $ARENA_SHOWCASE_DIR,
/tmp/arena-showcase by default. In the XML a case fails when its spec does not
build or draw; one that draws carries its spec, its images' file names and its
placement verdict as properties, since an invalid layout is drawn all the same.
`artefacts.yaml` runs this as an Artefacts job, into the run's upload.

$ARENA_SHOWCASE_SEED (default 42) draws the themes' props and places every
layout: another seed is another gallery.

Two kinds of case:

- `SPECS`: Arena's own specs, a few of each family, chosen for range.
- `themes/*.yaml`: specs for the robots, backgrounds and props no shipped spec
  uses. Each is a spec but for a `draw` key, whose props are picked from
  SimReady categories by the seed (see `generate`).
"""

from __future__ import annotations

import importlib.util
import os
import random
import re
import socket
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from arena_composer import catalog, render
from arena_composer.cli import DEFAULT_SEED
from arena_composer.usd_assets import use_local_asset_cache

OUT = Path(os.environ.get("ARENA_SHOWCASE_DIR", "/tmp/arena-showcase"))
SEED = int(os.environ.get("ARENA_SHOWCASE_SEED", DEFAULT_SEED))
#: Larger than `arena-render`'s default: these are for a person, not a model.
SIZE = 1024

#: Arena's own specs, relative to `isaaclab_arena_environments`. Not imported:
#: its `cli` module imports Isaac Sim.
ARENA = Path(importlib.util.find_spec("isaaclab_arena_environments").submodule_search_locations[0])
SPECS = [
    "robolab/tasks/clutter_pumpkin.yaml",
    "robolab/tasks/black_items_in_bin.yaml",
    "maple_table_top/simready_droid_pick_place_cans_hammer_maple_table.yaml",
    "isaac_cap/gear_insertion/gear_medium.yaml",
    "isaac_cap/syringe_sort/environments/syringe_cluttered.yaml",
    "kitchen_bench/kitchen_bench_lightwheel_place_bananas_in_wooden_bowl_u_shaped_with_island_farmhouse2.yaml",
    "kitchen_bench/kitchen_bench_lightwheel_place_lime_in_wooden_bowl_l_shaped_scandinavian.yaml",
    "kitchen_bench/kitchen_bench_lightwheel_open_oven_g_shaped_large_scandinavian.yaml",
    "kitchen_bench/kitchen_bench_replicator_l_shape_mustard_bowl.yaml",
    "kitchen_bench/kitchen_bench_replicator_l_island_banana_bowl.yaml",
]

THEMES = sorted((Path(__file__).parent / "themes").glob("*.yaml"))

catalog.load_extensions(())
use_local_asset_cache(None)


def generate(theme: Path, seed: int) -> Path:
    """Write `theme` out as a spec, its `draw` key replaced by the props it names.

    `draw` takes `from`, SimReady category prefixes such as
    `Industrial/Tools/Hand`; `count`, how many props; and `onto`, the node they
    stand on. Each prop is an object with an `on` relation to that node. A theme
    with no `task` gets a pick-and-place of the first prop onto the second, so
    it draws two at least.
    """
    from arena_composer.resolve_layout import check_usd_params
    from arena_composer.spec_edit import SpecError

    spec = yaml.safe_load(theme.read_text())
    draw = spec.pop("draw")
    pool = [asset for asset in catalog.remote() if asset.category.startswith(tuple(draw["from"]))]
    random.Random(f"{theme.stem}:{seed}").shuffle(pool)

    objects, relations = spec.setdefault("objects", []), spec.setdefault("relations", [])
    taken = {entry["id"] for entry in objects}
    drawn = []
    for asset in pool:
        if len(drawn) == draw["count"]:
            break
        entry = {"id": _identifier(asset.name), "registry_name": asset.registry_name, "params": dict(asset.params)}
        if entry["id"] in taken:
            continue
        try:
            # Most of SimReady is several rigid bodies, which the sim cannot
            # spawn as one object, and only the file says which. Downloads it.
            check_usd_params([SimpleNamespace(**entry)])
        except SpecError:
            continue
        objects.append(entry)
        relations.append({"kind": "on", "subject": entry["id"], "reference": draw["onto"], "params": {}})
        taken.add(entry["id"])
        drawn.append(entry["id"])
    if len(drawn) < min(2, draw["count"]):
        raise ValueError(f"{theme.name}: only {len(drawn)} usable prop(s) under {draw['from']}")

    spec.setdefault(
        "task",
        {
            "composition": "atomic",
            "description": f"Pick up the {drawn[0]} and place it on the {drawn[1]}.",
            "subtasks": [
                {
                    "kind": "PickAndPlaceTask",
                    "params": {
                        "pick_up_object": drawn[0],
                        "destination_location": drawn[1],
                        "background_scene": spec["background"]["id"],
                    },
                }
            ],
        },
    )
    path = OUT / "specs" / theme.name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(spec, sort_keys=False))
    return path


def _identifier(name: str) -> str:
    """`Groove_Joint_Pliers_B01` → `groove_joint_pliers_b01`."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


@pytest.mark.parametrize("spec", [ARENA / spec for spec in SPECS] + THEMES, ids=lambda spec: spec.stem)
def test_draw(spec: Path, record_property):
    from arena_composer.payload import encode
    from arena_composer.resolve_layout import summary

    shots = {view: OUT / f"{spec.stem}-{view}.png" for view in render.VIEWS}
    # The last run's pictures of a case that now fails would pass for this run's.
    for path in shots.values():
        path.unlink(missing_ok=True)
    # Paths in the XML are relative, so they still name the files once the
    # directory is uploaded or moved.
    if spec in THEMES:
        spec = generate(spec, SEED)
        record_property("spec", str(spec.relative_to(OUT)))
    else:
        record_property("spec", f"{ARENA.name}/{spec.relative_to(ARENA)}")

    dump = _resolve(spec)
    record_property("placement", summary(dump))
    render.render(encode(dump, remote=False), shots, SIZE)
    for view, path in shots.items():
        record_property(view, path.name)


def _resolve(spec: Path) -> dict:
    """Solve `spec`'s layout, and once more a minute after a network error.

    A Lightwheel kitchen asks api.lightwheel.net for its floorplan on every
    solve, cached or not, and that name does not always resolve.
    """
    from arena_composer.resolve_layout import resolve_layout_from_yaml

    try:
        return resolve_layout_from_yaml(spec, seed=SEED)
    except Exception as error:
        if not _network(error):
            raise
    time.sleep(60)
    return resolve_layout_from_yaml(spec, seed=SEED)


def _network(error: BaseException | None) -> bool:
    """Whether a lost connection or an unresolved name is behind `error`,
    however many wrappers the client put around it."""
    while error is not None:
        if isinstance(error, (ConnectionError, TimeoutError, socket.gaierror)):
            return True
        error = error.__cause__ or error.__context__
    return False

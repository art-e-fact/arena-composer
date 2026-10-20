"""The README's picture of the showcase: a few of its cases in one image, each
drawn from a camera placed for a person to look at rather than from the views
the gallery checks.

Run it as the gallery runs, in Arena's venv on a machine that can draw:

    python showcase/collage.py

It writes `showcase.png` to $ARENA_SHOWCASE_DIR (/tmp/arena-showcase by
default), drawing the layouts `pytest showcase` draws: the same specs, props
and seed. `docs/publish.sh` puts it on the README, under `<!-- showcase.png -->`.
"""

from __future__ import annotations

from PIL import Image

import test_showcase as showcase
from arena_composer import render
from arena_composer.payload import encode

#: A case by its spec's stem, and the camera that draws it: an eye and the point
#: it looks at, in Arena's frame, or "top" for the plan the gallery draws.
TILES = [
    ("kitchen_island_cookware", ((2.2, -4.0, 2.0), (2.95, -2.0, 1.05))),
    ("kitchen_pantry_galbot", ((4.0, -2.2, 1.8), (2.15, -0.8, 1.0))),
    (
        "kitchen_bench_lightwheel_place_bananas_in_wooden_bowl_u_shaped_with_island_farmhouse2",
        ((3.5, -4.2, 1.8), (2.3, -2.4, 1.0)),
    ),
    # Looking down, or the packing table's ground plane ends at a horizon.
    ("tabletop_workshop", ((-0.5, -1.6, 1.35), (0.8, 0.25, 0.12))),
    ("kitchen_bench_replicator_l_shape_mustard_bowl", ((1.3, 0.0, 1.7), (-0.05, 1.5, 1.0))),
    ("kitchen_gshaped_humanoid", "top"),
]
COLUMNS = 3
#: A tile's side. Each is drawn at twice that and scaled down, which smooths the
#: edges more than the renderer's own multisampling does.
TILE = 600
#: Between tiles, left transparent for the page behind to show through.
GAP = 12


def main() -> None:
    cases = {spec.stem: spec for spec in [showcase.ARENA / spec for spec in showcase.SPECS] + showcase.THEMES}
    pictures = []
    for stem, camera in TILES:
        spec = cases[stem]
        if spec in showcase.THEMES:
            spec = showcase.generate(spec, showcase.SEED)
        dump = showcase._resolve(spec)
        view = "top" if camera == "top" else "default"
        if view == "default":
            # The perspective view stands where the background suggests.
            dump["viewer"] = {"eye": list(camera[0]), "lookat": list(camera[1])}
        path = showcase.OUT / "collage" / f"{stem}-{view}.png"
        render.render(encode(dump, remote=False), {view: path}, 2 * TILE)
        pictures.append(Image.open(path).convert("RGBA").resize((TILE, TILE), Image.Resampling.LANCZOS))

    rows = -(-len(pictures) // COLUMNS)
    sheet = Image.new("RGBA", (COLUMNS * TILE + (COLUMNS - 1) * GAP, rows * TILE + (rows - 1) * GAP))
    for i, picture in enumerate(pictures):
        sheet.paste(picture, ((i % COLUMNS) * (TILE + GAP), (i // COLUMNS) * (TILE + GAP)))
    out = showcase.OUT / "showcase.png"
    sheet.save(out, optimize=True)
    print(out)


if __name__ == "__main__":
    main()

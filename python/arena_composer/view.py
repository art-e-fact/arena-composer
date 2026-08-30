"""Resolve a layout and open the Bevy viewer on it.

The split is the whole point: Python owns Arena — registries, USD reads, the
placement solver — and hands the result over as one JSON document. The viewer
never imports Arena, and the same JSON runs through ``arena-viewer`` on its own
(see ``--output``), so the Bevy side can be iterated on without paying for a
re-import and re-solve every time.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path

from arena_composer.resolve_layout import DEFAULT_SEED, resolve_layout_from_yaml
from arena_composer.usd_assets import use_local_asset_cache


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("spec", type=Path, help="Path to an ArenaEnvGraphSpec YAML")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help=f"Placement seed (default: {DEFAULT_SEED})")
    parser.add_argument("--output", type=Path, help="Also write the resolved scene here, for `arena-viewer`")
    parser.add_argument(
        "--asset-cache",
        type=Path,
        help="Directory for downloaded USD assets (default: $ARENA_COMPOSER_ASSET_CACHE, else ~/.cache/arena-composer)",
    )
    args = parser.parse_args(argv)

    use_local_asset_cache(args.asset_cache)
    # Arena and the solver print progress to stdout; keep it off stdout so a
    # caller can still pipe this command's own output somewhere.
    with contextlib.redirect_stdout(sys.stderr):
        scene = resolve_layout_from_yaml(args.spec, seed=args.seed)

    scene_json = json.dumps(scene)
    if args.output:
        args.output.write_text(scene_json + "\n", encoding="utf-8")

    from arena_composer import _core

    # Blocks until the window is closed.
    _core.run_viewer(scene_json)
    return 0


if __name__ == "__main__":
    sys.exit(main())

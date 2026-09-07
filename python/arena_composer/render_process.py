"""The render child process: draws one scene from a few angles, then exits.

:mod:`arena_composer.render` runs this as ``python -m
arena_composer.render_process`` so the render owns a process of its own. Bevy
assumes one app per process — task pools, asset server and the wgpu device are
effectively global — so a second render in the same process would build a second
device rather than reuse the first. A child also keeps a driver hang or a GPU
reset away from whatever asked for the picture.

The packed scene arrives on stdin; unlike the viewer's stream it is one payload
and nothing follows it, so end-of-file is the whole framing.
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "shots",
        nargs="+",
        metavar="VIEW=PATH",
        help="A view to draw and the PNG to write it to (default, top, front or side)",
    )
    parser.add_argument("--size", type=int, default=768, help="Image size in pixels, square (default: 768)")
    args = parser.parse_args(argv)

    shots = []
    for shot in args.shots:
        view, separator, path = shot.partition("=")
        if not separator or not path:
            parser.error(f"expected VIEW=PATH, got {shot!r}")
        shots.append((view, path))

    # Imported here so `--help` does not pay for loading the extension module.
    from arena_composer import _core

    _core.render_scene(sys.stdin.buffer.read(), shots, args.size, args.size)
    return 0


if __name__ == "__main__":
    sys.exit(main())

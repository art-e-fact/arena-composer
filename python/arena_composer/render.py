"""`arena-render`: draw specs headless, a PNG per view. And one-shot offscreen
renders of a packed scene, which it and the MCP server's `render_views` share.

The other half of :mod:`arena_composer.view`: same payload, same Bevy code, but
no window, no watcher and no display server — a few PNGs and a process that is
gone a second later. That is what lets a long-lived caller (the MCP server) show
a layout without holding a GPU open between calls.

Drawing happens in a child process (:mod:`arena_composer.render_process`) for
the reasons given there.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import subprocess
import sys
from collections import Counter
from collections.abc import Mapping
from pathlib import Path

from arena_composer import cli, render_process, spec_edit

#: View names the renderer understands, matching `arena_composer::layout::View`.
#:
#: The three axis views are orthographic. What they are asked is "does the
#: banana rest on the table or float above it" and "do these two overlap", and
#: under perspective both answers depend on where in the frame the object sits.
VIEWS = ("default", "top", "front", "side")

#: Square, and modest: an image costs the model that reads it roughly a
#: thousand tokens, and more pixels do not make a gap easier to see.
DEFAULT_SIZE = 768

#: Bevy logs its adapter, its diagnostics and every screenshot it writes. Only
#: the tail is worth quoting back when a render fails, and only the first few
#: errors when it draws the wrong thing.
_ERROR_LINES = 20

#: `arena-render --help`'s closing words.
_EPILOG = """\
views:
  default  perspective, framed the way arena-view's window opens
  top      orthographic from above: placement in XY, overlaps
  front    orthographic from Arena -Y: heights, gaps, floating objects
  side     orthographic from Arena +X: the same, along the other axis

Each spec's PNGs are <out-dir>/<spec>-<view>.png, <spec> being its file name
less the extension, and their paths are printed one per line. A layout that fails
placement is drawn all the same, with the checks it failed on stderr. A spec
that does not build is skipped with its reasons on stderr, the others are
drawn, and the exit status is 1.

Needs a Vulkan driver, not a display: on a machine with no GPU, Mesa's lavapipe
draws through VK_ICD_FILENAMES=/usr/share/vulkan/icd.d/lvp_icd.json.
"""


def render(payload: bytes, shots: Mapping[str, Path], size: int = DEFAULT_SIZE) -> None:
    """Draw `payload` from each view in `shots`, writing it to the PNG the view
    maps to.

    Blocks for as long as the render takes — a second or so, nearly all of it
    Bevy starting up, which is why the views are drawn together rather than one
    call each.
    """
    if not shots:
        raise ValueError("no views to render")
    if unknown := [view for view in shots if view not in VIEWS]:
        raise ValueError(f"unknown view(s) {unknown}, expected any of {list(VIEWS)}")

    for path in shots.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        # A render that fails after a previous one wrote here would otherwise
        # pass its stale image off as the current layout.
        path.unlink(missing_ok=True)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            render_process.__name__,
            "--size",
            str(size),
            *(f"{view}={path}" for view, path in shots.items()),
        ],
        input=payload,
        capture_output=True,
        check=False,
        # Bevy colours its log unless this is set, and the reader here is a
        # pipe, not a terminal.
        env={**os.environ, "NO_COLOR": "1"},
    )
    stderr = result.stderr.decode(errors="replace")

    failure = None
    if missing := [path.name for path in shots.values() if not path.exists()]:
        tail = "\n".join(stderr.splitlines()[-_ERROR_LINES:])
        failure = f"render wrote no {', '.join(missing)} (exit {result.returncode})\n{tail}"
    # Bevy draws the rest of the scene around what it could not load and writes
    # the picture anyway, so a failure at this point looks like a good render of
    # a scene missing an object. Returning that image would answer "did my edit
    # land" with a no that isn't true.
    elif errors := logged_errors(stderr):
        failure = "render logged {} error(s):\n{}".format(len(errors), "\n".join(errors[:_ERROR_LINES]))
    if failure:
        # Whatever a failed render did write goes too: left on disk, it would
        # answer for the layout to whoever looks there next.
        for path in shots.values():
            path.unlink(missing_ok=True)
        raise RuntimeError(failure)


def logged_errors(stderr: str) -> list[str]:
    """The ERROR lines out of a render's log.

    Bevy's format puts the level second, after the timestamp; matching the field
    rather than the text keeps a message that merely mentions an error out.
    """
    return [line for line in stderr.splitlines() if line.split(maxsplit=2)[1:2] == ["ERROR"]]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Solve each spec's layout and draw it headless, a PNG per view.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "specs", nargs="+", type=Path, metavar="SPEC", help="A task YAML; several share one import of Arena"
    )
    parser.add_argument(
        "--view",
        dest="views",
        action="append",
        choices=VIEWS,
        metavar="VIEW",
        help=f"A view to draw: {', '.join(VIEWS)}, described below; repeatable (default: default)",
    )
    parser.add_argument(
        "--size",
        type=int,
        default=DEFAULT_SIZE,
        metavar="PIXELS",
        help=f"Image size in pixels, square (default: {DEFAULT_SIZE})",
    )
    parser.add_argument(
        "-o",
        "--out-dir",
        type=Path,
        default=Path("."),
        metavar="DIR",
        help="Where the PNGs go (default: the working directory)",
    )
    cli.add_spec_options(parser)
    args = parser.parse_args(argv)

    # Everything an argument can get wrong is refused here, in milliseconds,
    # rather than after the seconds Arena takes to import.
    if args.size < 1:
        parser.error(f"--size must be at least 1, got {args.size}")
    # One spec named twice — by a glob and by hand, or by two spellings of its
    # path — is drawn once, as first named.
    first: dict[Path, Path] = {}
    for spec in args.specs:
        first.setdefault(spec.resolve(), spec)
    args.specs = list(first.values())
    for spec in args.specs:
        if not spec.is_file():
            parser.error(f"no spec at {spec}")
        try:
            spec_edit.check_entry(spec)
        except ValueError as error:
            parser.error(str(error))
    if clashes := sorted(name for name, count in Counter(spec.stem for spec in args.specs).items() if count > 1):
        parser.error(f"more than one spec is named {', '.join(clashes)}; render them to separate --out-dir")

    views = list(dict.fromkeys(args.views or ["default"]))
    shots = {spec: {view: args.out_dir / f"{spec.stem}-{view}.png" for view in views} for spec in args.specs}
    # Made now, so a path that is no directory is refused before Arena is
    # imported rather than by the first render.
    try:
        args.out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        parser.error(f"cannot write to --out-dir {args.out_dir}: {error}")

    cli.apply_spec_options(parser, args)
    # Imported here: Arena takes seconds to import, and --help and the checks
    # above have no use for it. The MCP server imports this module too.
    from arena_composer.payload import encode
    from arena_composer.resolve_layout import resolve_layout_from_yaml, summary

    failed = 0
    for spec, paths in shots.items():
        try:
            # Arena and the solver print progress to stdout, which is the
            # list of pictures.
            with contextlib.redirect_stdout(sys.stderr):
                dump = resolve_layout_from_yaml(spec, seed=args.seed)
                payload = encode(dump, remote=False)
            print(f"{spec}: {summary(dump)}", file=sys.stderr)
            render(payload, paths, args.size)
        except Exception as error:
            failed += 1
            # The last run's pictures of it go too, or a glob over the
            # directory would take them for this run's.
            for path in paths.values():
                path.unlink(missing_ok=True)
            for line in spec_edit.reasons(error):
                print(f"{spec}: {line}", file=sys.stderr)
            continue
        print(*paths.values(), sep="\n", flush=True)
    if failed:
        print(f"{parser.prog}: {failed} of {len(shots)} spec(s) not drawn", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

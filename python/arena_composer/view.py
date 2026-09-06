"""Resolve a layout and open the Bevy viewer on it.

The split is the whole point: Python owns everything that needs OpenUSD — the
registries, the placement solver, and reading the assets' geometry — and hands
the result over as one packed payload. The viewer never imports Arena and never
opens a USD; the same payload runs through ``arena-viewer`` on its own (see
``--payload``), so the Bevy side can be iterated on without paying for a
re-import, a re-solve and a re-read every time.

Drawing happens in a child process (:mod:`arena_composer.viewer_process`), not
here: Bevy's event loop wants the main thread and never gives it back, so a
viewer in this process would leave nothing to run beside it. As a child it gets
that main thread, this one stays an ordinary asyncio program with a task per
concern, and Arena stays imported across a viewer restart or crash.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
from pathlib import Path

from arena_composer import viewer_process
from arena_composer.payload import encode
from arena_composer.resolve_layout import DEFAULT_SEED, resolve_layout_from_yaml
from arena_composer.usd_assets import use_local_asset_cache

# The spec is a single file, so it is polled rather than watched through
# inotify: one stat twice a second costs nothing and adds no dependency.
WATCH_INTERVAL = 0.5


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("spec", type=Path, help="Path to an ArenaEnvGraphSpec YAML")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help=f"Placement seed (default: {DEFAULT_SEED})")
    parser.add_argument("--payload", type=Path, help="Also write the packed scene here, for `arena-viewer`")
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
        payload = encode(resolve_layout_from_yaml(args.spec, seed=args.seed))

    if args.payload:
        args.payload.write_bytes(payload)

    return asyncio.run(_view(args.spec, payload))


async def _view(spec: Path, payload: bytes) -> int:
    """Run the viewer with the spec watcher alongside it; the window ends both."""
    watcher = asyncio.create_task(_watch(spec))
    try:
        return await _run_viewer(payload)
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher


async def _run_viewer(payload: bytes) -> int:
    """Draw `payload` in a child process; return its exit status."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        viewer_process.__name__,
        stdin=asyncio.subprocess.PIPE,
    )
    try:
        assert proc.stdin is not None
        proc.stdin.write(viewer_process.pack_frame(payload))
        await proc.stdin.drain()
        # Left open on purpose: later scenes go down the same pipe.
        return await proc.wait()
    finally:
        # Reached on Ctrl-C or a failure beside us, where the window would
        # otherwise outlive the process that owns it.
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()


async def _watch(spec: Path, interval: float = WATCH_INTERVAL) -> None:
    """Report every write to `spec` until cancelled. Nothing reloads on it yet."""
    stamp = _mtime(spec)
    while True:
        await asyncio.sleep(interval)
        if (current := _mtime(spec)) != stamp:
            stamp = current
            print(f"{spec}: changed", file=sys.stderr)


def _mtime(path: Path) -> int | None:
    """`path`'s modification time, or None while it is missing — an editor
    saving a spec replaces the file, so it briefly is."""
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return None


if __name__ == "__main__":
    sys.exit(main())

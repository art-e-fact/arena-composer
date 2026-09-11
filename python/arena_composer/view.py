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
concern, and Arena stays imported between resolves — which is what makes a
reload seconds rather than a restart.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import sys
from pathlib import Path

import msgpack
import yaml

from arena_composer import spec_edit, viewer_process
from arena_composer.payload import encode
from arena_composer.resolve_layout import DEFAULT_SEED, resolve_layout_from_yaml
from arena_composer.usd_assets import use_local_asset_cache

# A spec is one or two small files, so they are polled rather than watched
# through inotify: two stats twice a second cost nothing and add no dependency.
WATCH_INTERVAL = 0.5
# An editor writes a file in bursts, and a spec caught mid-write does not parse.
# Resolve once it has held still this long.
SETTLE = 0.3


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
    payload = _resolve(args.spec, args.seed)

    if args.payload:
        args.payload.write_bytes(payload)

    return asyncio.run(_view(args.spec, args.seed, payload))


def _resolve(spec: Path, seed: int) -> bytes:
    """Resolve `spec` into a packed scene. Seconds of work."""
    # Arena and the solver print progress to stdout; keep it off stdout so a
    # caller can still pipe this command's own output somewhere.
    with contextlib.redirect_stdout(sys.stderr):
        return encode(resolve_layout_from_yaml(spec, seed=seed))


async def _view(spec: Path, seed: int, payload: bytes) -> int:
    """Draw `payload`, reloading it from `spec` until the window closes."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        viewer_process.__name__,
        stdin=asyncio.subprocess.PIPE,
        # The editor's edits come back this way. Bevy logs to stderr, so
        # stdout carries nothing else — see `src/wire.rs`.
        stdout=asyncio.subprocess.PIPE,
    )

    def stop() -> None:
        """Close the window. Safe once it has closed itself."""
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()

    # Signalling this process alone would strand the window: Arena installs a
    # SIGINT handler here, so asyncio never sees a KeyboardInterrupt, and a
    # default SIGTERM would kill us before `finally` runs. A terminal's Ctrl-C
    # reaches the child too; a `kill` at this process does not.
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop)

    await _send(proc, payload)
    running = [
        asyncio.create_task(_reload_on_change(spec, seed, proc)),
        asyncio.create_task(_apply_edits(spec, seed, proc)),
    ]
    try:
        return await proc.wait()
    finally:
        for task in running:
            task.cancel()
        for task in running:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        stop()


async def _send(proc: asyncio.subprocess.Process, payload: bytes) -> None:
    """Hand a packed scene to the viewer. The pipe stays open for the next."""
    assert proc.stdin is not None
    proc.stdin.write(viewer_process.pack_scene(payload))
    await proc.stdin.drain()


async def _apply_edits(spec: Path, seed: int, proc: asyncio.subprocess.Process) -> None:
    """Apply the edits the editor sends, and draw what they produce.

    The channel only answers in scenes: an edit that lands is answered by the
    spec redrawn, and one that does not by the errors alone. Writing goes
    through :mod:`arena_composer.spec_edit`, which owns which of the two files
    a key lives in and validates before either is touched, so a refused edit
    leaves the spec exactly as the editor is still drawing it.
    """
    assert proc.stdout is not None
    while (edit := await _read_edit(proc.stdout)) is not None:
        result = await asyncio.to_thread(
            spec_edit.apply, spec, edit.get("op", ""), list(edit.get("path", [])), edit.get("value")
        )
        if proc.returncode is not None:
            return  # the window closed while we were writing

        if not result.ok:
            # No re-resolve: the spec did not change, and reading every asset's
            # USD again to say so would cost seconds. The viewer keeps the
            # scene it has and shows why the edit was refused.
            await _send(proc, msgpack.packb({"errors": result.errors}, use_bin_type=True))
            print(f"{spec}: refused: {'; '.join(result.errors)}", file=sys.stderr)
            continue

        # `_reload_on_change` would catch this too, but only after its poll and
        # settle; an edit the user just made should redraw now.
        await _send(proc, await asyncio.to_thread(_resolve, spec, seed))
        print(f"{spec}: {'; '.join(result.changed)}", file=sys.stderr)


async def _read_edit(stream: asyncio.StreamReader) -> dict | None:
    """One edit off the viewer's stdout, or None once it has ended."""
    try:
        header = await stream.readexactly(viewer_process.HEADER)
        return msgpack.unpackb(await stream.readexactly(int.from_bytes(header, "big")))
    except asyncio.IncompleteReadError:
        return None


async def _reload_on_change(spec: Path, seed: int, proc: asyncio.subprocess.Process) -> None:
    """Re-resolve `spec` on every write to it, or to the scene it includes, and
    draw what comes out."""
    stamps = _watched(spec)
    while True:
        await asyncio.sleep(WATCH_INTERVAL)
        if _watched(spec) == stamps:
            continue
        await asyncio.sleep(SETTLE)
        stamps = _watched(spec)

        try:
            # Off this thread: a resolve takes seconds, and the loop has to
            # stay answerable for all of them — not least to the window
            # closing under us.
            payload = await asyncio.to_thread(_resolve, spec, seed)
        except Exception as error:
            # Anything Arena raises on a half-written spec. The viewer keeps
            # drawing the last scene that did resolve.
            print(f"{spec}: {type(error).__name__}: {error}", file=sys.stderr)
            continue

        if proc.returncode is not None:
            return  # the window closed while we were resolving
        await _send(proc, payload)
        print(f"{spec}: reloaded", file=sys.stderr)


def _watched(spec: Path) -> dict[Path, int | None]:
    """`spec` and the file its top-level `external_yaml` includes, each with its
    modification time. Re-read every poll, so pointing the include at another
    scene is itself a change — and so is that scene's own arrival."""
    paths = [spec]
    # Missing, half-written or not a mapping: watch the spec alone until it parses again.
    with contextlib.suppress(Exception):
        include = yaml.safe_load(spec.read_text()).get("external_yaml")
        if include:
            paths.append((spec.parent / include).resolve())
    return {path: _mtime(path) for path in paths}


def _mtime(path: Path) -> int | None:
    """`path`'s modification time, or None while it is missing — an editor
    saving a spec replaces the file, so it briefly is."""
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return None


if __name__ == "__main__":
    sys.exit(main())

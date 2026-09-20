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

from arena_composer import simready, spec_edit, usd_assets, viewer_process
from arena_composer.payload import encode, encode_spec
from arena_composer.resolve_layout import DEFAULT_SEED, resolve_layout_from_yaml
from arena_composer.usd_assets import use_local_asset_cache

# A spec is one or two small files, so they are polled rather than watched
# through inotify: two stats twice a second cost nothing and add no dependency.
WATCH_INTERVAL = 0.5
# An editor writes a file in bursts, and a spec caught mid-write does not parse.
# Resolve once it has held still this long.
SETTLE = 0.3

#: What the panel can ask for that is not an edit — see :func:`_serve_assets`.
#: Both are network against NVIDIA's bucket and neither touches the spec.
ASSET_REQUESTS = ("reindex", "thumbnails")

#: How many times an asset request's answer is retried. Its message carries the
#: spec, so a spec caught mid-write fails to build one, and unlike an edit's
#: answer nothing polls behind this one — the request is already served.
ANSWER_TRIES = 3


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
    # The opening payload is the only one that carries the SimReady props: the
    # window keeps the list it is given, and a payload written to a file has
    # nothing else to fill its browser from.
    payload = _resolve(args.spec, args.seed, remote=True)

    if args.payload:
        args.payload.write_bytes(payload)

    return asyncio.run(_view(args.spec, args.seed, payload))


def _resolve(spec: Path, seed: int, remote: bool = False) -> bytes:
    """Resolve `spec` into a packed scene. Seconds of work."""
    # Arena and the solver print progress to stdout; keep it off stdout so a
    # caller can still pipe this command's own output somewhere.
    with contextlib.redirect_stdout(sys.stderr):
        return encode(resolve_layout_from_yaml(spec, seed=seed), remote=remote)


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
        asyncio.create_task(_serve_requests(spec, seed, proc)),
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
    """Hand one packed message to the viewer. The pipe stays open for the next.

    Safe to call from more than one task: the whole framed message goes into the
    transport in a single `write`, so two senders cannot interleave a length
    with somebody else's body.
    """
    assert proc.stdin is not None
    proc.stdin.write(viewer_process.pack_scene(payload))
    await proc.stdin.drain()


async def _serve_requests(spec: Path, seed: int, proc: asyncio.subprocess.Process) -> None:
    """Serve what the editor asks for: edits to the spec, and asset requests.

    An accepted edit is answered twice: with the spec as soon as it is on disk,
    and with the layout it resolves to once the solver has caught up. Only the
    first is on this path, and it costs milliseconds against the solve's
    seconds — which is what lets the panel show an edit at once, and what keeps
    this loop reading while a solve is still running.

    Writing goes through :mod:`arena_composer.spec_edit`, which owns which of
    the two files a key lives in and validates before either is touched, so a
    refused edit leaves the spec exactly as the editor is still drawing it —
    and is answered by that same spec, carrying the reason.

    An asset request is neither written nor solved — it is a minute of network
    at worst — so it goes to :func:`_serve_assets` and this loop reads on.
    """
    assert proc.stdout is not None
    asked = asyncio.Event()
    wanted: asyncio.Queue[dict] = asyncio.Queue()
    helpers = [
        asyncio.create_task(_solve_when_asked(spec, seed, proc, asked)),
        asyncio.create_task(_serve_assets(spec, proc, wanted)),
    ]
    try:
        while (request := await _read_request(proc.stdout)) is not None:
            if (op := request.get("op", "")) in ASSET_REQUESTS:
                wanted.put_nowait(request)
                if op == "reindex":
                    # From here rather than from the worker, which may still be
                    # fetching pictures: the button has to stop taking clicks
                    # when one is asked for, not when one starts.
                    with contextlib.suppress(Exception):
                        await _send(proc, encode_spec(spec, indexing=True))
                continue

            result = await asyncio.to_thread(
                spec_edit.apply, spec, op, list(request.get("path", [])), request.get("value")
            )
            if proc.returncode is not None:
                return  # the window closed while we were writing

            await _send(proc, encode_spec(spec, result.errors, solving=result.ok))
            if result.ok:
                # `_reload_on_change` would catch this too, but only after its
                # poll and settle; an edit the user just made should redraw now.
                asked.set()
                print(f"{spec}: {'; '.join(result.changed)}", file=sys.stderr)
            else:
                print(f"{spec}: refused: {'; '.join(result.errors)}", file=sys.stderr)
    finally:
        for helper in helpers:
            helper.cancel()


async def _serve_assets(spec: Path, proc: asyncio.subprocess.Process, wanted: asyncio.Queue[dict]) -> None:
    """Crawl the bucket, or fetch thumbnails, as the browser asks.

    One request at a time, so a crawl and a batch of pictures never compete for
    the same bucket. Within a batch the pictures do go together; see
    :func:`_thumbnails`.

    Every answer carries the prop list again, a failed one included: the panel
    is waiting on that list, and an answer is what tells it to stop. The list
    is where the crawl's result and the thumbnails' local paths both show up,
    so there is nothing else to send back.
    """
    while True:
        request = await wanted.get()
        crawl = request.get("op") == "reindex"
        errors: list[str] = []
        try:
            if crawl:
                found = await asyncio.to_thread(simready.build)
                print(f"{simready.index_path()}: {len(found)} assets", file=sys.stderr)
            else:
                urls = list(request.get("urls", []))
                await _thumbnails(urls)
                print(f"simready: fetched {len(urls)} thumbnail(s)", file=sys.stderr)
        except Exception as error:
            errors = [f"{type(error).__name__}: {error}"]
            print(f"simready: {errors[0]}", file=sys.stderr)

        if proc.returncode is not None:
            return  # the window closed while we were fetching
        # A failed crawl still says it has stopped, or the button never comes
        # back; the reason is in `errors` beside it.
        await _answer(proc, spec, errors, indexing=False if crawl else None)


async def _answer(
    proc: asyncio.subprocess.Process,
    spec: Path,
    errors: list[str],
    indexing: bool | None = None,
) -> None:
    """Send the browser what it is waiting for, waiting out a write to the spec.

    The message carries the spec as every message does, so one caught mid-write
    builds no message at all. An edit's answer can afford to drop on that —
    `_reload_on_change` sends another the moment the file parses — but this is
    a request's only reply, and the request has already been served.
    """
    for attempt in range(ANSWER_TRIES):
        try:
            await _send(proc, encode_spec(spec, errors, remote=True, indexing=indexing))
            return
        except Exception as error:
            reason = error
        await asyncio.sleep(SETTLE)
    print(f"{spec}: an asset request went unanswered: {reason}", file=sys.stderr)


async def _thumbnails(usd_paths: list[str]) -> None:
    """Mirror the sidecar of each prop, so the panel can draw it off disk.

    Fetched together rather than one after another: each is about 800 ms of
    round trip, and a screenful in a row is eight seconds of blank rows. The
    batch is a screenful because that is what the panel asks for, so this
    leans on the default executor rather than bounding itself.
    """
    await asyncio.gather(*(asyncio.to_thread(_thumbnail, usd_path) for usd_path in usd_paths))


def _thumbnail(usd_path: str) -> None:
    """One sidecar into the mirror.

    A prop with no published sidecar is skipped rather than raised. The panel
    asks for one once and draws a blank for whatever never arrives, so a single
    missing picture must not cost the rest of the batch theirs.
    """
    with contextlib.suppress(Exception):
        usd_assets.localize(simready.thumbnail_url(usd_path))


async def _solve_when_asked(spec: Path, seed: int, proc: asyncio.subprocess.Process, asked: asyncio.Event) -> None:
    """Re-resolve `spec` whenever `asked`, and draw what comes out.

    One solve per burst of edits, not one per edit: `asked` is cleared before
    the solve and set again by anything that arrives during it, so the edits
    made while the solver was busy are answered together by the next one. They
    are all in the file by then — the layout is a function of the spec, not of
    the edits that got it there.
    """
    while True:
        await asked.wait()
        asked.clear()
        layout, errors = await asyncio.to_thread(_solved, spec, seed)
        if proc.returncode is not None:
            return  # the window closed while we were resolving
        # `asked` set again means the edits that arrived during this solve have
        # one of their own coming, so the panel goes on saying the view is
        # behind rather than blinking off and on.
        await _draw(proc, spec, layout, errors, solving=asked.is_set())


def _solved(spec: Path, seed: int) -> tuple[bytes | None, list[str]]:
    """The scene `spec` resolves to, and what stopped it where it does not."""
    try:
        return _resolve(spec, seed), []
    except Exception as error:
        # Anything Arena raises on a spec that validates but will not build.
        reason = f"{type(error).__name__}: {error}"
        print(f"{spec}: {reason}", file=sys.stderr)
        return None, [reason]


async def _draw(
    proc: asyncio.subprocess.Process,
    spec: Path,
    layout: bytes | None,
    errors: list[str] | None = None,
    solving: bool = False,
) -> None:
    """Send a layout, and behind it the spec as the file reads at this moment.

    Two messages, because the spec a layout carries is the one its own solve
    began from, and an edit made during those seconds leaves that copy behind.
    The panel takes the second and ignores the first; `viewer::apply_updates` is
    where it does. Nothing is awaited between reading the file here and writing
    it into the pipe, so what goes out is the file as it stands.

    The spec is left out where it no longer reads, which takes `spec` becoming
    unreadable since the edit that validated it. `_reload_on_change` answers
    once it parses again; until then the panel is left as it is.
    """
    if layout is not None:
        await _send(proc, layout)
    with contextlib.suppress(Exception):
        await _send(proc, encode_spec(spec, errors or [], solving=solving))


async def _read_request(stream: asyncio.StreamReader) -> dict | None:
    """One request off the viewer's stdout, or None once it has ended."""
    try:
        header = await stream.readexactly(viewer_process.HEADER)
        return msgpack.unpackb(await stream.readexactly(int.from_bytes(header, "big")))
    except asyncio.IncompleteReadError:
        return None


async def _reload_on_change(spec: Path, seed: int, proc: asyncio.subprocess.Process) -> None:
    """Re-resolve `spec` on every write to it, or to the scene it includes, and
    draw what comes out. The SimReady index is watched from here too.

    An index rebuilt by another process — the MCP server, or ``python -m
    arena_composer.simready`` — is a new prop list and nothing else, so it is
    sent on its own rather than costing a solve.
    """
    stamps = _watched(spec)
    index = simready.index_stamp()
    while True:
        await asyncio.sleep(WATCH_INTERVAL)
        if (rebuilt := simready.index_stamp()) != index:
            index = rebuilt
            with contextlib.suppress(Exception):
                await _send(proc, encode_spec(spec, remote=True))
            print(f"{simready.index_path()}: reloaded", file=sys.stderr)
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
        await _draw(proc, spec, payload)
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

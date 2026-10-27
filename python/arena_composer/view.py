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
import random
import signal
import sys
from pathlib import Path

import msgpack
import yaml

from arena_composer import catalog, cli, simready, spec_edit, viewer_process
from arena_composer.payload import encode, encode_spec
from arena_composer.resolve_layout import resolve_layout_from_yaml

# A spec is one or two small files, so they are polled rather than watched
# through inotify: two stats twice a second cost nothing and add no dependency.
WATCH_INTERVAL = 0.5
# An editor writes a file in bursts, and a spec caught mid-write does not parse.
# Resolve once it has held still this long.
SETTLE = 0.3

#: How many layouts a solve draws, one per env, for the panel to page through.
#: Five cost little more than one: the solver takes them in one batch.
SAMPLES = 5

#: What the panel asks for that is not an edit: the props, from NVIDIA's
#: bucket — see :func:`_serve_assets` — and the layouts again, from a new seed.
#: Neither touches the spec.
ASSET_REQUEST = "reindex"
REGENERATE = "regenerate"

#: How many times an asset request's answer is retried. Its message carries the
#: spec, so a spec caught mid-write fails to build one, and unlike an edit's
#: answer nothing polls behind this one — the request is already served.
ANSWER_TRIES = 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("spec", type=Path, help="Path to an ArenaEnvGraphSpec YAML")
    cli.add_spec_options(parser)
    parser.add_argument("--payload", type=Path, help="Also write the packed scene here, for `arena-viewer`")
    parser.add_argument(
        "--samples",
        type=int,
        default=SAMPLES,
        metavar="N",
        help=f"Layouts each solve draws, one per env, to page through in the panel (default: {SAMPLES})",
    )
    args = parser.parse_args(argv)
    if args.samples < 1:
        parser.error(f"--samples must be at least 1, got {args.samples}")

    cli.apply_spec_options(parser, args)
    # Stamped before the first solve, so a change made during those seconds is
    # noticed for what it is rather than taken for the file that was solved.
    stamps = _watched(args.spec)
    # The opening payload is the only one that carries the SimReady props: the
    # window keeps the list it is given, and a payload written to a file has
    # nothing else to fill its browser from.
    payload = _resolve(args.spec, args.seed, args.samples, remote=True)

    if args.payload:
        args.payload.write_bytes(payload)

    return asyncio.run(_view(args.spec, args.seed, args.samples, payload, stamps))


def _resolve(spec: Path, seed: int | None, samples: int, remote: bool = False) -> bytes:
    """Resolve `spec` into a packed scene of `samples` layouts. Seconds of work."""
    # Arena and the solver print progress to stdout; keep it off stdout so a
    # caller can still pipe this command's own output somewhere.
    with contextlib.redirect_stdout(sys.stderr):
        return encode(resolve_layout_from_yaml(spec, seed=seed, samples=samples), remote=remote)


async def _view(
    spec: Path, seed: int | None, samples: int, payload: bytes, stamps: dict[Path, int | None]
) -> int:
    """Draw `payload`, reloading it from `spec` until the window closes.
    `stamps` is the files as they stood when `payload` was solved from them."""
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
    # One solver, asked by the panel's edits and by changes on disk alike: two
    # solves at once could land out of order and leave the older layout drawn.
    # The files are stamped as `payload` was solved from them, and the panel
    # stamps its own writes, so the watcher does not ask for those a second
    # time. `reseed` asks the next solve to draw from a new seed.
    asked, reseed = asyncio.Event(), asyncio.Event()
    running = [
        asyncio.create_task(_solve_when_asked(spec, seed, samples, proc, asked, reseed)),
        asyncio.create_task(_reload_on_change(spec, proc, stamps, asked)),
        asyncio.create_task(_serve_requests(spec, proc, stamps, asked, reseed)),
        asyncio.create_task(_catch_up_on_pictures(spec, proc)),
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


async def _serve_requests(
    spec: Path,
    proc: asyncio.subprocess.Process,
    stamps: dict[Path, int | None],
    asked: asyncio.Event,
    reseed: asyncio.Event,
) -> None:
    """Serve what the editor asks for: edits to the spec, new layouts, and
    asset requests.

    An accepted edit is answered twice: with the spec as soon as it is on disk,
    and with the layout it resolves to once `_solve_when_asked`, which `asked`
    asks, has caught up. Only the first is on this path, and it costs
    milliseconds against the solve's seconds — which is what lets the panel
    show an edit at once, and what keeps this loop reading while a solve is
    still running.

    Writing goes through :mod:`arena_composer.spec_edit`, which owns which of
    the two files a key lives in and validates before either is touched, so a
    refused edit leaves the spec exactly as the editor is still drawing it —
    and is answered by that same spec, carrying the reason.

    An asset request is neither written nor solved — it is minutes of network
    — so it goes to :func:`_serve_assets` and this loop reads on.

    New layouts are a solve and no write: `reseed` tells
    :func:`_solve_when_asked` to draw from a new seed, and the answer says one
    is coming, as an accepted edit's does.
    """
    assert proc.stdout is not None
    crawls: asyncio.Queue[None] = asyncio.Queue()
    helpers = [asyncio.create_task(_serve_assets(spec, proc, crawls))]
    try:
        while (request := await _read_request(proc.stdout)) is not None:
            if (op := request.get("op", "")) == ASSET_REQUEST:
                crawls.put_nowait(None)
                # From here rather than from the worker, which may still be on
                # the crawl before this one: the button has to stop taking
                # clicks when one is asked for, not when one starts.
                with contextlib.suppress(Exception):
                    await _send(proc, encode_spec(spec, indexing=True))
                continue
            if op == REGENERATE:
                reseed.set()
                asked.set()
                with contextlib.suppress(Exception):
                    await _send(proc, encode_spec(spec, solving=True))
                continue

            result = await asyncio.to_thread(
                spec_edit.apply, spec, op, list(request.get("path", [])), request.get("value")
            )
            if proc.returncode is not None:
                return  # the window closed while we were writing

            if result.ok:
                # Stamped and asked for before the answer goes out, however long
                # the pipe takes to drain: the watcher catches the write after its
                # poll and settle, and one it finds unstamped it asks for again —
                # a refusal twice over, dropping what was typed in between.
                _restamp(stamps, spec)
                asked.set()
                print(f"{spec}: {'; '.join(result.changed)}", file=sys.stderr)
            else:
                print(f"{spec}: refused: {'; '.join(result.errors)}", file=sys.stderr)
            if result.remaining_errors:
                print(f"{spec}: still wrong from before: {'; '.join(result.remaining_errors)}", file=sys.stderr)
            await _send(proc, encode_spec(spec, result.errors, solving=result.ok))
    finally:
        for helper in helpers:
            helper.cancel()


async def _serve_assets(spec: Path, proc: asyncio.subprocess.Process, crawls: asyncio.Queue[None]) -> None:
    """Crawl the bucket as the browser asks, one crawl at a time.

    A crawl writes the index, then fetches the picture of everything in it —
    about a minute each, and ~170 MB for the pictures, which is what the button
    that asks for this says. The panel hears twice: `_reload_on_change` notices
    the index the moment it lands and sends the names, and this answer carries
    the pictures behind them.

    That answer carries the prop list again, a failed crawl's included: the
    panel is waiting on that list, and an answer is what tells it to stop.
    """
    while True:
        await crawls.get()
        errors: list[str] | None = None
        try:
            found = await asyncio.to_thread(simready.build)
            print(f"{simready.index_path()}: {len(found)} assets", file=sys.stderr)
            fetched = await asyncio.to_thread(simready.fetch_thumbnails, [asset.usd_path for asset in found])
            print(f"simready: fetched {fetched} thumbnail(s)", file=sys.stderr)
        except Exception as error:
            errors = [f"{type(error).__name__}: {error}"]
            print(f"simready: {errors[0]}", file=sys.stderr)

        if proc.returncode is not None:
            return  # the window closed while we were fetching
        # A failed crawl still says it has stopped, or the button never comes
        # back; the reason is in `errors` beside it.
        await _answer(proc, spec, errors, indexing=False)


async def _answer(
    proc: asyncio.subprocess.Process,
    spec: Path,
    errors: list[str] | None = None,
    indexing: bool | None = None,
) -> None:
    """Send the browser what it is waiting for, waiting out a write to the spec.

    The message carries the spec as every message does, so one caught mid-write
    builds no message at all, unless it has errors to carry (see
    `payload.encode_spec`). An edit's answer can afford to drop on that — the
    write that finishes the file is a change `_reload_on_change` asks a solve
    for, and the solve answers — but this is a request's only reply, and the
    request has already been served.
    """
    for attempt in range(ANSWER_TRIES):
        try:
            await _send(proc, encode_spec(spec, errors, remote=True, indexing=indexing))
            return
        except Exception as error:
            reason = error
        await asyncio.sleep(SETTLE)
    print(f"{spec}: an asset request went unanswered: {reason}", file=sys.stderr)


async def _catch_up_on_pictures(spec: Path, proc: asyncio.subprocess.Process) -> None:
    """Fetch whatever picture Arena's registered assets are missing, once, at
    startup.

    A prop's picture comes with the crawl that indexes it, but the registered
    assets are in no index — nothing else would ever fetch theirs, and they are
    what the browser opens on. A few hundred small files, ~20 MB, the first
    time; a stat each after that. The props are not offered here even where
    the index has some without pictures: that is up to 170 MB, and only the
    reindex button asks for it.

    The answer is sent only if something arrived. Without that, every startup
    would spend a megabyte of prop list saying nothing had changed.
    """
    try:
        fetched = await asyncio.to_thread(
            simready.fetch_thumbnails, [asset.usd_path for asset in catalog.catalog()]
        )
    except Exception as error:
        # Pictures are a nicety: a failure costs the blank rows and nothing more.
        print(f"simready: {type(error).__name__}: {error}", file=sys.stderr)
        return
    print(f"simready: fetched {fetched} thumbnail(s)", file=sys.stderr)
    if fetched and proc.returncode is None:
        await _answer(proc, spec)


async def _solve_when_asked(
    spec: Path,
    seed: int | None,
    samples: int,
    proc: asyncio.subprocess.Process,
    asked: asyncio.Event,
    reseed: asyncio.Event,
) -> None:
    """Re-resolve `spec` whenever `asked` — by an edit from the panel, a change
    on disk, or the panel asking for new layouts — and draw what comes out.

    The one solver for all of them, so no two solves overlap: each reads the
    file after the last one did, and the layouts land in the order of the
    files they were solved from.

    One solve per burst of edits, not one per edit: `asked` is cleared before
    the solve and set again by anything that arrives during it, so the edits
    made while the solver was busy are answered together by the next one. They
    are all in the file by then — the layout is a function of the spec, not of
    the edits that got it there.

    And of the seed, which `reseed` replaces with a random one. Every solve
    after keeps it, an edit's included, as ``--seed`` would: the layouts asked
    for stay up rather than going back to the old seed's at the next edit.
    """
    while True:
        await asked.wait()
        asked.clear()
        if reseed.is_set():
            reseed.clear()
            # Six digits, to read off the panel and pass back as --seed.
            seed = random.randrange(1_000_000)
        layout, errors = await asyncio.to_thread(_solved, spec, seed, samples)
        if proc.returncode is not None:
            return  # the window closed while we were resolving
        # `asked` set again means the edits that arrived during this solve have
        # one of their own coming, so the panel goes on saying the view is
        # behind rather than blinking off and on.
        await _draw(proc, spec, layout, errors, solving=asked.is_set())


def _solved(spec: Path, seed: int | None, samples: int) -> tuple[bytes | None, list[str]]:
    """The scene `spec` resolves to, and what stopped it where it does not."""
    try:
        return _resolve(spec, seed, samples), []
    except Exception as error:
        # Anything Arena raises on a spec that will not build: one from the
        # panel that validates but will not place, or a change on disk that
        # does not even read.
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

    A spec that no longer reads still carries `errors`, on the spec it last
    read to — see `payload.encode_spec`. With none to carry it is left out, and
    `_reload_on_change` asks for another solve once it reads again.
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


async def _reload_on_change(
    spec: Path, proc: asyncio.subprocess.Process, stamps: dict[Path, int | None], asked: asyncio.Event
) -> None:
    """Ask for `spec` to be re-resolved on every write to it, or to the scene it
    includes. The SimReady index is watched from here too.

    Solved by `_solve_when_asked`, as an edit from the panel is, and answered
    the same way: a change Arena cannot build — a hand edit, or an agent's —
    leaves the last layout up, and the reason goes to the panel with the spec
    as the file now reads, or, where it no longer reads at all, as it last did.

    `stamps` is shared with `_serve_requests`, which brings it up to date with
    every write it makes: it asks for those itself.

    An index rebuilt by another process — the MCP server, or ``python -m
    arena_composer.simready`` — is a new prop list and nothing else, so it is
    sent on its own rather than costing a solve.
    """
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
        if _watched(spec) == stamps:
            continue  # the panel's own write, landing while this one polled
        _restamp(stamps, spec)
        asked.set()
        print(f"{spec}: changed on disk", file=sys.stderr)


def _watched(spec: Path) -> dict[Path, int | None]:
    """`spec` and the files its top-level `external_yaml` includes, each with its
    modification time. Re-read every poll, so pointing the include at another
    scene is itself a change — and so is that scene's own arrival."""
    paths = [spec]
    # Missing, half-written or not a mapping: watch the spec alone until it parses again.
    with contextlib.suppress(Exception):
        include = yaml.safe_load(spec.read_text()).get("external_yaml")
        paths += [(spec.parent / path).resolve() for path in ([include] if isinstance(include, str) else include or [])]
    return {path: _mtime(path) for path in paths}


def _restamp(stamps: dict[Path, int | None], spec: Path) -> None:
    """Bring `stamps` up to the files as they stand. In place, since the
    watcher and the request loop hold the same one."""
    stamps.clear()
    stamps.update(_watched(spec))


def _mtime(path: Path) -> int | None:
    """`path`'s modification time, or None while it is missing — an editor
    saving a spec replaces the file, so it briefly is."""
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return None


if __name__ == "__main__":
    sys.exit(main())

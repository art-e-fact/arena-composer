"""An MCP server for authoring Arena environment specs.

Nine tools, chosen against the question "what can the agent not already do with
Read and Edit?" — listing a spec's objects did not survive that test, since the
YAML is short and reading it is exact and free. What survived either touches
live state (the asset registry, the SimReady index, the solver, a GPU) or knows
something about the task/scene file pair that a text edit does not.

The server describes itself: ``INSTRUCTIONS`` below and each tool's own
docstring are what a client shows the model, so they are the documentation for
using these tools and there is no copy of it elsewhere to keep in step.

Nothing here holds state. Every tool takes a spec path and leaves the result on
disk, so an ``arena-view`` window open on the same spec redraws itself as the
agent works — its watcher does not care who wrote the file.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import Context, Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from arena_composer import catalog, render, simready, spec_edit, usd_assets

# From `spec_edit`, not `resolve_layout`: importing the latter imports Arena's
# solver, two seconds the client spends waiting on the handshake. The first
# tool that solves pays for it instead — see `_resolve`.
from arena_composer.spec_edit import DEFAULT_SEED, SpecError

#: Sent to the client at handshake, as the model's orientation to this server.
#: Per-tool detail lives in the tool docstrings; this is what ties them into an
#: order of work and says what each step costs.
INSTRUCTIONS = """\
Authoring tools for IsaacLab-Arena environment specs: the YAML that names a
scene's robot, background and objects, and the spatial relations a solver turns
into a layout.

A spec is usually two files — a task YAML including a scene YAML through
`external_yaml` — which validate as one document. Address the tools at the task;
the edit tools work out which of the two an edit belongs in.

How the tools fit together:

1. `search_assets` to find a `registry_name`. Never guess one: the registry is
   the only place these names exist. NVIDIA's SimReady props — ~1,500 objects
   beyond the registry — are searched too once `reindex_assets` has built the
   local index; a hit among them carries the `params` to add it with.
   `list_kinds` for the relation and task kinds a spec can name, and the
   params each takes.
2. `add_object` / `remove_object` to edit the objects, `set_embodiment` to
   change the robot. Editing the YAML directly is fine too, and is the way to
   do anything these do not cover; run `check_spec` after a hand edit. Quote
   the `on` relation kind — `kind: 'on'` — since Arena reads YAML 1.1, where
   a bare `on` is `true`.
3. `describe_scene` for where the solver actually put things, and whether the
   layout passed the checks a dry solve can run.
4. `render_views` to look at it.

Steps 1 and 2 and `check_spec` return in milliseconds. Steps 3 and 4 re-run
the placement solver and read every asset's USD, so they take seconds — make a
run of edits, then check once.

A failed edit is a normal result, not an error: it comes back as
`{"ok": false, "errors": [...]}` with both files untouched, and so does a
`describe_scene` of a spec that does not read, validate or build, and a
`check_spec` of one. Read the errors and correct the call, or the spec. An
edit on a spec that is already broken is refused only for what it introduces;
what was wrong before comes back as `remaining_errors`, whether the edit
landed or not, so a spec with two problems is repaired one edit at a time.

`describe_scene` answers "did that land where I meant" more cheaply and more
precisely than a picture. Reach for `render_views` when the question is about
shape or contact — does this rest on the surface, do these two overlap, is it
facing the right way. Ask only for the views that answer it: each image costs
roughly a thousand tokens to read, and in any single view one object can sit
behind another.

What a resolve reports is one seeded sample. Pin `seed` to compare two edits;
at runtime Arena draws a different valid layout on every reset, so before
calling a layout done ask `describe_scene` for `samples=8` and read how many
passed.
"""

mcp = MCPServer("arena-composer", instructions=INSTRUCTIONS)

#: Declared on the tools that write nothing of the user's, so a client may run
#: them without asking. A resolve still fills the asset cache and a render
#: writes PNGs under tmp, which is this process's own housekeeping.
_READ_ONLY = ToolAnnotations(read_only_hint=True)

#: Where renders are written. One directory per spec and view, so a re-render
#: replaces its picture rather than filling the disk with a history of them.
_RENDERS = Path(tempfile.gettempdir()) / "arena-composer-renders"


@contextlib.contextmanager
def _quiet():
    """Keep Arena's chatter out of the protocol.

    Arena and the placement solver report progress with `print`, and on a stdio
    server stdout is the wire: one `[placement] Validated ...` line in the
    stream and the client fails to parse a message. Every tool does its work in
    here, importing included — the registries print while they populate.

    So it is also where `catalog.load_extensions` runs, before any tool reads
    a registry. Not in `main()`: that imports Arena, seconds the handshake
    would wait for and the first tool pays for anyway. A module that does not
    import is a refusal the agent can read and pass on.
    """
    with contextlib.redirect_stdout(sys.stderr):
        try:
            catalog.load_extensions()
        except ImportError as error:
            raise ToolError(str(error)) from error
        yield


def _spec_path(spec: str) -> Path:
    """The task YAML `spec` names, or a refusal the agent can read.

    A `ToolError`, the SDK's channel for a failure a tool saw coming: the
    model reads its message, where any other exception reaches it as a bare
    `Error executing tool`. Not a result, because these are about the
    argument — no file there, a scene where the task was asked for — and the
    tools' results are about the spec.
    """
    path = Path(spec).expanduser().resolve()
    if not path.is_file():
        raise ToolError(f"no spec at {path}")
    try:
        spec_edit.check_entry(path)
    except ValueError as error:
        raise ToolError(str(error)) from error
    return path


def _resolve(path: Path, seed: int, samples: int = 1) -> dict[str, Any]:
    """Resolve a spec into the layout dump. Seconds of work: Arena's solver runs
    and every asset's USD is read."""
    with _quiet():
        # Imported here, not at module scope: Arena takes seconds to import,
        # and an MCP server is started by its client, not by the person
        # waiting for it.
        from arena_composer.resolve_layout import resolve_layout_from_yaml

        return resolve_layout_from_yaml(path, seed=seed, samples=samples)


#: Seconds between progress reports while a solve or a download runs.
HEARTBEAT = 10.0


class _Progress:
    """Progress for one tool call, through its `ctx`.

    Progress, not a log message: the protocol deprecated logging (SEP-2577,
    2026-07-28) and a client has to opt in per request to receive any, where
    a progress notification reaches whoever asked for progress on this call.
    The protocol also wants each value above the last, so the steps and the
    heartbeats between them share one count.
    """

    def __init__(self, ctx: Context | None) -> None:
        self.ctx, self.count = ctx, 0

    async def report(self, message: str) -> None:
        if self.ctx is not None:
            # A Context with no request behind it, as a direct call gives,
            # has nobody to report to.
            with contextlib.suppress(ValueError):
                await self.ctx.report_progress(self.count, message=message)
        self.count += 1

    async def run(self, doing: str, work: Callable[..., Any], *args: Any) -> Any:
        """`work(*args)` off the event loop, saying it is still `doing` every
        `HEARTBEAT` seconds: a solve or a first download runs for minutes with
        nothing else to show, and looks frozen."""
        await self.report(doing)
        started = time.monotonic()
        running = asyncio.ensure_future(asyncio.to_thread(work, *args))
        while True:
            try:
                # Shielded: the wait timing out must not cancel the work.
                return await asyncio.wait_for(asyncio.shield(running), HEARTBEAT)
            except TimeoutError:
                await self.report(f"{doing}: {time.monotonic() - started:.0f} s")


def _reasons(error: Exception) -> list[str]:
    """What stopped a resolve, as lines the agent can act on: `SpecError`'s
    own, one per object, and for anything else Arena raises on a spec that
    does not read, validate or build — not YAML, a name the registry does not
    know, a placement that fails — its type and message, as `arena-view`'s
    toast gives them."""
    return error.errors if isinstance(error, SpecError) else [f"{type(error).__name__}: {error}"]


async def _or_refused(progress: _Progress, doing: str, work: Callable[..., Any], *args: Any) -> Any:
    """`work(*args)` off the event loop — a resolve or a render blocks for
    seconds — or what stopped it as a `ToolError`. Pictures or a reason, never
    both; and a `ToolError` because the model reads its message, where any
    other exception's is withheld."""
    try:
        return await progress.run(doing, work, *args)
    except Exception as error:
        raise ToolError("\n".join(_reasons(error))) from error


@mcp.tool(annotations=_READ_ONLY)
def search_assets(query: str = "", role: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """Find assets to use as an object, background or embodiment `registry_name`.

    Two sources. Arena's registry, with the Isaac Cap assets its own CLI adds
    and whatever the modules in `ARENA_COMPOSER_IMPORTS` register — set in
    `bin/arena-mcp`, which `arena-composer init --import MODULE` writes. And
    NVIDIA's SimReady props: ~1,500 physics-ready objects on a public bucket,
    searched once `reindex_assets` has built the local index. A SimReady hit shares one `registry_name` with
    every other and is told apart by its `params`, which `add_object` has to be
    given as they are. Never make a URL up; the index is where they come from.

    `cached` says whether the asset's USD is already on this machine. The
    first resolve of one that is not downloads it: tens of megabytes for a
    prop, more for a scene.

    Args:
        query: Words to match against an asset's name, tags and file name,
            e.g. "banana" or "ycb bowl". Empty matches everything.
        role: Restrict to "object", "background" or "embodiment".
        limit: How many to return, best match first — registered before
            SimReady.
    """
    with _quiet():
        return [_hit(asset) for asset in catalog.search(query, role=role, limit=limit)]


def _hit(asset: catalog.Asset) -> dict[str, Any]:
    """One search result, carrying only the fields the asset has."""
    return {
        "name": asset.name,
        "registry_name": asset.registry_name,
        "role": asset.role,
        "tags": asset.tags,
        **({"object_type": asset.object_type} if asset.object_type else {}),
        **({"params": asset.params} if asset.params else {}),
        **({"cached": usd_assets.mirrored(asset.usd_path) is not None} if asset.usd_path else {}),
    }


@mcp.tool()
async def reindex_assets() -> dict[str, Any]:
    """Build the local index of NVIDIA's SimReady props, so `search_assets`
    finds them.

    Needed once: the index is a file, kept until this runs again, and shared
    with a viewer open on the same machine. Takes tens of seconds — a listing
    of the bucket, with no asset downloaded. Run it again to pick up props
    published since.
    """
    started = time.monotonic()
    assets = await asyncio.to_thread(_reindex)
    return {"indexed": len(assets), "index": str(simready.index_path()), "seconds": round(time.monotonic() - started, 1)}


def _reindex() -> list[simready.RemoteAsset]:
    with _quiet():
        return simready.build()


@mcp.tool(annotations=_READ_ONLY)
async def describe_scene(
    spec: str, seed: int = DEFAULT_SEED, samples: int = 1, ctx: Context | None = None
) -> dict[str, Any]:
    """Resolve a spec and report where every asset ended up.

    This is the spec as the simulator would build it, not as it was written:
    positions come from the placement solver. Read the YAML itself for what
    was authored.

    `placement` is the solver's verdict on the first sample: `checks` and
    `failed_checks` for what a dry solve can decide — resting on, next to,
    overlap — and `not_run` for what it did not check: what only the
    simulator can, physics settling among them, and any the spec's
    `placement_validators` leave out. A pass is necessary for the sim to
    accept the layout, not sufficient.

    Arena draws a new layout on every reset, so one passing sample says
    little. With `samples` above 1 the solver draws that many at once and
    `placement` adds `passed` of `samples`, `failed_samples` by index,
    `check_failures` counting each failed check across them, and
    `distinct_layouts`, since samples can converge on the same layout.

    A light or a ground plane has nothing to place: it stays where it is
    configured, unsolved, with an empty box.

    Takes seconds, and longer the first time an asset is downloaded; progress
    is reported every ten seconds meanwhile. Eight samples cost about what
    one does on a scene of boxes, twice that on detailed meshes. Prefer it
    after a run of edits rather than between each.

    A spec that does not read, validate or build comes back as `{"ok": false,
    "errors": [...]}` with no layout: one line per object whose
    `params.usd_path` cannot be used — a URL that is not on the server, or a
    prop that is not the single rigid body the sim would spawn — one per
    relation naming a light or a ground plane, which take none, or Arena's
    own reason.

    Args:
        spec: Path to the task YAML.
        seed: Placement seed. The solver samples a different valid layout per
            seed, so pin it to compare two edits.
        samples: How many layouts to draw and check, at least 1. The first is
            the one described.
    """
    path = _spec_path(spec)
    if samples < 1:
        raise ToolError(f"samples must be at least 1, got {samples}")
    # Off the event loop: a resolve blocks for seconds and the server has to
    # stay answerable for all of them.
    try:
        dump = await _Progress(ctx).run(f"resolving {path.name} (seed {seed})", _resolve, path, seed, samples)
    except Exception as error:
        # A spec that does not read, validate or build is the same normal case
        # as a refused edit, and reads the same way. Returned rather than
        # raised: the SDK withholds any exception's message but `ToolError`'s.
        return {"ok": False, "errors": _reasons(error)}
    return {
        "env_name": dump["env_name"],
        "spec_path": dump["spec_path"],
        "placement": dump["placement"],
        "assets": [
            {
                "id": asset["id"],
                "role": asset["role"],
                "registry_name": asset.get("registry_name"),
                "position_xyz": [round(value, 4) for value in asset["position_xyz"]],
                "size_xyz": [round(high - low, 4) for high, low in zip(asset["bbox_max"], asset["bbox_min"])],
                "is_anchor": asset["is_anchor"],
                "solved": asset["solved"],
            }
            for asset in dump["assets"]
        ],
        "relations": dump["relations"],
    }


@mcp.tool()
def add_object(
    spec: str,
    id: str,
    registry_name: str,
    on: str | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Add an object to a scene, resting on another asset.

    Writes to whichever of the spec and its included scene owns `objects` —
    they are a disjoint union, so only one of them can. The edit is validated
    against the merged pair and both files are left untouched if it fails. A
    spec already broken is refused only for what this edit introduces, with
    the rest as `remaining_errors` — so an object a task already names can be
    added even while the task names another that is still missing.

    Args:
        spec: Path to the task YAML.
        id: Node id for the new object, unique within the spec.
        registry_name: A name from `search_assets`.
        on: Id of the asset to place it on. Without one the object takes no
            part in placement and lands at the origin. Leave it out for a
            light or a ground plane, which take no relation.
        params: The `params` the search hit carries, as they are: a SimReady
            prop needs its `usd_path`. An object short of what its asset needs
            is refused before the file is written.
    """
    with _quiet():
        return vars(spec_edit.add_object(_spec_path(spec), id, registry_name, on, params))


@mcp.tool()
def remove_object(spec: str, id: str) -> dict[str, Any]:
    """Remove an object from a scene, along with every relation naming it and
    the object references inside it — a reference is a prim of the object's
    USD — with their relations in turn. `changed` lists what went.

    A task param still pointing at it is left in place and refused as the
    error this edit introduces — what to do about the task is a decision, not
    a cleanup. Problems the spec had before come back as `remaining_errors`.

    Args:
        spec: Path to the task YAML.
        id: Node id of the object to remove.
    """
    with _quiet():
        return vars(spec_edit.remove_object(_spec_path(spec), id))


@mcp.tool()
def set_embodiment(spec: str, registry_name: str) -> dict[str, Any]:
    """Change the robot a spec is built around.

    Variants of one robot differ in how it is commanded — `droid_abs_joint_pos`
    against `droid_differential_ik` — and that is what a policy's action space
    has to match. `search_assets(role="embodiment")` lists them all.

    The embodiment keeps its `id`, which relations, task params and CLI
    overrides name it by. Its `params` keep what the new robot also takes and
    lose the rest: `enable_cameras` survives any switch, a DROID's
    `stand_height_m` does not survive a switch to a Franka. `changed` lists
    what was dropped.

    The first `describe_scene` or `render_views` after switching to a robot not
    yet on this machine downloads its USD. Problems the spec had before the
    switch come back as `remaining_errors`, and do not refuse it.

    Args:
        spec: Path to the task YAML.
        registry_name: An embodiment's name from `search_assets`. Any other
            asset is refused.
    """
    with _quiet():
        return vars(spec_edit.apply(_spec_path(spec), "set", ["embodiment", "registry_name"], registry_name))


@mcp.tool(annotations=_READ_ONLY)
async def render_views(
    spec: str,
    views: list[str] | None = None,
    size: int = render.DEFAULT_SIZE,
    seed: int = DEFAULT_SEED,
    ctx: Context | None = None,
) -> list[Image]:
    """Draw a spec and return the pictures, to check a layout by looking at it.

    Views:
        default  perspective, framed the way the viewer window opens
        top      orthographic from above — placement in XY, and overlaps
        front    orthographic from Arena -Y — heights, gaps, floating objects
        side     orthographic from Arena +X — the same, along the other axis

    The axis views are orthographic so that a gap reads as a gap: under
    perspective, whether an object looks like it rests on a surface depends on
    where in the frame it sits. "top" and "front" together answer most of it.

    Takes seconds, and images are expensive to read — ask for the views that
    answer the question, not all four.

    A spec that does not read, validate or build fails with the reason — the
    lines `describe_scene` returns as `errors` — since there is no layout to
    draw. A render that logs an error fails too, rather than returning the
    picture, since an asset that did not load leaves its object out of a frame
    that otherwise looks right. That message is the renderer's, and is not
    about the spec.

    Args:
        spec: Path to the task YAML.
        views: Which views to draw. Defaults to top and front.
        size: Image size in pixels, square.
        seed: Placement seed, as for `describe_scene`.
    """
    path = _spec_path(spec)
    views = views or ["top", "front"]
    if unknown := [view for view in views if view not in render.VIEWS]:
        raise ToolError(f"unknown view(s) {unknown}, expected any of {list(render.VIEWS)}")

    progress = _Progress(ctx)
    payload = await _or_refused(progress, f"resolving {path.name} (seed {seed})", _pack, path, seed)
    out_dir = _RENDERS / hashlib.sha256(str(path).encode()).hexdigest()[:16]
    paths = await _or_refused(progress, f"drawing {', '.join(views)}", render.render, payload, out_dir, views, size)
    return [Image(path=path) for path in paths]


@mcp.tool(annotations=_READ_ONLY)
def check_spec(spec: str) -> dict[str, Any]:
    """Validate a spec as Arena will read it: milliseconds, no solve, no asset read.

    The check every edit tool runs before it writes, on the files as they are
    on disk — so this is what to run after editing the YAML by hand, before
    `describe_scene` spends seconds on it. It catches what stops a spec
    loading: YAML that does not parse, a `registry_name` the registry lacks, a
    relation or task param naming an id the spec has not got, an object short
    of the params its asset needs, a bare `on` read as the boolean true.

    Whether the layout solves it cannot see; that is `describe_scene`.

    Returns what an edit returns, with nothing written: `{"ok": true}` for a
    spec that validates, else `{"ok": false, "errors": [...]}`, one line per
    problem.

    Args:
        spec: Path to the task YAML.
    """
    with _quiet():
        return vars(spec_edit.check(_spec_path(spec)))


@mcp.tool(annotations=_READ_ONLY)
def list_kinds() -> dict[str, Any]:
    """The relation and task kinds a spec can name, with the params each takes.

    `relations` and `tasks` carry one entry per registered kind: its `name` as
    the spec writes it, a `summary`, and `fields` — each param with its
    `kind` (`number`, `int`, `bool`, `text`, `enum` with `options`, or
    `node_ref`, which names another entry's id), whether it is `required`, and
    its `default`. `other_params` lists the params with a shape no field
    covers — a tuple range, a list of ids — by their annotated type.
    `agent_ready` marks the kinds Arena vetted for its own agent; the rest are
    registered all the same. A `unary` relation takes no `reference`.
    `compositions` is what `task.composition` may be.

    Milliseconds, about 16 kB, and the same for the life of the server, so
    once is enough.
    """
    with _quiet():
        # Imported here, as `_pack` does: the payload module pulls in numpy
        # and the geometry readers, which nothing on this path needs.
        from arena_composer.payload import kinds

        return kinds()


def _pack(path: Path, seed: int) -> bytes:
    """Resolve a spec and pack it for the renderer.

    Without the SimReady props: they exist for the asset browser to list, and a
    render has no panel to list them in.
    """
    dump = _resolve(path, seed)
    with _quiet():
        from arena_composer.payload import encode

        return encode(dump, remote=False)


def main() -> int:
    """Run the server on stdio, the transport a local client starts it with."""
    mcp.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())

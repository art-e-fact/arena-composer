"""An MCP server for authoring Arena environment specs.

Six tools, chosen against the question "what can the agent not already do with
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
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import Context, Image, MCPServer

from arena_composer import catalog, render, simready, spec_edit, usd_assets
from arena_composer.resolve_layout import DEFAULT_SEED, SpecError

#: Sent to the client at handshake, as the model's orientation to this server.
#: Per-tool detail lives in the tool docstrings; this is what ties them into an
#: order of work and says what each step costs.
INSTRUCTIONS = """\
Authoring tools for IsaacLab-Arena environment specs: the YAML under
`environments/` that names a scene's robot, background and objects, and the
spatial relations a solver turns into a layout.

A spec is usually two files — a task YAML including a scene YAML through
`external_yaml` — which validate as one document. The edit tools work out which
of the two an edit belongs in, so address them at either path.

How the tools fit together:

1. `search_assets` to find a `registry_name`. Never guess one: the registry is
   the only place these names exist. NVIDIA's SimReady props — ~1,500 objects
   beyond the registry — are searched too once `reindex_assets` has built the
   local index; a hit among them carries the `params` to add it with.
2. `add_object` / `remove_object` to edit. Editing the YAML directly is fine
   too, and is the way to do anything these two do not cover.
3. `describe_scene` for where the solver actually put things, and whether the
   layout passed its checks.
4. `render_views` to look at it.

Steps 1 and 2 return in milliseconds. Steps 3 and 4 re-run the placement solver
and read every asset's USD, so they take seconds — make a run of edits, then
check once.

A failed edit is a normal result, not an error: it comes back as
`{"ok": false, "errors": [...]}` with both files untouched. Read the errors and
correct the call.

`describe_scene` answers "did that land where I meant" more cheaply and more
precisely than a picture. Reach for `render_views` when the question is about
shape or contact — does this rest on the surface, do these two overlap, is it
facing the right way. Ask only for the views that answer it: each image costs
roughly a thousand tokens to read, and in any single view one object can sit
behind another.

What a resolve reports is one seeded sample. Pin `seed` to compare two edits;
at runtime Arena draws a different valid layout on every reset.
"""

mcp = MCPServer("arena-composer", instructions=INSTRUCTIONS)

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
    """
    with contextlib.redirect_stdout(sys.stderr):
        yield


def _spec_path(spec: str) -> Path:
    path = Path(spec).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"no spec at {path}")
    return path


def _resolve(path: Path, seed: int) -> dict[str, Any]:
    """Resolve a spec into the layout dump. Seconds of work: Arena's solver runs
    and every asset's USD is read."""
    with _quiet():
        # Imported here, not at module scope: Arena takes seconds to import,
        # and an MCP server is started by its client, not by the person
        # waiting for it.
        from arena_composer.resolve_layout import resolve_layout_from_yaml

        return resolve_layout_from_yaml(path, seed=seed)


@mcp.tool()
def search_assets(query: str = "", role: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """Find assets to use as an object, background or embodiment `registry_name`.

    Two sources. Arena's registry, and NVIDIA's SimReady props: ~1,500
    physics-ready objects on a public bucket, searched once `reindex_assets`
    has built the local index. A SimReady hit shares one `registry_name` with
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


@mcp.tool()
async def describe_scene(spec: str, seed: int = DEFAULT_SEED) -> dict[str, Any]:
    """Resolve a spec and report where every asset ended up.

    This is the spec as the simulator would build it, not as it was written:
    positions come from the placement solver, and `placement` says whether the
    layout it found is one the sim would accept. Read the YAML itself for what
    was authored.

    Takes seconds. Prefer it after a run of edits rather than between each.

    An object whose `params.usd_path` cannot be used — a URL that is not on the
    server, or a prop that is not the single rigid body the sim would spawn —
    comes back as `{"ok": false, "errors": [...]}`, one line per object, with
    no layout.

    Args:
        spec: Path to the task or scene YAML.
        seed: Placement seed. The solver samples a different valid layout per
            seed, so pin it to compare two edits.
    """
    path = _spec_path(spec)
    # Off the event loop: a resolve blocks for seconds and the server has to
    # stay answerable for all of them.
    try:
        dump = await asyncio.to_thread(_resolve, path, seed)
    except SpecError as error:
        # A spec that validates but names an asset the sim cannot use is the
        # same normal case as a refused edit, and reads the same way.
        return {"ok": False, "errors": error.errors}
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
    against the merged pair and both files are left untouched if it fails.

    Args:
        spec: Path to the task or scene YAML.
        id: Node id for the new object, unique within the spec.
        registry_name: A name from `search_assets`.
        on: Id of the asset to place it on. Without one the object takes no
            part in placement and lands at the origin.
        params: The `params` the search hit carries, as they are: a SimReady
            prop needs its `usd_path`. An object short of what its asset needs
            is refused before the file is written.
    """
    with _quiet():
        return vars(spec_edit.add_object(_spec_path(spec), id, registry_name, on, params))


@mcp.tool()
def remove_object(spec: str, id: str) -> dict[str, Any]:
    """Remove an object from a scene, along with every relation naming it.

    A task param still pointing at it is left in place and reported as a
    validation error — what to do about the task is a decision, not a cleanup.

    Args:
        spec: Path to the task or scene YAML.
        id: Node id of the object to remove.
    """
    with _quiet():
        return vars(spec_edit.remove_object(_spec_path(spec), id))


@mcp.tool()
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

    A render that logs an error fails rather than returning the picture, since
    an asset that did not load leaves its object out of a frame that otherwise
    looks right. That message is the renderer's, and is not about the spec.

    Args:
        spec: Path to the task or scene YAML.
        views: Which views to draw. Defaults to top and front.
        size: Image size in pixels, square.
        seed: Placement seed, as for `describe_scene`.
    """
    path = _spec_path(spec)
    views = views or ["top", "front"]
    if unknown := [view for view in views if view not in render.VIEWS]:
        raise ValueError(f"unknown view(s) {unknown}, expected any of {list(render.VIEWS)}")

    if ctx:
        await ctx.info(f"resolving {path.name} (seed {seed})")
    payload = await asyncio.to_thread(_pack, path, seed)

    if ctx:
        await ctx.info(f"drawing {', '.join(views)}")
    out_dir = _RENDERS / hashlib.sha256(str(path).encode()).hexdigest()[:16]
    paths = await asyncio.to_thread(render.render, payload, out_dir, views, size)
    return [Image(path=path) for path in paths]


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

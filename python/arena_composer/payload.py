"""The viewer payload: a resolved layout plus the geometry to draw it with.

MessagePack rather than JSON, because the geometry is megabytes of float arrays
and JSON spends most of a second formatting them as decimal text. The arrays
travel as msgpack ``bin`` blobs of little-endian ``f32``/``u32``, which the
viewer casts back in place; everything around them stays an ordinary map, so the
payload keeps JSON's habit of ignoring unknown keys and defaulting absent ones.

The layout half is the same dump ``arena_composer.resolve_layout`` produces —
that stays the human- and agent-readable artifact, and this only picks out the
fields a renderer needs.

The two halves also travel apart. An edit is answered by :func:`encode_spec`
first, which carries the editor's half and no layout at all, and by a full
payload once the solver has caught up; the viewer keeps drawing the layout it
has in between. Every message carries the whole editor half, so the panel never
has to piece one together.

A layout is followed by a second :func:`encode_spec` for the same reason it is
preceded by one: the spec a layout carries was read when its solve began, and
the editor can be several edits past that by the time the solve lands. So the
panel is answered by the messages carrying no layout, and a full payload's own
spec is read only to open one (``viewer::setup``).

The SimReady props travel apart for the opposite reason. They are a megabyte of
table that changes only when the index is rebuilt or a thumbnail lands, so
`remote` rides the message that changed it and no other; the panel keeps the
last list it was sent.

What is wrong with the spec rides with it, a line per problem at the entry it
is about, for the panel to mark that entry's card — see :func:`_problems`.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import asdict
from functools import cache
from pathlib import Path
from typing import Any

import msgpack
import numpy as np

from arena_composer.usd import Material, Node, extract, open_stage
from arena_composer.usd.shapes import box

# Roles whose USD is drawn by somebody else. An object reference is a prim
# *inside* its parent's stage, so drawing it too would double the geometry.
_UNDRAWN_ROLES = {"object_reference"}

# An asset with no USD to read is drawn as its bounding box, in a colour of its
# own: in the default grey it vanishes into a white table seen from above.
# Linear, like every colour in the payload: sRGB (0.3, 0.55, 0.9).
_STAND_IN = Material(base_color=(0.073, 0.263, 0.787))

# The spec and owners each file last read to, which is what the panel holds: it
# opens on the first layout's and takes every spec that reads after that. What a
# message about a file that no longer reads carries instead — see `encode_spec`.
_last_read: dict[Path, tuple[dict[str, Any], dict[str, str]]] = {}

# The spec each file's last solve read, and what that solve warned of: sent on
# while the file still reads to that spec — see `_problems`.
_last_solved: dict[Path, tuple[dict[str, Any], list[str]]] = {}

#: How a problem line names the entry it is about: by its path, first in the
#: line or after the file `SpecFiles.explain` names, as docs/errors.md has every
#: check write it.
_ENTRY = re.compile(
    r"(task\.subtasks\.\d+|task|embodiment|background|(?:objects|object_sets|object_references|relations)\.\d+)"
    r"(?=[.:]|$)"
)


def build_payload(dump: dict[str, Any], remote: bool = True) -> dict[str, Any]:
    """Turn a resolved-layout dump into the structure the viewer decodes.

    `layouts` is every layout the solve drew, one per sample, each with its
    verdict, for the panel to page through; `assets` is the first, which is
    all a render draws. The layouts share one geometry table, so a sample costs
    a pose per asset — and a geometry only for an object set's member that no
    other layout drew.

    `remote` carries the SimReady props along. On by default, because a payload
    written to a file is opened on its own by ``arena-viewer`` and has nothing
    else to fill the browser from; a viewer being fed already has the list.
    """
    geometries: list[list[dict[str, Any]]] = []
    by_usd: dict[tuple[str, str], int] = {}
    boxes: dict[tuple[str, str], int] = {}

    def geometry_index(asset: dict[str, Any]) -> int | None:
        """Extract the asset's USD once, however many assets share it at one pose.

        A robot is drawn at its start pose, which Arena bounds it at, rather than at
        whatever pose its file authors.
        """
        if asset.get("role") in _UNDRAWN_ROLES:
            return None
        path = asset.get("local_usd_path")
        if not path and asset["bbox_min"] == asset["bbox_max"]:
            # A light or a ground plane: nothing to draw, and a degenerate box
            # would still pull the camera's framing to where it sits.
            return None
        if not path:
            # The box it was placed by, once however many layouts stand it
            # somewhere: the viewer moves a piece the next layout shares rather
            # than building it again. It already carries the asset's scale, and
            # the viewer applies that again.
            scale = asset.get("scale", [1.0, 1.0, 1.0])
            key = asset["id"], json.dumps([asset["bbox_min"], asset["bbox_max"], scale])
            if key not in boxes:
                boxes[key] = len(geometries)
                surface = box(np.divide(asset["bbox_min"], scale), np.divide(asset["bbox_max"], scale))
                geometries.append([_encode_node(Node(asset["id"], surface, _STAND_IN))])
            return boxes[key]
        joint_pos = asset.get("joint_pos")
        key = path, json.dumps(joint_pos, sort_keys=True)
        if key not in by_usd:
            by_usd[key] = len(geometries)
            geometries.append([_encode_node(node) for node in extract(open_stage(path), joint_pos)])
        return by_usd[key]

    def placed(asset: dict[str, Any]) -> dict[str, Any]:
        """One asset where a layout puts it, and what it draws there."""
        return {
            "id": asset["id"],
            "role": asset.get("role", ""),
            "scale": _floats(asset.get("scale", [1.0, 1.0, 1.0])),
            "position_xyz": _floats(asset["position_xyz"]),
            "rotation_xyzw": _floats(asset["rotation_xyzw"]),
            "bbox_min": _floats(asset["bbox_min"]),
            "bbox_max": _floats(asset["bbox_max"]),
            "geometry": geometry_index(asset),
        }

    spec, owners = dump.get("spec") or {}, _owners(dump.get("spec_path"))
    lighting = dump.get("lighting", {})
    problems = None
    if dump.get("spec_path"):
        key = Path(dump["spec_path"]).resolve()
        # The first layout only: a later one's spec was read when its solve
        # began, and the panel may be edits past it by the time it lands.
        _last_read.setdefault(key, (spec, owners))
        _last_solved[key] = spec, list(dump.get("warnings", []))
        # What the panel opens with; it takes a later layout's from the spec
        # sent behind it. The file can have moved on since the solve read it,
        # and a change it has not solved is solved again at once.
        problems = _problems(key, spec)
    placement = dump.get("placement", {})
    # A dump of one sample carries it as `assets` alone, its verdict in the summary.
    solved = dump.get("layouts") or [{**placement, "assets": dump["assets"]}]
    layouts = [
        {
            "assets": [placed(asset) for asset in layout["assets"]],
            "success": layout.get("success"),
            "failed_checks": list(layout.get("failed_checks", [])),
        }
        for layout in solved
    ]

    return {
        "env_name": dump.get("env_name", ""),
        "assets": layouts[0]["assets"],
        "layouts": layouts,
        "seed": placement.get("seed"),
        "geometries": geometries,
        # The viewer's one stand-in light also stands in for a light the spec
        # lists, and for the lights a background's USD carries: neither is drawn.
        "lighting": {
            "auto_injected_dome_light": bool(
                lighting.get("auto_injected_dome_light")
                or lighting.get("light_assets")
                or lighting.get("background_usd_has_light")
            )
        },
        "viewer": _viewer(dump.get("viewer")),
        "spec": spec,
        "owners": owners,
        "vocabulary": _vocabulary(),
        "remote": _remote() if remote else None,
        # A layout that resolved is the answer to whatever was asked of it, so
        # this clears whatever the last refusal put in front of the form.
        "errors": [],
        "problems": problems,
    }


def _problems(spec_path: Path, spec: dict[str, Any]) -> list[dict[str, Any]] | None:
    """What is wrong with `spec`, the one `spec_path` reads to, at the entries
    the panel's cards draw: what `spec_edit.check` finds in the file, and what
    the last solve warned of, while the file still reads to the spec that solve
    read. A warning names its entry by position, which an edit since may have
    handed to another, so it waits for the solve of the spec it is about.

    A nicety: one that cannot be found is left out of the message rather than
    taking it down, since that message may be an edit's answer.
    """
    from arena_composer import spec_edit

    try:
        solved, warnings = _last_solved.get(spec_path, (None, []))
        return _attributed(spec, spec_edit.check(spec_path).errors, warnings if solved == spec else [])
    except Exception as error:
        print(f"arena-composer: problems skipped: {type(error).__name__}: {error}", file=sys.stderr)
        return None


def _attributed(spec: dict[str, Any], errors: list[str], warnings: list[str]) -> list[dict[str, Any]]:
    """`errors` and `warnings`, lines about `spec`, each at the path of the
    entry it is about: the path its card in the panel is keyed by,
    ``["relations", 2]``.

    A line names its entry by path, ``relations.2.params.side: ...``, or by
    id, ``bowl: ...``, as the solve's warnings do. One that names neither, or
    names a key with no card, such as ``placer_params``, is left out: the panel
    has nowhere to put it.
    """
    ids = {node["id"]: [key] for key in ("embodiment", "background") if isinstance(node := spec.get(key), dict)}
    for key in ("objects", "object_sets", "object_references"):
        ids.update((entry["id"], [key, index]) for index, entry in enumerate(spec.get(key) or []))
    found = []
    for warning, lines in ((False, errors), (True, warnings)):
        for line in lines:
            # The second, after the file a line names first where the spec is two.
            for head in line.split(": ")[:2]:
                # A file is no entry, however it is named: `task.yaml` reads as
                # the task's path, and `.yaml` as a key under it.
                if "/" in head or head.endswith((".yaml", ".yml")):
                    continue
                if match := _ENTRY.match(head):
                    path = [int(step) if step.isdigit() else step for step in match[1].split(".")]
                elif head in ids:
                    path = ids[head]
                else:
                    continue
                found.append({"path": path, "line": line, "warning": warning})
                break
    return found


def _owners(spec_path: str | None) -> dict[str, str]:
    """Which of the files holds each top-level key: ``task`` or ``scene``, or
    with more than one include, the file's name; ``scene + task`` for a key
    Arena merges across files.

    The editor shows it, because which file a key lives in is not visible
    from the merged spec the form edits.
    """
    if not spec_path:
        return {}
    from arena_composer.spec_edit import INCLUDE, SpecFiles

    files = SpecFiles(Path(spec_path))

    def name(path: Path) -> str:
        if path == files.path:
            return "task"
        return "scene" if len(files.spellings) == 1 else Path(files.spellings[path]).name

    return {
        key: " + ".join(name(path) for path in files.merge_order if key in files.documents[path])
        for document in files.documents.values()
        for key in document
        if key != INCLUDE
    }


def _vocabulary() -> dict[str, Any]:
    """Everything registered with Arena that a spec may name: the assets, the
    relation and task kinds with the form that edits each, and the task
    compositions a root task may pick.

    Sent whole rather than searched. It is ~30 kB against megabytes of
    geometry, and a form filters faster than a round trip.
    """
    from arena_composer import catalog

    return {"assets": [_asset(asset) for asset in catalog.catalog()], **kinds()}


@cache
def kinds() -> dict[str, Any]:
    """The relation and task kinds, each with the form that edits it, and the
    task composition names Arena's ``CompositeTaskSpec`` accepts. Cached: a
    form is inferred by introspecting a constructor, and no registry changes
    while the process runs.

    The assets are not cached with them, because an asset's picture arrives
    after the asset does — see :func:`_thumbnail`. The MCP server's
    `list_kinds` returns this as it is.
    """
    from isaaclab_arena.environment_spec.arena_env_graph_types import TaskCompositionType

    from arena_composer import catalog

    return {
        "relations": [asdict(kind) for kind in catalog.relations()],
        "tasks": [asdict(kind) for kind in catalog.tasks()],
        "compositions": [value.value for value in TaskCompositionType],
    }


def _remote() -> list[dict[str, Any]]:
    """The SimReady props the local index knows. Empty until it is built."""
    from arena_composer import catalog

    return [_asset(asset) for asset in catalog.remote()]


def _asset(asset: Any) -> dict[str, Any]:
    """One asset as the browser lists it.

    `params` is what an entry naming it has to carry beyond the registry name —
    empty for a registered asset, the URL for a SimReady prop — so the panel
    writes an entry that builds without knowing which kind it picked. The URL
    is given again as `usd_path` whether `params` carries it or not, because an
    asset is searched by it and the browser shows it. `category` is a prop's
    folder under the SimReady root, where the browser's tree files it, and
    `credit` the attribution a SimReady Central prop's license asks for, which
    the browser shows beside it.
    """
    return {
        "registry_name": asset.registry_name,
        "role": asset.role,
        "tags": asset.tags,
        "object_type": asset.object_type,
        "name": asset.name,
        "usd_path": asset.usd_path or "",
        "thumbnail": _thumbnail(asset.usd_path),
        "params": asset.params,
        "category": asset.category,
        "credit": asset.credit,
    }


def _thumbnail(usd_path: str | None) -> str:
    """The asset's picture as a local file, or empty where none is on disk.

    A path and not a URL: the viewer draws from disk and opens no socket, and
    asks for nothing — what is fetched is fetched before the list is sent. Both
    kinds of asset are looked up the same way: the bucket keeps a picture
    beside a layer, whoever published it, and an asset that composes its USD
    has no layer and so no picture.
    """
    from arena_composer.simready import picture

    return picture(usd_path or "") or ""


def encode(dump: dict[str, Any], remote: bool = True) -> bytes:
    """Build and serialize the payload."""
    return msgpack.packb(build_payload(dump, remote), use_bin_type=True)


def encode_spec(
    spec_path: Path,
    errors: list[str] | None = None,
    solving: bool | None = None,
    remote: bool = False,
    indexing: bool | None = None,
) -> bytes:
    """Pack the editor's half alone: the spec as written, with no layout.

    What answers an edit the moment it is on disk. :mod:`arena_composer.spec_edit`
    has already validated and written it in milliseconds, where resolving the
    layout it implies takes seconds, so the panel is answered from the file and
    the viewer keeps drawing what it has until the solve catches up. `solving`
    is what says one is coming.

    The spec is read back rather than carried over from the edit, so this is the
    same document :func:`build_payload` would report — validated, with every
    default filled in.

    `remote` sends the SimReady props with it, which is how a crawl's answer and
    newly fetched pictures reach the panel. Off by default: nothing else changes
    that list, and it is a megabyte.

    `errors`, `solving` and `indexing` are each left out by a message that has
    nothing to say about them, and the panel keeps the last answer it had, the
    way it keeps the last prop list. A message about assets knows nothing of
    the last edit or the solve behind it, so sending it must not clear a
    refusal or stop a spinner. Pass ``[]`` to say an edit landed, and
    ``False`` to say no solve is coming.

    A file that no longer reads — a hand edit that is not YAML, or that Arena
    will not validate — has no editor half of its own. A message with `errors`
    to carry goes out on the one it last read to instead, which is the one the
    panel holds, so the reason reaches the panel and the form stays as it is.
    A message with nothing to say raises, as it always has: the next one to
    read says it.

    `problems` are what is wrong with the spec sent, at the entries the panel's
    cards draw (:func:`_problems`). Left out with a spec the file no longer
    reads to: the panel keeps the ones it has, which are that spec's.
    """
    from isaaclab_arena.environment_spec.arena_env_graph_spec import ArenaEnvGraphSpec

    key = Path(spec_path).resolve()
    problems = None
    try:
        _last_read[key] = ArenaEnvGraphSpec.from_yaml(spec_path).model_dump(mode="json"), _owners(str(spec_path))
    except Exception:
        if not errors or key not in _last_read:
            raise
    else:
        problems = _problems(key, _last_read[key][0])
    spec, owners = _last_read[key]

    return msgpack.packb(
        {
            "spec": spec,
            "owners": owners,
            "vocabulary": _vocabulary(),
            "remote": _remote() if remote else None,
            "indexing": indexing,
            "errors": None if errors is None else list(errors),
            "solving": solving,
            "problems": problems,
        },
        use_bin_type=True,
    )


def _encode_node(node: Node) -> dict[str, Any]:
    surface, material = node.surface, node.material
    return {
        "path": node.path,
        "positions": _blob(surface.positions, np.float32),
        "normals": _blob(surface.normals, np.float32),
        "uvs": _blob(surface.uvs, np.float32),
        "indices": _blob(surface.triangles, np.uint32),
        "base_color": _floats(material.base_color),
        "opacity": float(material.opacity),
        "metallic": float(material.metallic),
        "roughness": float(material.roughness),
        "emissive": _floats(material.emissive),
        "double_sided": bool(material.double_sided),
        "textures": dict(material.textures),
        "linear": list(material.linear),
    }


def _blob(array: np.ndarray | None, dtype: Any) -> bytes | None:
    """A C-contiguous little-endian view of an array, as raw bytes."""
    if array is None or array.size == 0:
        return None
    return np.ascontiguousarray(array, dtype=np.dtype(dtype).newbyteorder("<")).tobytes()


def _floats(values: Any) -> list[float]:
    return [float(value) for value in values]


def _viewer(viewer: dict[str, Any] | None) -> dict[str, list[float]] | None:
    if not viewer:
        return None
    return {"eye": _floats(viewer["eye"]), "lookat": _floats(viewer["lookat"])}

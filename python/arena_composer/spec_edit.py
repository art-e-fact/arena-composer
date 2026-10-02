"""Structured edits to an ArenaEnvGraphSpec YAML, validated before they land.

The agent editing these files already has Read and Edit, and for reading a spec
those are better than anything here — the YAML is short and exact. What it does
not have is the two things this module adds:

* **Where an edit belongs.** A task spec includes a scene through
  ``external_yaml``, and the two are a disjoint union: ``objects`` and
  ``relations`` live in exactly one of the files. An edit goes to whichever file
  owns the key.
* **What an edit drags with it.** Removing an object also has to remove the
  relations that name it, and renaming one — an object's, the embodiment's, or
  the background's — has to carry them across, or the spec stops loading with a
  dangling reference. Switching the embodiment's or the background's
  ``registry_name`` has to shed the params the new one does not take, or the
  spec stops building.

Every edit is checked against the merged spec before either file is written, so
a rejected edit leaves the pair on disk exactly as it was.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from ruamel.yaml import YAML

#: Key through which a task spec includes its scene.
INCLUDE = "external_yaml"

#: Strings that have to go out quoted.
#:
#: Arena loads these files with PyYAML, which reads YAML 1.1, where a bare
#: ``on`` is the boolean true. ruamel writes YAML 1.2, where it is the string
#: it looks like. Write the ``on`` relation kind unquoted and it comes back a
#: bool, and the spec stops loading.
YAML_11_BOOLEANS = frozenset("y yes n no true false on off".split())


def _represent_str(representer: Any, value: str) -> Any:
    """Quote a string YAML 1.1 would otherwise read as a boolean."""
    style = "'" if value.lower() in YAML_11_BOOLEANS else None
    return representer.represent_scalar("tag:yaml.org,2002:str", value, style=style)


def _represent_none(representer: Any, _value: None) -> Any:
    """Write None as ``null``, which is both what Arena's own writer emits and
    what these specs are authored with. ruamel's default is an empty value,
    which would rewrite every ``reference: null`` line an edit passes over."""
    return representer.represent_scalar("tag:yaml.org,2002:null", "null")


@dataclass
class Result:
    """What an edit did, or why it did nothing.

    Returned rather than raised: a spec an agent got wrong is the normal case
    and the message is the useful half, so it travels as data it can act on.
    """

    ok: bool
    #: Files actually rewritten.
    wrote: list[str] = field(default_factory=list)
    #: One line per change, in the order they were made.
    changed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @classmethod
    def failed(cls, *errors: str) -> Result:
        return cls(ok=False, errors=list(errors))


def check_entry(path: Path) -> None:
    """Fail with a way to find its task when `path` is a scene: a file with
    neither a ``task`` nor an include of its own.

    A scene validates only merged with a task that includes it, and several
    tasks can include one scene, so which to load is the caller's to name. The
    message ends with a regex for the include line, to search the specs with.

    A file that does not read as YAML is not known to be a scene, and is left
    to whoever reads it next — an edit, or Arena's loader — which refuses it
    naming the file and the line.
    """
    try:
        document = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return
    if isinstance(document, dict) and "task" not in document and INCLUDE not in document:
        pattern = rf"""{INCLUDE}:\s*['"]?(.*/)?{re.escape(Path(path).name)}"""
        raise ValueError(
            f"{path} has no `task`, so it is a scene: pass the task YAML that includes it through `{INCLUDE}`."
            f" To find one, search the specs for the regex {pattern}"
        )


class SpecFiles:
    """A spec and the scene it includes, held as round-trip YAML documents.

    Loaded together because that is the unit validation applies to: a change to
    the scene can only be judged against the task that includes it.
    """

    def __init__(self, path: Path) -> None:
        self.yaml = YAML()
        self.yaml.preserve_quotes = True
        # ruamel wraps at 80 columns by default, which would refold every task
        # description longer than that — leaving a trailing space at the break —
        # the first time any edit passes over the file. An edit rewrites only
        # what it was asked to.
        self.yaml.width = 4096
        self.yaml.representer.add_representer(str, _represent_str)
        self.yaml.representer.add_representer(type(None), _represent_none)
        self.path = Path(path).resolve()
        self.documents: dict[Path, Any] = {self.path: self._read(self.path)}

        include = self.documents[self.path].get(INCLUDE)
        self.include_path = (self.path.parent / include).resolve() if include else None
        if self.include_path:
            self.documents[self.include_path] = self._read(self.include_path)

    def _read(self, path: Path) -> Any:
        with path.open(encoding="utf-8") as handle:
            document = self.yaml.load(handle)
        if not isinstance(document, dict):
            raise ValueError(f"{path}: env graph spec must be a mapping")
        return document

    def owner(self, key: str) -> Path:
        """The file that holds `key`, or the one an edit should add it to.

        The include is a disjoint union — Arena refuses a key present in both —
        so at most one file can own it. A key nobody has goes next to
        ``objects``, which is where a scene's other lists already are.
        """
        for path, document in self.documents.items():
            if key in document:
                return path
        if self.include_path and "objects" in self.documents[self.include_path]:
            return self.include_path
        return self.path

    def section(self, key: str) -> list[Any]:
        """The list stored under `key`, created in its owning file if absent."""
        document = self.documents[self.owner(key)]
        if not document.get(key):
            document[key] = []
        return document[key]

    def merged(self, documents: dict[Path, Any] | None = None) -> dict[str, Any]:
        """`documents` as Arena would merge them, ready to validate."""
        # Arena's own merge, so the include's rules stay defined in one place.
        from isaaclab_arena.environment_spec.arena_env_graph_yaml_loader import _merge_env_graph_dicts

        documents = self.documents if documents is None else documents
        entry = {key: value for key, value in documents[self.path].items() if key != INCLUDE}
        if not self.include_path:
            return entry
        return _merge_env_graph_dicts(dict(documents[self.include_path]), entry)

    def validate(self, texts: dict[Path, str]) -> list[str]:
        """Problems with the spec `texts` would produce, empty when it is good.

        Checked by reading `texts` back with PyYAML rather than by validating
        the documents in memory, because PyYAML is what Arena will read the
        file with and the two do not always agree — see ``YAML_11_BOOLEANS``.
        A spec that only validates before it is written is not validated.
        """
        from isaaclab_arena.environment_spec.arena_env_graph_spec import ArenaEnvGraphSpec
        from pydantic import ValidationError

        try:
            reloaded = {path: yaml.safe_load(text) for path, text in texts.items()}
            spec = ArenaEnvGraphSpec.from_dict(self.merged(reloaded))
        except ValidationError as error:
            # Not `str(error)`: that quotes the whole merged spec back as the
            # offending input, burying the one sentence worth reading.
            return [_message(entry) for entry in error.errors()]
        except Exception as error:
            # Arena asserts outside pydantic too, e.g. on a duplicate key
            # across the include.
            return [f"{type(error).__name__}: {error}"]

        # Arena checks that a registry name exists, not what the asset behind
        # it takes; an entry short of a required parameter fails only when the
        # spec is built.
        from isaaclab_arena.assets.registries import AssetRegistry
        from isaaclab_arena.embodiments.embodiment_base import EmbodimentBase

        from arena_composer import catalog

        errors = [
            f"objects.{index}.params: {obj.registry_name} needs {', '.join(missing)}"
            for index, obj in enumerate(spec.objects)
            if (missing := catalog.missing_params(obj.registry_name, obj.params))
        ]
        # Nor that the asset is the kind its slot holds: a banana validates as
        # the embodiment, and fails when the scene is built.
        if not issubclass(AssetRegistry().get_asset_by_name(spec.embodiment.registry_name), EmbodimentBase):
            errors.append(f"embodiment.registry_name: {spec.embodiment.registry_name} is not an embodiment")
        return errors

    def write(self, changed: list[str]) -> Result:
        """Validate the text an edit produced, then write the files it changes."""
        texts = {path: self._dump(document) for path, document in self.documents.items()}
        if errors := self.validate(texts):
            return Result.failed(*errors)

        wrote = []
        for path, text in texts.items():
            if text != path.read_text(encoding="utf-8"):
                path.write_text(text, encoding="utf-8")
                wrote.append(str(path))
        return Result(ok=True, wrote=wrote, changed=changed)

    def _dump(self, document: Any) -> str:
        buffer = io.StringIO()
        self.yaml.dump(document, buffer)
        return buffer.getvalue()

    def asset_ids(self) -> list[str]:
        """Every id an edit can refer to, in the order the spec declares them."""
        spec = self.merged()
        ids = [spec.get("embodiment", {}).get("id"), spec.get("background", {}).get("id")]
        for key in ("objects", "object_sets", "object_references"):
            ids += [entry.get("id") for entry in (spec.get(key) or [])]
        return [asset_id for asset_id in ids if asset_id]


def _files(path: Path) -> SpecFiles | Result:
    """`path` and its scene, or the refusal to make of a pair that does not read.

    A refusal rather than an exception, for the reason :class:`Result` is one:
    a file broken by hand mid-session — not YAML, not a mapping, an include
    that is not there — is the normal case, the message names the file and
    the line, and the loop serving the editor panel has to outlive it.
    """
    try:
        return SpecFiles(path)
    except Exception as error:
        return Result.failed(f"{type(error).__name__}: {error}")


def _message(entry: dict[str, Any]) -> str:
    """One pydantic error as a line an agent can act on."""
    # Arena's validators are bare `assert`s, which pydantic reports under this
    # prefix; the sentence after it is the one the author wrote.
    message = entry["msg"].removeprefix("Assertion failed, ")
    location = ".".join(str(part) for part in entry["loc"])
    return f"{location}: {message}" if location else message


def add_object(
    path: Path,
    node_id: str,
    registry_name: str,
    on: str | None = None,
    params: dict[str, Any] | None = None,
) -> Result:
    """Add an object to `path`'s scene, optionally resting on another asset.

    Without `on` the object takes no part in placement and lands at the origin,
    which for most scenes means inside the floor. It stays optional because an
    anchored scene may want the object placed by a relation added separately.

    `params` is what the asset needs beyond its name — a SimReady prop's
    `usd_path`, as the catalogue reports it. `registry_name` is checked by
    Arena, not here: its own field validator rejects a name the asset registry
    does not know. What the asset then needs is checked in
    :meth:`SpecFiles.validate`, before the file is written.
    """
    files = _files(path)
    if isinstance(files, Result):
        return files
    known = files.asset_ids()
    if node_id in known:
        return Result.failed(f"id {node_id!r} is already used in this spec")
    if on is not None and on not in known:
        return Result.failed(f"no asset {on!r} to put {node_id!r} on; this spec has {known}")

    files.section("objects").append({"id": node_id, "registry_name": registry_name, "params": dict(params or {})})
    changed = [f"added object {node_id!r} ({registry_name})"]
    if on is not None:
        files.section("relations").append({"kind": "on", "subject": node_id, "reference": on, "params": {}})
        changed.append(f"added relation {node_id!r} on {on!r}")
    return files.write(changed)


#: Sections whose entries own an id a relation may name. Removing from one of
#: these has to take the relations naming it too.
_ID_SECTIONS = ("objects", "object_sets", "object_references")


def apply(path: Path, op: str, steps: list[Any], value: Any = None) -> Result:
    """Apply one edit addressed at a path into the merged spec.

    This is the editor's whole write surface. `steps` walks the spec the way it
    reads — ``["relations", 2, "params", "side"]`` — and `op` is one of:

    * ``set`` — replace the leaf `steps` names. Setting an entry's ``id`` — an
      object's, or the embodiment's or background's own — renames it in the
      relations and task params that name it too, and setting the embodiment's
      or background's ``registry_name`` drops the params the new one does not
      take.
    * ``insert`` — append `value` to the list `steps` names. Appending a second
      subtask to an ``atomic`` task switches its composition to ``sequential``,
      since ``atomic`` holds only one.
    * ``remove`` — drop what `steps` names: an entry, with any relation left
      dangling, or a parameter, which hands it back to the kind's default.
      Dropping a task down to one subtask switches its composition back to
      ``atomic``.

    One leaf at a time rather than whole sections, so a key the editor's form
    never shows is not dropped by a write that passes over it. Which file the
    edit lands in, and whether the result validates, are :class:`SpecFiles`'
    problem — a rejected edit leaves both files as they were.
    """
    if not steps:
        return Result.failed("an edit needs a path")

    files = _files(path)
    if isinstance(files, Result):
        return files
    key = str(steps[0])
    document = files.documents[files.owner(key)]

    value = _ordered(value)

    try:
        if op == "insert":
            # A single step names a top-level list, which may not exist yet.
            target = files.section(key) if len(steps) == 1 else _walk(document, steps)
            target.append(value)
            changed = [f"added an entry to {_label(steps)}"]
            if steps == ["task", "subtasks"] and (bumped := _keep_composition_valid(files)):
                changed.append(bumped)
            return files.write(changed)

        parent, last = _walk(document, steps[:-1]), steps[-1]
        if op == "set":
            previous = parent.get(last) if isinstance(parent, dict) else None
            parent[last] = value
            changed = [f"set {_label(steps)} to {value!r}"]
            # An id is a name other entries hold, so renaming it has to carry
            # them — the same reason removing it has to drop them. An object's
            # id sits under a list entry (`objects.2.id`); the embodiment's and
            # background's sit directly on the singleton mapping, so they are
            # matched by their exact two-step path rather than by `last`, or a
            # param happening to be called `id` would trigger this too.
            renames_id = (last == "id" and key in _ID_SECTIONS) or steps in (
                ["embodiment", "id"],
                ["background", "id"],
            )
            if renames_id and previous and previous != value:
                if renamed := _rename_node(files, previous, value):
                    changed.append(f"renamed {previous!r} in {renamed} place(s)")
            if steps in (["embodiment", "registry_name"], ["background", "registry_name"]) and (
                dropped := _drop_params_not_taken(parent, value)
            ):
                changed.append(f"dropped {key}.params {', '.join(dropped)}, which {value} does not take")
            return files.write(changed)
        if op == "remove":
            changed = [f"removed {_label(steps)}"]
            # Capture the id before the entry goes, so the cascade can find the
            # relations that named it. Only a whole entry — `objects.2` — owns
            # one; a parameter whose value is a mapping with an `id` does not.
            names_entry = key in _ID_SECTIONS and len(steps) == 2 and isinstance(parent[last], dict)
            node_id = parent[last].get("id") if names_entry else None
            del parent[last]
            # ruamel writes a mapping emptied of a commented key at column 0,
            # which does not parse. A fresh one writes as `{}`.
            if isinstance(parent, dict) and not parent and len(steps) > 1:
                _walk(document, steps[:-2])[steps[-2]] = {}
            if node_id and (dropped := _drop_relations_naming(files, node_id)):
                changed.append(f"removed {dropped} relation(s) naming {node_id!r}")
            if steps[:2] == ["task", "subtasks"] and len(steps) == 3 and (bumped := _keep_composition_valid(files)):
                changed.append(bumped)
            return files.write(changed)
    except (KeyError, IndexError, TypeError, AttributeError) as error:
        return Result.failed(f"{_label(steps)}: {type(error).__name__}: {error}")

    return Result.failed(f"unknown edit {op!r}, expected set, insert or remove")


#: The order an entry's keys read best in, ahead of anything unlisted.
_KEY_ORDER = (
    "id",
    "registry_name",
    "kind",
    "subject",
    "reference",
    "parent_id",
    "prim_path",
    "object_type",
    "members",
    "random_choice",
    "composition",
    "description",
    "subtasks",
    "params",
)


def _ordered(value: Any) -> Any:
    """A mapping in the order a spec reads best.

    An edit arrives over MessagePack, where a mapping has whatever order the
    sender's map iterated in. These files are read by people, so a written
    entry gets a fixed key order instead of a fresh one each time.
    """
    if not isinstance(value, dict):
        return value
    rank = {key: index for index, key in enumerate(_KEY_ORDER)}
    return {
        key: _ordered(inner)
        for key, inner in sorted(value.items(), key=lambda item: (rank.get(item[0], len(rank)), item[0]))
    }


def _walk(document: Any, steps: list[Any]) -> Any:
    """The value `steps` names, starting from `document`."""
    for step in steps:
        document = document[step]
    return document


def _label(steps: list[Any]) -> str:
    """A path as it reads in the spec, for a change line or an error."""
    return ".".join(str(step) for step in steps)


def _drop_relations_naming(files: SpecFiles, node_id: str) -> int:
    """Remove every relation with `node_id` as subject or reference.

    A relation pointing at an asset that has gone stops the spec loading at
    all, so removing the asset has to remove them in the same edit.
    """
    relations = files.section("relations")
    dropped = [r for r in relations if node_id in (r.get("subject"), r.get("reference"))]
    for relation in dropped:
        relations.remove(relation)
    return len(dropped)


def _rename_node(files: SpecFiles, previous: str, node_id: str) -> int:
    """Point everything naming `previous` at `node_id` instead.

    A name left behind is a reference to an asset that no longer exists, which
    stops the spec loading — the same failure removing an asset without its
    relations causes. Two places hold one: a relation's ``subject`` and
    ``reference``, and a task param, since Arena requires every string-valued
    task param to name a node.

    Task params go with a rename and not with a remove, which leaves them
    alone deliberately: the node a rename renames still exists.
    """
    renamed = 0
    for relation in files.section("relations"):
        for slot in ("subject", "reference"):
            if relation.get(slot) == previous:
                relation[slot] = node_id
                renamed += 1

    task = files.documents[files.owner("task")].get("task") or {}
    for subtask in task.get("subtasks") or []:
        params = subtask.get("params") or {}
        for name in [name for name, value in params.items() if value == previous]:
            params[name] = node_id
            renamed += 1
    return renamed


def _keep_composition_valid(files: SpecFiles) -> str | None:
    """Fit `task.composition` to its current subtask count, and say so — or
    ``None`` if it already fits.

    ``CompositeTaskSpec`` requires exactly one subtask for ``atomic`` and at
    least two for ``sequential``/``parallel``. Edits are validated one at a
    time, so without this a task could never grow past one subtask: inserting
    a second is refused while still ``atomic``, and switching composition
    first is refused while there is only one. Bumping the count and the
    composition together in the same edit keeps every step on its own valid.
    """
    task = files.documents[files.owner("task")].get("task") or {}
    subtasks = task.get("subtasks") or []
    if task.get("composition") == "atomic" and len(subtasks) >= 2:
        task["composition"] = "sequential"
        return "set task.composition to 'sequential': 'atomic' holds exactly one subtask"
    if task.get("composition") != "atomic" and len(subtasks) == 1:
        task["composition"] = "atomic"
        return "set task.composition to 'atomic': one subtask cannot be sequential or parallel"
    return None


def _drop_params_not_taken(entry: Any, registry_name: Any) -> list[str]:
    """Remove the params in `entry` that `registry_name`'s constructor does not
    take, and name them.

    An entry's params belong to its constructor, and one passed to a
    constructor that does not take it fails the build. What the new one also
    takes stays: ``enable_cameras`` on any switch, a stand's height between two
    DROID variants. A param is matched by name, not meaning, so an
    ``initial_joint_pose`` carries over to a robot with other joints.
    """
    from isaaclab_arena.assets.registries import AssetRegistry

    from arena_composer import catalog

    params = entry.get("params")
    # An unregistered name is left to validation, which refuses it by name.
    if not params or not isinstance(registry_name, str) or not AssetRegistry().is_registered(registry_name):
        return []
    dropped = catalog.unexpected_params(registry_name, params)
    for name in dropped:
        del params[name]
    # As in `apply`'s remove: an emptied commented mapping does not parse.
    if not params:
        entry["params"] = {}
    return dropped


def remove_object(path: Path, node_id: str) -> Result:
    """Remove an object from `path`'s scene, along with the relations naming it.

    Relations go with it because a relation pointing at a missing asset stops
    the spec loading at all. Task params naming it are left alone deliberately:
    a task whose target has gone is a decision about the task, not a reference
    to tidy up, and the validation error says so.
    """
    files = _files(path)
    if isinstance(files, Result):
        return files
    objects = files.section("objects")
    matched = [entry for entry in objects if entry.get("id") == node_id]
    if not matched:
        return Result.failed(f"no object {node_id!r} in this spec; it has {files.asset_ids()}")

    for entry in matched:
        objects.remove(entry)
    changed = [f"removed object {node_id!r}"]

    if dropped := _drop_relations_naming(files, node_id):
        changed.append(f"removed {dropped} relation(s) naming {node_id!r}")

    return files.write(changed)



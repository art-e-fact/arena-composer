"""Structured edits to an ArenaEnvGraphSpec YAML, validated before they land.

The agent editing these files already has Read and Edit, and for reading a spec
those are better than anything here — the YAML is short and exact. What it does
not have is the two things this module adds:

* **Where an edit belongs.** A task spec includes a scene through
  ``external_yaml``, and the two are a disjoint union: ``objects`` and
  ``relations`` live in exactly one of the files. An edit goes to whichever file
  owns the key.
* **What an edit drags with it.** Removing an object also has to remove the
  relations that name it and the object references inside it, and renaming one
  — an object's, the embodiment's, or the background's — has to carry every
  slot that holds its id (see :func:`_id_slots`), or the spec stops loading
  with a dangling reference. Switching the embodiment's or the background's
  ``registry_name`` has to shed the params the new one does not take, or the
  spec stops building.

Every edit is checked against the merged spec before either file is written, so
a rejected edit leaves the pair on disk exactly as it was. A spec that is already
broken is refused only for what the edit introduces — see :func:`_introduced` —
or an agent with two problems to fix could fix neither.
"""

from __future__ import annotations

import difflib
import inspect
import io
import os
import re
import shutil
import sys
import tempfile
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from ruamel.yaml import YAML
from ruamel.yaml.representer import RoundTripRepresenter

#: Key through which a task spec includes its scene.
INCLUDE = "external_yaml"

#: The placement seed every command and tool solves with unless told otherwise.
DEFAULT_SEED = 42


class SpecError(Exception):
    """A spec that validates but names an asset the sim could not use. One
    line per asset, since a scene wrong in one place is often wrong in two.

    Raised by :mod:`arena_composer.resolve_layout`, and defined here with
    ``DEFAULT_SEED`` so the MCP server and ``arena-view`` can name both at
    import without paying for Arena's solver before their first solve.
    """

    def __init__(self, errors: list[str]) -> None:
        super().__init__("\n".join(errors))
        self.errors = errors


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


def _represent_float(representer: Any, value: float) -> Any:
    """Write an exponent with a dot before it, ``1.0e-05``, which PyYAML reads
    as the float it is. ruamel writes ``1e-05``, valid YAML 1.2 but a string to
    YAML 1.1: a task param refused as an unknown node, a relation param wrong
    without a word. Only for a float an edit adds; one read from the file keeps
    the spelling it has."""
    node = RoundTripRepresenter.represent_float(representer, value)
    mantissa, exponent, power = node.value.partition("e")
    if exponent and "." not in mantissa:
        node.value = f"{mantissa}.0e{power}"
    return node


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
    #: What refused the edit: the problems it would introduce.
    errors: list[str] = field(default_factory=list)
    #: What was wrong with the spec before the edit and still is, whether the
    #: edit landed or not. `ok` says only whether the edit landed.
    remaining_errors: list[str] = field(default_factory=list)

    @classmethod
    def failed(cls, *errors: str) -> Result:
        return cls(ok=False, errors=list(errors))


def check(path: Path) -> Result:
    """Validate the spec at `path` as Arena will read it, writing nothing.

    The check every edit passes before it lands, on the text as it is on
    disk — so a hand edit is judged by the bytes Arena's YAML 1.1 reader will
    see, a bare ``on`` included — and the same :class:`Result`: `ok` with
    nothing in `errors` for a spec that validates.
    """
    files = _files(path)
    if isinstance(files, Result):
        return files
    errors = files.validate({file: file.read_text(encoding="utf-8") for file in files.documents})
    return Result(ok=not errors, errors=errors)


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
        # Class-wide: ruamel keeps representers on the class, so every round-trip
        # YAML in this process writes these three types this way.
        self.yaml.representer.add_representer(str, _represent_str)
        self.yaml.representer.add_representer(float, _represent_float)
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
        merged = self._read_back(texts)
        return merged if isinstance(merged, list) else self.explain(self._problems(merged), merged)

    def _read_back(self, texts: dict[Path, str]) -> dict[str, Any] | list[str]:
        """`texts` merged as Arena reads them, or the one line saying why they
        do not read."""
        try:
            return self.merged({path: yaml.safe_load(text) for path, text in texts.items()})
        except Exception as error:
            return [f"{type(error).__name__}: {error}"]

    def _problems(self, merged: dict[str, Any]) -> list[str]:
        """What Arena's validator refuses in `merged`, then what it leaves to
        the build and this module checks for it — off the merged dict, so a
        spec the validator refuses is checked for those too, and an edit on
        it is judged on both (see :func:`_introduced`)."""
        from isaaclab_arena.environment_spec.arena_env_graph_spec import ArenaEnvGraphSpec
        from pydantic import ValidationError

        errors = []
        try:
            ArenaEnvGraphSpec.from_dict(merged)
        except ValidationError as error:
            # Not `str(error)`: that quotes the whole merged spec back as the
            # offending input, burying the one sentence worth reading.
            errors = [_message(entry) for entry in error.errors()]
        except Exception as error:
            # Arena asserts outside pydantic too, e.g. on a duplicate key
            # across the include.
            errors = [f"{type(error).__name__}: {error}"]
        return errors + _build_problems(merged)

    def write(self, changed: list[str]) -> Result:
        """Validate the text an edit produced, then write the files it changes.

        A spec that was good before is refused for anything wrong after. One
        that was already broken is refused only for what the edit introduces,
        and what was wrong before travels as `remaining_errors` — on the
        refusal, and on the edit that landed.
        """
        texts = {path: self._dump(document) for path, document in self.documents.items()}
        after_spec = self._read_back(texts)
        if isinstance(after_spec, list):
            return Result.failed(*after_spec)
        introduced, remaining = self._problems(after_spec), []
        if introduced:
            before_spec = self._read_back({path: path.read_text(encoding="utf-8") for path in self.documents})
            before = [] if isinstance(before_spec, list) else self._problems(before_spec)
            if before:
                introduced, remaining = _introduced(before_spec, after_spec, before, introduced)
        remaining = self.explain(remaining, after_spec)
        if introduced:
            return Result(ok=False, errors=self.explain(introduced, after_spec), remaining_errors=remaining)

        wrote = []
        for path, text in texts.items():
            if text != path.read_text(encoding="utf-8"):
                try:
                    _replace(path, text)
                except OSError as error:
                    # The pair is replaced a file at a time, so one may
                    # already be: `wrote` says which.
                    return Result(ok=False, wrote=wrote, errors=[f"{type(error).__name__}: {error}"])
                wrote.append(str(path))
        return Result(ok=True, wrote=wrote, changed=changed, remaining_errors=remaining)

    def explain(self, lines: list[str], spec: Any) -> list[str]:
        """`lines`, the problems with the merged `spec`, as an agent can act on
        them: each starts with the file to fix, where the spec is two, spelled
        as the include spells it; and a name, kind or id nothing declares
        comes with the closest that are — or, for a name nothing registered
        is close to, with how to import what registers it.
        """
        explained = []
        for line in lines:
            if hint := _hint(line, spec):
                # Arena's own lines are sentences; the hint follows as one.
                line = f"{line} {hint[0].upper()}{hint[1:]}" if line.endswith(".") else f"{line}; {hint}"
            explained.append(self._file_of(line) + line)
        return explained

    def _file_of(self, line: str) -> str:
        """The prefix naming the file that owns what `line` is about, or none."""
        if not self.include_path:
            return ""
        from isaaclab_arena.environment_spec.arena_env_graph_spec import ArenaEnvGraphSpec

        key = next((key for pattern, key in _SECTIONS if pattern.match(line)), None)
        if key is None and (match := re.match(r"(\w+)[.:]", line)):
            key = match[1]
        if key not in ArenaEnvGraphSpec.model_fields:
            return ""
        owner = self.owner(key)
        return f"{self.path.name if owner == self.path else self.documents[self.path][INCLUDE]}: "

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


def _replace(path: Path, text: str) -> None:
    """Write `text` over `path` whole: into a file beside it, then renamed over
    it, so a reader — `arena-view`'s watcher, Arena, an editor — finds the old
    file or the new one, never half of either."""
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        shutil.copymode(path, temporary)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


#: Arena's lines that name no path, and the key each is about.
_SECTIONS = (
    (re.compile(r"Relation\["), "relations"),
    (re.compile(r"Object reference '"), "object_references"),
    (re.compile(r"Task '"), "task"),
    (re.compile(r"CLI override '|Duplicate cli_override"), "cli_override_specs"),
)

#: A name or id an error line says nothing declares, and where to look for the
#: ones that are: a registry, or the spec's own ids.
_UNKNOWN = (
    (re.compile(r"Unknown asset registry_name '([^']*)'"), "asset"),
    (re.compile(r"Unknown relation kind '([^']*)'"), "relation"),
    (re.compile(r"Unknown task kind '([^']*)'"), "task"),
    (re.compile(r"invalid parent '([^']*)'"), "object_references"),
    (re.compile(r"non-swappable asset '([^']*)'"), "cli_override_specs"),
    (re.compile(r"unknown (?:subject|reference|node) '([^']*)'"), "ids"),
    # `_dangling`'s, whose path says which ids the slot takes.
    (re.compile(r"(\w+)\.[\w.]+: '([^']*)' is not "), "slot"),
)

_IMPORT_HINT = (
    "nothing registered is close. A module of the project that registers it has to be imported"
    " first: `--import MODULE`, or ARENA_COMPOSER_IMPORTS in bin/arena-mcp"
)


def _hint(line: str, spec: Any) -> str:
    """What to add to `line` about the name or id it says nothing declares."""
    try:
        for pattern, kind in _UNKNOWN:
            if match := pattern.search(line):
                break
        else:
            return ""
        if kind == "slot":
            section, value = match[1], match[2]
        else:
            section, value = kind, match[1]
        registered = kind in ("asset", "relation", "task")
        if registered:
            from arena_composer import catalog

            names = {
                "asset": lambda: [asset.registry_name for asset in catalog.catalog()],
                "relation": lambda: [relation.name for relation in catalog.relations()],
                "task": lambda: [task.name for task in catalog.tasks()],
            }[kind]()
        else:
            ids, allowed = _id_pools(spec)
            names = allowed.get(section, (ids, ""))[0]
        # The best and any nearly as good: a shared `_ycb_robolab` makes a
        # distant name score well enough to bury the one meant.
        scored = sorted(
            ((difflib.SequenceMatcher(None, value, name).ratio(), name) for name in names if isinstance(name, str)),
            reverse=True,
        )
        close = [name for ratio, name in scored[:3] if ratio >= max(0.6, scored[0][0] - 0.1)]
    except Exception:
        # A hint is a nicety; the line it would have gone on is the answer.
        return ""
    if close:
        quoted = [repr(name) for name in close]
        either = quoted[0] if len(quoted) == 1 else f"{', '.join(quoted[:-1])} or {quoted[-1]}"
        return f"did you mean {either}?"
    return _IMPORT_HINT if registered else ""


def _build_problems(spec: Any) -> list[str]:
    """What fails only when the scene is built, in the merged spec `spec`.

    Arena's validator checks that each name is registered and that each id a
    slot holds is declared, and leaves the rest to the build: whether the
    params bind to the constructor they are passed to, whether an asset is
    the kind its slot holds — a banana validates as the embodiment — whether
    an object reference can be built at all, and the ids in a list param,
    which it skips. A name Arena does not know is left to its validator,
    which refuses it by name.

    Each group fails open: one that cannot run, on an Arena that moved its
    internals, says so on stderr and refuses nothing.
    """
    errors = []
    for group in (_asset_problems, _reference_problems, _relation_problems, _subtask_problems):
        try:
            errors += group(spec)
        except Exception as error:
            print(f"arena-composer: {group.__name__} skipped: {type(error).__name__}: {error}", file=sys.stderr)
    return errors


def _asset_problems(spec: Any) -> list[str]:
    """The embodiment's, the background's and each object's params against the
    constructor they are passed to, and the embodiment an embodiment."""
    from isaaclab_arena.assets.registries import AssetRegistry
    from isaaclab_arena.embodiments.embodiment_base import EmbodimentBase

    registry = AssetRegistry()
    # Arena fills an object's `instance_name` from its id, and nobody else's.
    slots = [(role, spec.get(role) if isinstance(spec, dict) else None, ()) for role in ("embodiment", "background")]
    slots += [(f"objects.{index}", entry, ("instance_name",)) for index, entry in _indexed(spec, "objects")]
    errors = []
    for where, entry, given in slots:
        name = entry.get("registry_name") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not registry.is_registered(name):
            continue
        cls, params = registry.get_asset_by_name(name), entry.get("params")
        errors += _binding(f"{where}.params", name, cls, params if isinstance(params, dict) else {}, given)
        if where == "embodiment" and not issubclass(cls, EmbodimentBase):
            errors.append(f"embodiment.registry_name: {name} is not an embodiment")
    return errors


def _reference_problems(spec: Any) -> list[str]:
    """What Arena asserts as it builds each object reference."""
    from isaaclab_arena.assets.object_type import ObjectType

    # Private API: the params that make a reference an affordance, each its own class.
    from isaaclab_arena.environment_spec.arena_env_graph_conversion_utils import _AFFORDANCE_REFERENCE_CLASSES

    errors = []
    for index, reference in _indexed(spec, "object_references"):
        where, params = f"object_references.{index}", reference.get("params")
        if not reference.get("prim_path"):
            errors.append(f"{where}.prim_path: a reference is built from the prim it names, and names none")
        joints = [name for name in _AFFORDANCE_REFERENCE_CLASSES if isinstance(params, dict) and name in params]
        if len(joints) > 1:
            errors.append(f"{where}.params: a reference exposes one affordance, not {' and '.join(joints)}")
        elif joints and reference.get("object_type") != ObjectType.ARTICULATION.value:
            errors.append(f"{where}.object_type: {joints[0]} makes it an articulation; set it to articulation")
    return errors


def _relation_problems(spec: Any) -> list[str]:
    """Each relation's params against its kind's constructor."""
    from isaaclab_arena.assets.registries import ObjectRelationLibraryRegistry

    registry = ObjectRelationLibraryRegistry()
    errors = []
    for index, relation in _indexed(spec, "relations"):
        kind, params = relation.get("kind"), relation.get("params")
        if not isinstance(kind, str) or not registry.is_registered(kind):
            continue
        cls = registry.get_object_relation_by_name(kind)
        # A binary relation's first argument is the reference, which Arena passes.
        given = () if cls.is_unary() else tuple(name for name in inspect.signature(cls.__init__).parameters)[1:2]
        errors += _binding(f"relations.{index}.params", kind, cls, params if isinstance(params, dict) else {}, given)
    return errors


def _subtask_problems(spec: Any) -> list[str]:
    """Each subtask's params against its kind's constructor, and the ids its
    list params hold, which Arena's validator skips and its builder looks up."""
    from isaaclab_arena.assets.registries import TaskRegistry

    registry = TaskRegistry()
    task = spec.get("task") if isinstance(spec, dict) else None
    # Arena passes the task's description to an atomic task only.
    given = ("task_description",) if isinstance(task, dict) and task.get("composition") == "atomic" else ()
    ids, _ = _id_pools(spec)
    errors = []
    for index, subtask in _indexed(task, "subtasks"):
        kind, params = subtask.get("kind"), subtask.get("params")
        if not isinstance(kind, str) or not registry.is_registered(kind):
            continue
        params, where = params if isinstance(params, dict) else {}, f"task.subtasks.{index}.params"
        errors += _binding(where, kind, registry.get_task_by_name(kind), params, given)
        for name in _node_lists(kind) or ():
            value = params.get(name)
            if value is not None and not isinstance(value, list):
                errors.append(f"{where}.{name}: {kind} takes a list of ids here")
                continue
            for item, member in enumerate(value or []):
                if member not in ids:
                    errors.append(f"{where}.{name}.{item}: {member!r} is not an id this spec declares")
        # ponytail: SortMultiObjectTask's own assert, the one list rule Arena's
        # tasks share with nobody. Read the asserts off the task if more appear.
        picked, placed = params.get("pick_up_object_list"), params.get("destination_location_list")
        if kind == "SortMultiObjectTask" and isinstance(picked, list) and isinstance(placed, list) and len(picked) != len(placed):
            errors.append(
                f"{where}: {kind} pairs each pick_up_object_list entry with a destination_location_list one,"
                f" and has {len(picked)} and {len(placed)}"
            )
    return errors


def _binding(where: str, label: str, cls: type, params: dict[str, Any], given: tuple[str, ...]) -> list[str]:
    """What passing `params` to `cls`, with Arena's `given`, would refuse: a
    required parameter nobody supplies, and a key the constructor does not take."""
    from arena_composer import catalog

    errors = []
    if missing := catalog.missing_params(cls, params, given):
        errors.append(f"{where}: {label} needs {', '.join(missing)}")
    taken = [name for name in inspect.signature(cls.__init__).parameters if name not in ("self", *given)]
    for name in catalog.unexpected_params(cls, params):
        close = difflib.get_close_matches(str(name), taken, n=1)
        errors.append(f"{where}.{name}: {label} takes no {name!r}" + (f"; did you mean {close[0]!r}?" if close else ""))
    return errors


def _node_lists(kind: Any) -> set[str] | None:
    """The params of task `kind` that list node ids, or None when it is not
    registered, and any list of strings may."""
    from isaaclab_arena.assets.registries import TaskRegistry
    from isaaclab_arena.environment_spec.arena_env_graph_task_conversion_utils import (
        find_node_ref_params_in_signature,
    )

    registry = TaskRegistry()
    if not isinstance(kind, str) or not registry.is_registered(kind):
        return None
    found = find_node_ref_params_in_signature(registry.get_task_by_name(kind))
    return {name for name, listed in found.items() if listed}


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
    if entry.get("type") == "string_type" and isinstance(entry.get("input"), bool):
        message += (
            "; Arena reads YAML 1.1, where a bare on, off, yes or no is a boolean, so quote it"
        )
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
    * ``remove`` — drop what `steps` names: an entry, with the relations
      naming it and the object references inside it, or a parameter, which
      hands it back to the kind's default. Dropping a task down to one subtask
      switches its composition back to ``atomic``.

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
            # which does not parse. A fresh one writes as `{}`; a list has its
            # own trap, see `_emptied`.
            if not parent and len(steps) > 1:
                grandparent = _walk(document, steps[:-2])
                if isinstance(parent, dict):
                    grandparent[steps[-2]] = {}
                else:
                    _emptied(grandparent, steps[-2])
            if node_id:
                changed += _drop_dependents(files, node_id)
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


def _indexed(mapping: Any, key: str) -> list[tuple[int, dict[str, Any]]]:
    """The mapping entries of the list under `key` with their indices, or
    none: these paths run on specs a hand edit may have left in any shape."""
    found = mapping.get(key) if isinstance(mapping, dict) else None
    return [(index, entry) for index, entry in enumerate(found if isinstance(found, list) else []) if isinstance(entry, dict)]


def _entries(mapping: Any, key: str) -> list[dict[str, Any]]:
    return [entry for _, entry in _indexed(mapping, key)]


def _id_slots(spec: Any) -> Iterator[tuple[Any, Any, str]]:
    """Every slot in `spec` — one document, or the merged pair — holding a node
    id, as ``(container, key, path)``: the first two to read or write through,
    the path as the spec reads, ``relations.2.reference``.

    The complete list, checked against Arena's validator and its builder: a
    relation's ``subject`` and ``reference``; an object reference's
    ``parent_id``; a CLI override's ``target_node_id``; and a task param that
    is a string, or the strings in one its kind types as a list of assets,
    since Arena requires each to name a node — a list of directions is not
    one, and an object set's ``members`` are registry names, not ids.
    """
    for index, relation in _indexed(spec, "relations"):
        for slot in ("subject", "reference"):
            if isinstance(relation.get(slot), str):
                yield relation, slot, f"relations.{index}.{slot}"
    for index, reference in _indexed(spec, "object_references"):
        if isinstance(reference.get("parent_id"), str):
            yield reference, "parent_id", f"object_references.{index}.parent_id"
    for index, override in _indexed(spec, "cli_override_specs"):
        if isinstance(override.get("target_node_id"), str):
            yield override, "target_node_id", f"cli_override_specs.{index}.target_node_id"
    for index, subtask in _indexed(spec.get("task") if isinstance(spec, dict) else None, "subtasks"):
        params = subtask.get("params")
        if not isinstance(params, dict):
            continue
        # A kind nobody registered: any list of strings may hold ids.
        listed = _node_lists(subtask.get("kind"))
        for name, value in params.items():
            if isinstance(value, str):
                yield params, name, f"task.subtasks.{index}.params.{name}"
            elif isinstance(value, list) and (listed is None or name in listed):
                for item, member in enumerate(value):
                    if isinstance(member, str):
                        yield value, item, f"task.subtasks.{index}.params.{name}.{item}"


#: A list index in an error line or a slot path — `objects.2.params`,
#: `Relation[3]` — which an edit that inserts or removes an entry shifts.
_INDEX = re.compile(r"(?<=\.)\d+(?=[.:]|$)|\[\d+\]")


def _normalised(line: str) -> str:
    return _INDEX.sub("*", line)


def _dangling(spec: Any) -> list[tuple[tuple[str, str], str]]:
    """Each slot of `spec` naming an id it does not declare — or one Arena
    does not allow there: a reference's parent is the background or an
    object, an override's target is one of those or the embodiment — as
    ``((normalised path, id), line)``.

    Computed here rather than read off Arena's validator, whose
    cross-reference checks are asserts in one validator: it reports the first
    dangling id and stops, so fixing one surfaces the next as if the fix had
    introduced it.
    """
    ids, allowed = _id_pools(spec)
    found = []
    for container, key, where in _id_slots(spec):
        declared, expected = allowed.get(where.split(".")[0], (ids, "an id this spec declares"))
        if (value := container[key]) not in declared:
            found.append(((_normalised(where), value), f"{where}: {value!r} is not {expected}"))
    return found


def _id_pools(spec: Any) -> tuple[set[str], dict[str, tuple[set[str], str]]]:
    """Every id `spec` declares, and the ones the slots under a section are
    narrowed to, with how to say so: a reference's parent is the background
    or an object, an override's target one of those or the embodiment."""
    singles = {
        role: spec[role].get("id")
        for role in ("embodiment", "background")
        if isinstance(spec, dict) and isinstance(spec.get(role), dict) and isinstance(spec[role].get("id"), str)
    }
    ids = {
        section: {entry["id"] for entry in _entries(spec, section) if isinstance(entry.get("id"), str)}
        for section in _ID_SECTIONS
    }
    allowed = {
        "object_references": ({singles.get("background")} | ids["objects"], "the background or an object"),
        "cli_override_specs": (set(singles.values()) | ids["objects"], "the embodiment, the background or an object"),
    }
    return set(singles.values()).union(*ids.values()), allowed


#: What Arena's cross-reference asserts say — see `ArenaEnvGraphSpec.validate`.
#: Their lines are replaced by `_dangling`'s, which name every slot; a wording
#: Arena changes falls through to the text comparison, which can only refuse.
_CROSS_REFERENCE = ("references unknown", "references invalid parent", "targets unknown or non-swappable")


def _introduced(
    before_spec: Any, after_spec: Any, before: list[str], after: list[str]
) -> tuple[list[str], list[str]]:
    """Split `after`, the problems with the edited spec, into the ones the edit
    introduced and the ones `before` already had, as ``(introduced, remaining)``.

    Dangling ids are compared as the slots this module finds, since Arena
    names only the first; everything else as lines, with list indices
    normalised so an entry inserted or removed above does not make every line
    below it read as new, and as a multiset, so a second entry wrong in the
    same way as a first is still new.
    """
    was = Counter(key for key, _ in _dangling(before_spec))
    now = Counter(key for key, _ in _dangling(after_spec))
    lines = dict(_dangling(after_spec))
    introduced = [lines[key] for key in now if now[key] > was[key]]
    remaining = [lines[key] for key in now if now[key] <= was[key]]

    def rest(problems: list[str], spec: Any) -> list[str]:
        # `_build_problems` names a list param's dangling ids as `_dangling` does.
        dangling = {line for _, line in _dangling(spec)}
        return [line for line in problems if line not in dangling and not any(mark in line for mark in _CROSS_REFERENCE)]

    after_rest, before_rest = rest(after, after_spec), rest(before, before_spec)
    new = Counter(_normalised(line) for line in after_rest) - Counter(_normalised(line) for line in before_rest)
    for line in after_rest:
        if new[_normalised(line)] > 0:
            new[_normalised(line)] -= 1
            introduced.append(line)
        else:
            remaining.append(line)
    return introduced, remaining


def _remove(files: SpecFiles, key: str, entries: list[Any]) -> None:
    """Take `entries` out of the list under `key`, leaving a fresh ``[]`` when
    that empties it."""
    document = files.documents[files.owner(key)]
    for entry in entries:
        document[key].remove(entry)
    if not document[key]:
        _emptied(document, key)


def _emptied(mapping: Any, key: Any) -> None:
    """Leave a fresh ``[]`` under `key`, without the comment its entries had.

    ruamel keeps a comment written above a list's first entry on the key in
    the parent, and writes it between the key and the emptied list — where
    the `[]` lands at column 0, or indented with the next key's block sequence
    pulled onto its line, and neither parses. The comment was about entries
    that are gone.
    """
    mapping[key] = []
    if hasattr(mapping, "ca"):
        mapping.ca.items.pop(key, None)


def _drop_dependents(files: SpecFiles, node_id: str) -> list[str]:
    """Remove what cannot outlive the entry `node_id` names, one line per kind
    of thing dropped.

    The relations naming it, since a relation pointing at an asset that has
    gone stops the spec loading at all. And the object references whose parent
    it is, with their relations in turn: a reference is a prim inside its
    parent's USD, and Arena allows it no parent but a background or an object,
    so with the parent gone there is nothing for it to be — one outcome, hence
    a cascade and not a refusal.
    """
    gone, changed = [node_id], []
    references = _entries(files.documents[files.owner("object_references")], "object_references")
    if inside := [reference for reference in references if reference.get("parent_id") == node_id]:
        _remove(files, "object_references", inside)
        gone += [reference["id"] for reference in inside if isinstance(reference.get("id"), str)]
        changed.append(f"removed object reference(s) {gone[1:]} inside {node_id!r}")
    relations = _entries(files.documents[files.owner("relations")], "relations")
    if naming := [r for r in relations if r.get("subject") in gone or r.get("reference") in gone]:
        _remove(files, "relations", naming)
        changed.append(f"removed {len(naming)} relation(s) naming {' or '.join(repr(name) for name in gone)}")
    return changed


def _rename_node(files: SpecFiles, previous: str, node_id: str) -> int:
    """Point everything naming `previous` at `node_id` instead: every slot
    :func:`_id_slots` finds in either file, and the prim path of an object
    reference inside a renamed *object*, which Arena spells as
    ``{ENV_REGEX_NS}/<parent id>/...`` — an asset is spawned under its id. A
    reference inside the background spells the background's registry name
    there, which a rename of its id leaves alone.

    A name left behind is a reference to an asset that no longer exists, which
    stops the spec loading — the same failure removing an asset without its
    relations causes. Task params go with a rename and not with a remove,
    which leaves them alone deliberately: the node a rename renames still
    exists.
    """
    renamed = 0
    for document in files.documents.values():
        for container, key, _ in _id_slots(document):
            if container[key] == previous:
                container[key] = node_id
                renamed += 1
    object_ids = {entry.get("id") for entry in _entries(files.documents[files.owner("objects")], "objects")}
    prefix = f"{{ENV_REGEX_NS}}/{previous}/"
    for reference in _entries(files.documents[files.owner("object_references")], "object_references"):
        # `parent_id` already carries the new id by now.
        prim_path = reference.get("prim_path")
        if reference.get("parent_id") == node_id and node_id in object_ids and str(prim_path).startswith(prefix):
            reference["prim_path"] = f"{{ENV_REGEX_NS}}/{node_id}/{prim_path.removeprefix(prefix)}"
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
    dropped = catalog.unexpected_params(AssetRegistry().get_asset_by_name(registry_name), params)
    for name in dropped:
        del params[name]
    # As in `apply`'s remove: an emptied commented mapping does not parse.
    if not params:
        entry["params"] = {}
    return dropped


def remove_object(path: Path, node_id: str) -> Result:
    """Remove an object from `path`'s scene, along with the relations naming it
    and the object references inside it, with theirs.

    Those go with it because a relation pointing at a missing asset stops the
    spec loading at all, and a reference is a prim of its parent's USD — see
    :func:`_drop_dependents`. Task params naming it are left alone
    deliberately: a task whose target has gone is a decision about the task,
    not a reference to tidy up, and the validation error says so.
    """
    files = _files(path)
    if isinstance(files, Result):
        return files
    matched = [entry for entry in _entries(files.documents[files.owner("objects")], "objects") if entry.get("id") == node_id]
    if not matched:
        return Result.failed(f"no object {node_id!r} in this spec; it has {files.asset_ids()}")

    _remove(files, "objects", matched)
    return files.write([f"removed object {node_id!r}", *_drop_dependents(files, node_id)])



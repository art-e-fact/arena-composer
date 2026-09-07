"""Structured edits to an ArenaEnvGraphSpec YAML, validated before they land.

The agent editing these files already has Read and Edit, and for reading a spec
those are better than anything here — the YAML is short and exact. What it does
not have is the two things this module adds:

* **Where an edit belongs.** A task spec includes a scene through
  ``external_yaml``, and the two are a disjoint union: ``objects`` and
  ``relations`` live in exactly one of the files. An edit goes to whichever file
  owns the key.
* **What an edit drags with it.** Removing an object also has to remove the
  relations that name it, or the spec stops loading with a dangling reference.

Every edit is checked against the merged spec before either file is written, so
a rejected edit leaves the pair on disk exactly as it was.
"""

from __future__ import annotations

import io
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


class SpecFiles:
    """A spec and the scene it includes, held as round-trip YAML documents.

    Loaded together because that is the unit validation applies to: a change to
    the scene can only be judged against the task that includes it.
    """

    def __init__(self, path: Path) -> None:
        self.yaml = YAML()
        self.yaml.preserve_quotes = True
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
            ArenaEnvGraphSpec.from_dict(self.merged(reloaded))
        except ValidationError as error:
            # Not `str(error)`: that quotes the whole merged spec back as the
            # offending input, burying the one sentence worth reading.
            return [_message(entry) for entry in error.errors()]
        except Exception as error:
            # Arena asserts outside pydantic too, e.g. on a duplicate key
            # across the include.
            return [f"{type(error).__name__}: {error}"]
        return []

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


def _message(entry: dict[str, Any]) -> str:
    """One pydantic error as a line an agent can act on."""
    # Arena's validators are bare `assert`s, which pydantic reports under this
    # prefix; the sentence after it is the one the author wrote.
    message = entry["msg"].removeprefix("Assertion failed, ")
    location = ".".join(str(part) for part in entry["loc"])
    return f"{location}: {message}" if location else message


def add_object(path: Path, node_id: str, registry_name: str, on: str | None = None) -> Result:
    """Add an object to `path`'s scene, optionally resting on another asset.

    Without `on` the object takes no part in placement and lands at the origin,
    which for most scenes means inside the floor. It stays optional because an
    anchored scene may want the object placed by a relation added separately.

    `registry_name` is checked by Arena, not here — its own field validator
    rejects a name the asset registry does not know.
    """
    files = SpecFiles(path)
    known = files.asset_ids()
    if node_id in known:
        return Result.failed(f"id {node_id!r} is already used in this spec")
    if on is not None and on not in known:
        return Result.failed(f"no asset {on!r} to put {node_id!r} on; this spec has {known}")

    files.section("objects").append({"id": node_id, "registry_name": registry_name, "params": {}})
    changed = [f"added object {node_id!r} ({registry_name})"]
    if on is not None:
        files.section("relations").append({"kind": "on", "subject": node_id, "reference": on, "params": {}})
        changed.append(f"added relation {node_id!r} on {on!r}")
    return files.write(changed)


def remove_object(path: Path, node_id: str) -> Result:
    """Remove an object from `path`'s scene, along with the relations naming it.

    Relations go with it because a relation pointing at a missing asset stops
    the spec loading at all. Task params naming it are left alone deliberately:
    a task whose target has gone is a decision about the task, not a reference
    to tidy up, and the validation error says so.
    """
    files = SpecFiles(path)
    objects = files.section("objects")
    matched = [entry for entry in objects if entry.get("id") == node_id]
    if not matched:
        return Result.failed(f"no object {node_id!r} in this spec; it has {files.asset_ids()}")

    for entry in matched:
        objects.remove(entry)
    changed = [f"removed object {node_id!r}"]

    relations = files.section("relations")
    dropped = [r for r in relations if node_id in (r.get("subject"), r.get("reference"))]
    for relation in dropped:
        relations.remove(relation)
    if dropped:
        changed.append(f"removed {len(dropped)} relation(s) naming {node_id!r}")

    return files.write(changed)



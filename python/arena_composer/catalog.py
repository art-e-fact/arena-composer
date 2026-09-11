"""What Arena's registries have to offer: the assets, and the relation and task
kinds a spec can name.

The registries are live things that populate themselves on import, so this is
the one part of authoring a spec that cannot be answered by reading files.

Assets are large: dumping them costs more context than it is worth, and an
agent only ever needs the handful matching what it is looking for — hence
:func:`search` rather than a listing. The editor is the exception and takes
:func:`catalog` whole, because a form filters faster than a round trip.

The kinds carry the form that edits them. Every parameter is inferred from the
constructor rather than listed here, so a relation somebody registers
themselves gets a form with no change to this file.
"""

from __future__ import annotations

import enum
import inspect
import sys
import types
import typing
from dataclasses import dataclass
from functools import cache
from typing import Any, get_args, get_origin, get_type_hints


@dataclass(frozen=True)
class Asset:
    """One registered asset, as a spec's ``registry_name`` sees it."""

    registry_name: str
    #: `embodiment`, `background` or `object`.
    role: str
    tags: list[str]
    #: Physics type for objects (`rigid`, `articulated`, ...), else None.
    object_type: str | None

    def matches(self, terms: list[str]) -> bool:
        """Whether every term appears in the name or a tag."""
        haystack = " ".join([self.registry_name, *self.tags]).lower()
        return all(term in haystack for term in terms)


@cache
def catalog() -> tuple[Asset, ...]:
    """Every registered asset. Cached: the registry does not change at runtime,
    and building it imports the whole asset package."""
    from isaaclab_arena.agentic_environment_generation.catalogues import build_asset_catalogue

    built = build_asset_catalogue()
    return tuple(
        Asset(
            registry_name=entry["name"],
            role=role,
            tags=list(entry.get("tags") or []),
            object_type=entry.get("object_type"),
        )
        for role, entries in (
            ("embodiment", built.embodiments),
            ("background", built.backgrounds),
            ("object", built.objects),
        )
        for entry in entries
    )


def search(query: str = "", role: str | None = None, limit: int = 20) -> list[Asset]:
    """Assets whose name or tags contain every whitespace-separated term in
    `query`, nearest match first.

    An empty query matches everything, which with `role` set is how to see what
    kinds of background or embodiment exist at all.
    """
    terms = query.lower().split()
    found = [
        asset
        for asset in catalog()
        if asset.matches(terms) and (role is None or asset.role == role)
    ]
    # A term matching the name itself beats one that only matched a tag, and a
    # shorter name means fewer extra words around the match.
    found.sort(key=lambda asset: (not any(t in asset.registry_name for t in terms), len(asset.registry_name)))
    return found[:limit]


#: Modules whose classes name a node in the graph rather than a value. A
#: parameter annotated with one holds an id — ``pick_up_object: apple`` — so it
#: is edited by picking from the spec's ids, not by typing.
_NODE_REF_MODULES = (
    "isaaclab_arena.assets.asset",
    "isaaclab_arena.affordances",
    "isaaclab_arena.embodiments",
)

#: Already carried by the spec itself rather than by ``params``.
_NOT_PARAMS = frozenset({"self", "parent", "task_description"})


@dataclass(frozen=True)
class Field:
    """One parameter of a relation or task kind, as the control that edits it."""

    name: str
    #: `number`, `int`, `bool`, `text`, `enum` or `node_ref`.
    kind: str
    #: Whether the parameter accepts None. An optional number needs a way to say
    #: "unset" that a number box on its own cannot express.
    optional: bool
    #: No default, so the spec is invalid until it is given one.
    required: bool
    default: Any
    #: The values of an `enum`, empty for every other kind.
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class Kind:
    """A registered relation or task kind, and the form that edits it."""

    name: str
    fields: tuple[Field, ...]
    summary: str
    #: Relations only: a unary relation takes no `reference`.
    unary: bool | None = None


@cache
def relations() -> tuple[Kind, ...]:
    """Every registered relation kind. Not just the ``agent_ready`` ones —
    those are five of the twelve, and the specs in this repo use eleven."""
    from isaaclab_arena.assets.registries import ObjectRelationLibraryRegistry

    registry = ObjectRelationLibraryRegistry()
    found = [(name, registry.get_object_relation_by_name(name)) for name in sorted(registry.get_all_keys())]
    return tuple(_kind(cls, name, unary=cls.is_unary()) for name, cls in found)


@cache
def tasks() -> tuple[Kind, ...]:
    """Every registered task kind, keyed as the spec names them (PascalCase)."""
    from isaaclab_arena.assets.registries import TaskRegistry

    registry = TaskRegistry()
    return tuple(_kind(registry.get_task_by_name(name), name) for name in sorted(registry.get_all_keys()))


def _kind(cls: type, name: str, unary: bool | None = None) -> Kind:
    hints = _hints(cls)
    fields = []
    for param in inspect.signature(cls.__init__).parameters.values():
        if param.name in _NOT_PARAMS or param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        # A parameter no control can edit — a callable factory, a list of
        # assets — is left out. Edits address one leaf at a time, so a value
        # the form never shows still survives a write.
        if (field := _field(param, hints.get(param.name, param.annotation))) is not None:
            fields.append(field)
    return Kind(name=name, fields=tuple(fields), summary=_summary(cls), unary=unary)


def _field(param: inspect.Parameter, annotation: Any) -> Field | None:
    required = param.default is inspect.Parameter.empty
    default = None if required else _plain(param.default)

    def made(kind: str, optional: bool, options: tuple[str, ...] = ()) -> Field:
        return Field(param.name, kind, optional, required, default, options)

    # Before unwrapping the union: `Side | str` is an enum parameter that also
    # accepts the enum's value as a string.
    if (choices := _enum(annotation)) is not None:
        return made("enum", optional=_accepts_none(annotation), options=choices)

    base, optional = _unwrap(annotation)
    if base is bool:
        return made("bool", optional)
    if base is float:
        return made("number", optional)
    if base is int:
        return made("int", optional)
    if base is str:
        return made("text", optional)
    if isinstance(base, type) and base.__module__.startswith(_NODE_REF_MODULES):
        return made("node_ref", optional)
    return None


def _hints(cls: type) -> dict[str, Any]:
    """`cls.__init__`'s annotations, resolved to types.

    Falls back to evaluating them against their own module: under
    ``from __future__ import annotations`` an annotation is a string, and one
    naming a type this module cannot see fails to resolve from here.
    """
    try:
        return get_type_hints(cls.__init__)
    except Exception:
        module_globals = vars(sys.modules[cls.__module__])
        hints = {}
        for name, param in inspect.signature(cls.__init__).parameters.items():
            try:
                hints[name] = eval(param.annotation, module_globals)  # noqa: S307 — Arena's own annotations
            except Exception:
                hints[name] = param.annotation
        return hints


def _enum(annotation: Any) -> tuple[str, ...] | None:
    """The values of the Enum in `annotation`, including inside a union."""
    if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
        return tuple(str(member.value) for member in annotation)
    for argument in get_args(annotation):
        if (found := _enum(argument)) is not None:
            return found
    return None


def _unwrap(annotation: Any) -> tuple[Any, bool]:
    """`annotation` without its None, and whether it had one."""
    if get_origin(annotation) in (typing.Union, types.UnionType):
        rest = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(rest) == 1:
            return rest[0], True
    return annotation, False


def _accepts_none(annotation: Any) -> bool:
    return type(None) in get_args(annotation)


def _plain(value: Any) -> Any:
    """A default as the payload can carry it."""
    if isinstance(value, enum.Enum):
        return _plain(value.value)
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    # A default this can't express — a callable, a config object — reads as
    # absent, which is what the form shows for it anyway.
    return None


def _summary(cls: type) -> str:
    """The first non-empty line of `cls`'s docstring."""
    for line in (cls.__doc__ or "").splitlines():
        if stripped := line.strip():
            return stripped
    return ""

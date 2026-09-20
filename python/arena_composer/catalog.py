"""What a spec can name: the assets, and the relation and task kinds.

Assets come from two places. Arena's registries are live things that populate
themselves on import, so they are the one part of authoring a spec that cannot
be answered by reading files. NVIDIA's SimReady props are not registered at
all — a spec names one through the generic ``simready_usd_object`` and the
prop's URL — and come from the local index :mod:`arena_composer.simready`
builds, when there is one.

Assets are many: dumping them costs more context than it is worth, and an
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
from dataclasses import dataclass, field
from functools import cache, lru_cache
from pathlib import Path
from typing import Any, get_args, get_origin, get_type_hints
from urllib.parse import urlparse


@dataclass(frozen=True)
class Asset:
    """One asset a spec can name: registered with Arena, or a SimReady prop the
    local index knows."""

    registry_name: str
    #: `embodiment`, `background` or `object`.
    role: str
    tags: list[str]
    #: Physics type for objects (`rigid`, `articulated`, ...), else None.
    object_type: str | None
    #: What to call it: the registry name, or a remote asset's folder name,
    #: since every remote asset shares one registry name.
    name: str
    #: The USD it spawns from, where the asset says. It is what a resolve
    #: downloads, so whether that has happened yet can be told before a pick.
    usd_path: str | None = None
    #: What a spec entry has to carry in `params` beyond the registry name:
    #: the URL, for a remote asset.
    params: dict[str, Any] = field(default_factory=dict)
    #: A picture of the asset, where one is published beside it. A URL like
    #: `usd_path`: whether it has been fetched is `usd_assets.mirrored`'s to say.
    thumbnail: str | None = None

    def matches(self, terms: list[str]) -> bool:
        """Whether every term appears in the name, a tag or the USD's file name."""
        stem = Path(urlparse(self.usd_path).path).stem if self.usd_path else ""
        haystack = " ".join([self.name, *self.tags, stem]).lower()
        return all(term in haystack for term in terms)


@cache
def catalog() -> tuple[Asset, ...]:
    """Every registered asset. Cached: the registry does not change at runtime,
    and building it imports the whole asset package."""
    from isaaclab_arena.agentic_environment_generation.catalogues import build_asset_catalogue
    from isaaclab_arena.assets.registries import AssetRegistry

    registry = AssetRegistry()
    built = build_asset_catalogue()
    return tuple(
        Asset(
            registry_name=entry["name"],
            role=role,
            tags=list(entry.get("tags") or []),
            object_type=entry.get("object_type"),
            name=entry["name"],
            usd_path=_class_usd_path(registry.get_asset_by_name(entry["name"])),
        )
        for role, entries in (
            ("embodiment", built.embodiments),
            ("background", built.backgrounds),
            ("object", built.objects),
        )
        for entry in entries
    )


def _class_usd_path(cls: type) -> str | None:
    """The USD a registered class spawns, where that is a plain attribute. Not
    every class has one: some compose their USD at construction, some spawn
    procedurally."""
    usd_path = getattr(cls, "usd_path", None)
    return usd_path if isinstance(usd_path, str) and usd_path else None


def remote() -> tuple[Asset, ...]:
    """Every SimReady prop the local index knows, as an object a spec names
    with ``simready_usd_object`` and the prop's URL in `params`. Empty until the
    index is built; re-read when any process rebuilds it."""
    from arena_composer import simready

    return _remote(simready.index_stamp())


# One entry, because only the current index is ever asked for: a crawl started
# from the viewer replaces it, and every generation before it is dead weight.
@lru_cache(maxsize=1)
def _remote(stamp: int | None) -> tuple[Asset, ...]:
    from isaaclab_arena.assets.registries import AssetRegistry
    from isaaclab_arena.assets.simready_constants import SIMREADY_USD_OBJECT_REGISTRY_NAME

    from arena_composer import simready

    if stamp is None:
        return ()
    cls = AssetRegistry().get_asset_by_name(SIMREADY_USD_OBJECT_REGISTRY_NAME)
    return tuple(
        Asset(
            registry_name=SIMREADY_USD_OBJECT_REGISTRY_NAME,
            role="object",
            tags=[*cls.tags, *asset.category.lower().split("/")],
            object_type=_plain(cls.object_type),
            name=asset.name,
            usd_path=asset.usd_path,
            params={"usd_path": asset.usd_path},
            thumbnail=asset.thumbnail,
        )
        for asset in simready.load()
    )


def search(query: str = "", role: str | None = None, limit: int = 20) -> list[Asset]:
    """Assets whose name, tags or USD file name contain every whitespace-separated
    term in `query`, nearest match first.

    An empty query matches everything, which with `role` set is how to see what
    kinds of background or embodiment exist at all.
    """
    terms = query.lower().split()
    found = [
        asset
        for asset in (*catalog(), *remote())
        if asset.matches(terms) and (role is None or asset.role == role)
    ]
    # A term matching the name itself beats one that only matched a tag; a
    # registered asset, which Arena has vetted, beats a remote one; and a
    # shorter name means fewer extra words around the match.
    found.sort(
        key=lambda asset: (not any(t in asset.name.lower() for t in terms), bool(asset.params), len(asset.name))
    )
    return found[:limit]


def missing_params(registry_name: str, params: dict[str, Any]) -> list[str]:
    """Constructor parameters of the asset with no default that `params` does
    not supply — what makes a spec entry fail to build after it validated.

    Arena checks that a registry name exists, not what the asset behind it
    takes: a ``simready_usd_object`` without its ``usd_path`` is a valid spec
    that asserts when instantiated. `instance_name` is filled in from the
    entry's id and not counted.
    """
    from isaaclab_arena.assets.registries import AssetRegistry

    cls = AssetRegistry().get_asset_by_name(registry_name)
    return [
        param.name
        for param in inspect.signature(cls.__init__).parameters.values()
        if param.name not in ("self", "instance_name")
        and param.default is inspect.Parameter.empty
        and param.kind in (param.POSITIONAL_OR_KEYWORD, param.KEYWORD_ONLY)
        and param.name not in params
    ]


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

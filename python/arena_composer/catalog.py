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

What is registered depends on what has been imported, so every command calls
:func:`load_extensions` before anything here is first asked.
"""

from __future__ import annotations

import contextlib
import enum
import importlib
import inspect
import os
import re
import sys
import types
import typing
from dataclasses import dataclass, field
from collections.abc import Iterable
from functools import cache, lru_cache
from pathlib import Path
from typing import Any, get_args, get_origin, get_type_hints
from urllib.parse import urlparse

#: Modules every command imports before it reads a spec, separated by
#: `os.pathsep`: a project's own assets and kinds, registered as a side effect.
IMPORTS = "ARENA_COMPOSER_IMPORTS"


@cache
def load_extensions(modules: tuple[str, ...] = ()) -> None:
    """Import `modules` and the ones `ARENA_COMPOSER_IMPORTS` names, for what
    they register.

    Before the first :func:`catalog`, :func:`relations`, :func:`tasks` or
    ``payload.kinds``: those are cached, and keep what was registered when
    they were first asked. Raises an `ImportError` naming the module and
    where it was named, for a command to print; a failure is not cached, so
    the next call tries again.
    """
    # Arena and its registries print as they populate.
    with contextlib.redirect_stdout(sys.stderr):
        named = [(module, "--import") for module in modules]
        named += [(module, IMPORTS) for module in os.environ.get(IMPORTS, "").split(os.pathsep) if module]
        for module, source in named:
            try:
                importlib.import_module(module)
            except ImportError as error:
                raise ImportError(f"{source} names {module}, which does not import: {error}") from error
            except Exception as error:
                # A project's own module: a syntax error, or one it raises as
                # it registers, is the same refusal, and says which.
                raise ImportError(
                    f"{source} names {module}, which does not import: {type(error).__name__}: {error}"
                ) from error


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
    #: A remote asset's folders under the SimReady root, e.g.
    #: ``Residential/Kitchen/Baskets``: where the viewer's browser files it.
    #: Empty for a registered asset.
    category: str = ""

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
    procedurally, and Lightwheel's resolve theirs on first read — a descriptor
    that asks api.lightwheel.net every time, cached or not. Read statically, so
    listing assets never touches the network and those six have no path."""
    usd_path = inspect.getattr_static(cls, "usd_path", None)
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
            category=asset.category,
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


def missing_params(cls: type, params: dict[str, Any], given: Iterable[str] = ()) -> list[str]:
    """Constructor parameters of `cls` with no default that neither `params`
    nor `given`, what Arena passes itself, supplies — what makes a spec entry
    fail to build after it validated.

    Arena checks that a registry name exists, not what the class behind it
    takes: a ``simready_usd_object`` without its ``usd_path`` is a valid spec
    that asserts when instantiated.
    """
    return [
        param.name
        for param in _signature(cls)
        if param.default is inspect.Parameter.empty
        and param.kind in (param.POSITIONAL_OR_KEYWORD, param.KEYWORD_ONLY)
        and param.name not in params
        and param.name not in given
    ]


def unexpected_params(cls: type, params: dict[str, Any]) -> list[str]:
    """Keys of `params` that `cls`'s constructor does not take — what an entry
    carries over from the asset it named before, or a typo, and what fails the
    build with a TypeError. None for a constructor that takes ``**kwargs``."""
    taken = _signature(cls)
    if any(param.kind is param.VAR_KEYWORD for param in taken):
        return []
    return [name for name in params if name not in {param.name for param in taken}]


def _signature(cls: type) -> list[inspect.Parameter]:
    return [param for param in inspect.signature(cls.__init__).parameters.values() if param.name != "self"]


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
    #: The class the entry a `node_ref` names has to be, which the task asserts
    #: as Arena builds it: `Asset` for any, or one narrower, such as `Openable`.
    #: Empty for every other kind.
    needs: str = ""


@dataclass(frozen=True)
class Kind:
    """A registered relation or task kind, and the form that edits it."""

    name: str
    fields: tuple[Field, ...]
    summary: str
    #: Relations only: a unary relation takes no `reference`.
    unary: bool | None = None
    #: Marked by Arena as one its own spec-writing agent may use. The rest are
    #: registered all the same, and a spec may name them.
    agent_ready: bool = False
    #: The parameters no control edits — a tuple, a list of assets, a Literal,
    #: a callable — by name: their annotated `type`, whether one is `required`
    #: and the `default`, and for a list of ids the class each `needs`. A text
    #: edit can still set what a form cannot, so the kind's whole signature
    #: stays readable.
    other_params: dict[str, dict[str, Any]] = field(default_factory=dict)


@cache
def relations() -> tuple[Kind, ...]:
    """Every registered relation kind. Not just the ``agent_ready`` ones —
    those are five of the twelve."""
    from isaaclab_arena.assets.registries import ObjectRelationLibraryRegistry

    registry = ObjectRelationLibraryRegistry()
    found = [(name, registry.get_object_relation_by_name(name)) for name in sorted(registry.get_all_keys())]
    return tuple(_kind(cls, name, unary=cls.is_unary()) for name, cls in found)


@cache
def tasks() -> tuple[Kind, ...]:
    """Every registered task kind, keyed as the spec names them (PascalCase)."""
    from isaaclab_arena.assets.registries import TaskRegistry

    registry = TaskRegistry()
    found = [(name, registry.get_task_by_name(name)) for name in sorted(registry.get_all_keys())]
    return tuple(_kind(cls, name, refs=_node_refs(cls)) for name, cls in found)


def _node_refs(cls: type) -> dict[str, bool]:
    """The params of task `cls` that name a node, each True when it takes a
    list of ids: those Arena swaps for the entries they name as it builds the
    task. Empty when the annotations do not resolve, as then Arena cannot build
    the task either, and one project's task should not take the rest down."""
    from isaaclab_arena.environment_spec.arena_env_graph_task_conversion_utils import (
        find_node_ref_params_in_signature,
    )

    try:
        return find_node_ref_params_in_signature(cls)
    except Exception:
        return {}


def _kind(cls: type, name: str, unary: bool | None = None, refs: dict[str, bool] | None = None) -> Kind:
    """`refs` are the params that name a node, as :func:`_node_refs` finds
    them. A relation has none: Arena passes it its params as they are written."""
    hints, refs = _hints(cls), refs or {}
    fields, other = [], {}
    for param in inspect.signature(cls.__init__).parameters.values():
        if param.name in _NOT_PARAMS or param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        # A parameter no control can edit — a callable factory, a list of
        # assets — gets no field, only a line under `other_params`. Edits
        # address one leaf at a time, so a value the form never shows still
        # survives a write.
        annotation, ref = hints.get(param.name, param.annotation), refs.get(param.name)
        if (field := _field(param, annotation, ref)) is not None:
            fields.append(field)
        else:
            required = param.default is inspect.Parameter.empty
            other[param.name] = {
                "type": _type_name(annotation),
                "required": required,
                "default": None if required else _plain(param.default),
                **({"needs": _needs(annotation)} if ref else {}),
            }
    return Kind(
        name=name,
        fields=tuple(fields),
        summary=_summary(cls),
        unary=unary,
        agent_ready=bool(getattr(cls, "agent_ready", False)),
        other_params=other,
    )


def _type_name(annotation: Any) -> str:
    """An annotation as the signature reads, without module paths:
    ``tuple[float, float] | None``, ``list[Asset]``."""
    text = annotation.__qualname__ if isinstance(annotation, type) else repr(annotation)
    return re.sub(r"\b[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\.([A-Za-z_]\w*)", r"\1", text)


def _field(param: inspect.Parameter, annotation: Any, ref: bool | None = None) -> Field | None:
    """`ref` is False for a param that names one node, and True for one that
    takes a list of ids, which no control picks."""
    required = param.default is inspect.Parameter.empty
    default = None if required else _plain(param.default)

    def made(kind: str, optional: bool, options: tuple[str, ...] = (), needs: str = "") -> Field:
        return Field(param.name, kind, optional, required, default, options, needs)

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
    if ref is False:
        return made("node_ref", _accepts_none(annotation), needs=_needs(annotation))
    return None


def node_classes(annotation: Any) -> tuple[type, ...]:
    """The classes a node param's annotation asks for, as Arena reads them:
    `Openable` of `Openable`, `Asset` of `list[Asset] | None`."""
    union = get_origin(annotation) in (typing.Union, types.UnionType)
    found = []
    for branch in get_args(annotation) if union else (annotation,):
        if get_origin(branch) is list:
            branch = next(iter(get_args(branch)), None)
        if isinstance(branch, type) and branch is not type(None):
            found.append(branch)
    return tuple(found)


def _needs(annotation: Any) -> str:
    """What a node param's annotation asks for, as a field says it: `Openable`."""
    return " or ".join(cls.__name__ for cls in node_classes(annotation))


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

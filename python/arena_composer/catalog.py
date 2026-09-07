"""What Arena's asset registry has to offer, searchably.

The registry is a live thing that populates itself on import, so this is the
one part of authoring a spec that cannot be answered by reading files. It is
also large: dumping it costs more context than it is worth, and the agent only
ever needs the handful of assets matching what it is looking for — hence
:func:`search` rather than a listing.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache


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

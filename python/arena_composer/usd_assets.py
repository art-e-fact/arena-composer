"""Read Arena's remote USD assets without Isaac Sim's asset resolver.

Arena asset paths are Nucleus/HTTPS URLs. Inside Kit an Omniverse asset resolver makes
``Usd.Stage.Open("https://…")`` work; outside Kit it fails. Isaac Lab's ``retrieve_file_path``
mirrors a remote asset and its dependencies into a local directory, so patching
``Usd.Stage.Open`` to open that mirror makes every USD read in Arena — bounding boxes, prim
transforms, object-type detection — work sim-free.
"""

from __future__ import annotations

import atexit
import os
import shutil
from pathlib import Path

from pxr import Usd

DEFAULT_CACHE_DIR = Path(
    os.environ.get("ARENA_COMPOSER_ASSET_CACHE", Path.home() / ".cache" / "arena-composer" / "usd")
)
_USD_SUFFIXES = {".usd", ".usda", ".usdc"}

_cache_dir = DEFAULT_CACHE_DIR
# Downloads are cached on disk; this memo also skips the per-call remote stat.
_local_paths: dict[str, str] = {}
_patched = False
# This process's copies of rewritten layers; see `_note_copy`.
_copies: set[str] = set()


def cache_dir() -> Path:
    """Where remote assets are mirrored. The SimReady index lives there too, so
    one setting moves both."""
    return _cache_dir


def use_local_asset_cache(cache_dir: str | Path | None = None) -> Path:
    """Patch ``Usd.Stage.Open`` to localize remote paths first, and return the cache directory."""
    global _cache_dir, _patched
    if cache_dir is not None:
        _cache_dir = Path(cache_dir).expanduser()
    if not _patched:
        stage_open = Usd.Stage.Open

        def open_local(path, *args, **kwargs):
            return stage_open(localize(path) if isinstance(path, str) else path, *args, **kwargs)

        Usd.Stage.Open = staticmethod(open_local)
        _patched = True
    return _cache_dir


def localize(usd_path: str) -> str:
    """Return a local filesystem path for ``usd_path``, downloading it (with deps) if it is a URL."""
    if "://" not in usd_path:
        return usd_path
    if usd_path not in _local_paths:
        from isaaclab.utils.assets import retrieve_file_path

        local_path = retrieve_file_path(usd_path, download_dir=str(_cache_dir))
        _note_copy(local_path)
        _relink_url_references(local_path)
        _local_paths[usd_path] = local_path
    return _local_paths[usd_path]


def _note_copy(local_path: str) -> None:
    """Remove at exit the copy Isaac Lab made of a layer whose references it localized.

    Since Isaac Lab 3.0, ``retrieve_file_path`` leaves the downloads as they are and writes such a
    layer, with its references pointed at local files, into a fresh ``isaaclab_usd_<uuid>`` folder in
    the cache, in every process that asks for it. In a cache that outlives the process those would
    pile up, a table's 14 MB a run.
    """
    # ponytail: a process killed before exit leaves its copies; sweep old isaaclab_usd_* at start if that bites.
    root = os.path.abspath(_cache_dir)
    folder = os.path.relpath(os.path.abspath(local_path), root).split(os.sep)[0]
    if folder.startswith("isaaclab_usd_"):
        if not _copies:
            atexit.register(_drop_copies)
        _copies.add(os.path.join(root, folder))


def _drop_copies() -> None:
    for folder in _copies:
        shutil.rmtree(folder, ignore_errors=True)
    _copies.clear()


def _relink_url_references(local_root: str) -> None:
    """Repoint absolute URL references inside downloaded layers at their mirrored copies.

    The mirror keeps the remote directory structure, so *relative* references resolve on their
    own. An absolute ``https://…`` reference does not: USD would try to open it remotely and
    drop the subtree. Rewriting those to mirror-relative paths (once, in the cache) is what
    makes a composed scene like a Replicator kitchen load completely. Isaac Lab 3.0 leaves a
    UDIM texture, one image per tile, as a URL with its tiles on the server: those are fetched
    here (:func:`_udim`).
    """
    from pxr import Sdf, UsdUtils

    pending, visited = [local_root], set()
    while pending:
        layer_path = pending.pop()
        if layer_path in visited or Path(layer_path).suffix.lower() not in _USD_SUFFIXES:
            continue
        visited.add(layer_path)
        layer = Sdf.Layer.FindOrOpen(layer_path)
        if layer is None:
            continue
        layer_dir = os.path.dirname(layer_path)
        rewritten = False

        def repoint(asset_path: str, layer_dir: str = layer_dir) -> str:
            nonlocal rewritten
            if "://" in asset_path:
                local_path = _udim(asset_path) if "<UDIM>" in asset_path else mirrored(asset_path)
            else:
                local_path = os.path.join(layer_dir, asset_path)
            if local_path is None:
                return asset_path
            pending.append(os.path.normpath(local_path))
            if "://" not in asset_path:
                return asset_path
            rewritten = True
            return os.path.relpath(local_path, layer_dir)

        UsdUtils.ModifyAssetPaths(layer, repoint)
        if rewritten:
            layer.Save()


def _udim(url: str) -> str | None:
    """Mirror a UDIM texture's tiles and return the local path with its ``<UDIM>``, or None when it
    has none. Tiles are numbered from 1001 without a gap, so the first missing one ends them, as
    Isaac Lab fetched them before 3.0."""
    from isaaclab.utils.assets import _mirror_path, retrieve_file_path

    found = False
    for tile in range(1001, 1101):
        try:
            retrieve_file_path(url.replace("<UDIM>", str(tile)), download_dir=str(_cache_dir))
        except FileNotFoundError:
            break
        found = True
    return _mirror_path(url, str(_cache_dir)) if found else None


def mirrored(url: str) -> str | None:
    """Local copy of a downloaded URL, or None when it was never mirrored.

    The layout is Isaac Lab's — ``<cache>/<scheme>/<host>/<path>`` — and is
    taken from its own function rather than spelled out again here, so the two
    cannot drift apart. A copy that exists is reported without asking the
    server whether it is current; that check is the download's own.

    A path that is not a URL is its own local copy, the same rule
    :func:`localize` goes by — some assets are registered from a directory
    another SDK has already downloaded, and what sits beside those layers is
    just as much on disk as what was mirrored here.
    """
    if "://" not in url:
        return url if os.path.isfile(url) else None

    from isaaclab.utils.assets import _mirror_path

    path = _mirror_path(url, str(_cache_dir))
    return path if os.path.isfile(path) else None

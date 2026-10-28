"""Read Arena's remote USD assets without Isaac Sim's asset resolver.

Arena asset paths are Nucleus/HTTPS URLs. Inside Kit an Omniverse asset resolver makes
``Usd.Stage.Open("https://…")`` work; outside Kit it fails. Isaac Lab's ``retrieve_file_path``
mirrors a remote asset and its dependencies into a local directory, so patching
``Usd.Stage.Open`` to open that mirror makes every USD read in Arena — bounding boxes, prim
transforms, object-type detection — work sim-free.

Since Isaac Lab 3.0 the mirror holds the downloads as they are, and a layer whose references had
to be pointed at local files is copied, so pointed, into a fresh ``isaaclab_usd_<uuid>`` folder
of the cache in every process: :func:`_note_copy` removes this process's at exit, all but a robot
on a stand's (:func:`_kept_copy`). A UDIM texture it leaves on the server, which :func:`_udim`
fetches.
"""

from __future__ import annotations

import atexit
import contextlib
import functools
import hashlib
import json
import os
import shutil
import sys
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
    """Patch ``Usd.Stage.Open`` to localize remote paths first, and Isaac Lab's
    ``retrieve_file_path`` where Arena calls it without a directory, and return the cache
    directory.

    Arena's wrapper of a SimReady Central prop is made once a process too: the composer's check
    and Arena's build each ask for it, and each time costs a listing from the server and a
    rewritten wrapper, whose new mtime would be a new copy of it for ``retrieve_file_path``. Arena
    imports the function where it calls it, so replacing it on its module reaches every call.
    """
    global _cache_dir, _patched
    if cache_dir is not None:
        _cache_dir = Path(cache_dir).expanduser()
    if not _patched:
        stage_open = Usd.Stage.Open

        def open_local(path, *args, **kwargs):
            return stage_open(localize(path) if isinstance(path, str) else path, *args, **kwargs)

        Usd.Stage.Open = staticmethod(open_local)
        _route_retrievals()
        import isaaclab_arena.assets.simready_central_asset_loader as central

        central.download_and_wrap_central_asset = functools.cache(central.download_and_wrap_central_asset)
        _patched = True
    return _cache_dir


def _route_retrievals() -> None:
    """Send Arena's own downloads of a URL through :func:`localize`, once a process.

    Arena calls Isaac Lab's ``retrieve_file_path`` with no directory for every object it builds, to
    count its rigid bodies, and for every robot on a stand. Since Isaac Lab 3.0 each call writes a
    fresh copy of the asset's rewritten layers to the system's temporary directory, and nothing
    removes them: a sweep of solves left 457 of them, 1.3 GB, there in 17 minutes. A local copy
    Isaac Lab made goes the same way, since it would make another from the URL it came from, here
    in the cache: the rigid-body count runs on the copy :func:`localize` returned. Any other local
    file is copied into the cache once a process while it is unchanged (:func:`_copy_of`). Arena
    imports the function by name, so every module holding it is repointed, and the one it comes
    from for later imports. A forced download is left to Isaac Lab: nothing Arena builds asks for one.

    The robot on a stand is the exception: Arena writes it to a file of its own, which other
    processes read, so its copies stay (:func:`_kept_copy`).
    """
    import isaaclab.utils.assets
    import isaaclab_arena.embodiments.robot_on_stand_utils as robot_on_stand

    retrieve_file_path = isaaclab.utils.assets.retrieve_file_path

    def retrieve(path: str, download_dir: str | None = None, force_download: bool = False) -> str:
        if download_dir is not None or force_download:
            return retrieve_file_path(path, download_dir, force_download)
        url = str(path) if "://" in str(path) else isaaclab.utils.assets.unmirror_file_path(str(path))
        if url:
            return localize(url)
        path = os.path.abspath(path)
        return _copy_of(path, os.path.getmtime(path))

    for module in list(sys.modules.values()):
        if getattr(module, "__dict__", {}).get("retrieve_file_path") is retrieve_file_path:
            module.retrieve_file_path = retrieve
    robot_on_stand.retrieve_file_path = _kept_copy


@functools.cache
def _copy_of(path: str, mtime: float) -> str:
    """Isaac Lab's copy of local file `path`, made in the cache once a process while its `mtime`
    holds, and removed at exit. Arena retrieves the same file for every object built from it."""
    from isaaclab.utils.assets import retrieve_file_path

    local_path = retrieve_file_path(path, str(_cache_dir))
    _note_copy(local_path)
    return local_path


def _kept_copy(path: str) -> str:
    """``retrieve_file_path`` for Arena's robot on a stand, whose USD references what this returns.

    Arena writes that USD to ``~/.cache/isaaclab_arena/usd/robot_on_stand/``, one file for each
    robot, height and footprint, which every process rewrites and any running beside it reads, an
    Isaac Sim run among them. The robot's and the stand's copies are therefore never removed, and a
    later process reuses them, so they do not pile up: a DROID's are 32 MB. A marker in ``kept/``
    names each with the revision of the server's file it was made from, and a new one is made when
    the server reports another; one the server does not answer for reuses the last. The revision is
    the root layer's: a change on the server to a layer it references alone, such as the stand's
    ``franka_table.usd``, is not picked up.
    """
    from filelock import FileLock
    from isaaclab.utils.assets import _remote_fingerprint, retrieve_file_path

    revision = _remote_fingerprint(path) if "://" in path else os.path.getmtime(path)
    marker = _cache_dir / "kept" / hashlib.sha256(path.encode()).hexdigest()[:16]
    marker.parent.mkdir(parents=True, exist_ok=True)
    # One process makes a copy at a time, so two starting together share one.
    with FileLock(f"{marker}.lock"):
        with contextlib.suppress(OSError, ValueError, TypeError):
            kept_revision, kept = json.loads(marker.read_text(encoding="utf-8"))
            if revision in (None, kept_revision) and os.path.isfile(kept):
                return kept
        # ponytail: the copy of an older revision stays, since a robot-on-stand USD another process
        # wrote may still name it; nothing collects them.
        kept = retrieve_file_path(path, str(_cache_dir))
        _relink_url_references(kept)
        temporary = f"{marker}.{os.getpid()}.tmp"
        Path(temporary).write_text(json.dumps([revision, kept]), encoding="utf-8")
        os.replace(temporary, marker)
    return kept


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
    # ponytail: SIGKILL or a crash leaves this process's copies. A sweep at start would need to tell
    # a live process's folders apart (a viewer runs for days), and a kept one (`_kept_copy`): none yet.
    root = os.path.abspath(_cache_dir)
    folder = os.path.relpath(os.path.abspath(local_path), root).split(os.sep)[0]
    if folder.startswith("isaaclab_usd_"):
        if not _copies:
            atexit.register(_drop_copies)
        _copies.add(os.path.join(root, folder))


def _drop_copies() -> None:
    # A copy: a tool still running in a thread may add to the set (`arena-mcp`'s SIGTERM).
    for folder in list(_copies):
        shutil.rmtree(folder, ignore_errors=True)
    _copies.clear()


def _relink_url_references(local_root: str) -> None:
    """Repoint the URL references Isaac Lab leaves in the layers under `local_root` at local copies.

    USD outside Kit cannot open a URL. Since Isaac Lab 3.0 a reference to a file on the server is
    pointed at its local copy by Isaac Lab, in its copy of the layer, and a URL left is one the
    server has no file for, kept as it is, or a UDIM texture, one image per tile, whose tiles it
    leaves on the server: those are fetched here (:func:`_udim`). The walk follows local references,
    since one can be layers down. A download as it came is saved over only where it names a UDIM
    texture by URL itself: Isaac Lab's freshness check compares its record of the server's file with
    the server, never the file, so the edit costs no download, and one that replaces it is
    repointed by the next process to localize it.
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

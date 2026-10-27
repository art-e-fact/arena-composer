"""NVIDIA's SimReady props: physics-ready assets from two places, indexed once
into a local file so they can be searched without a network and picked into a
spec.

- **The Isaac Sim bucket**: ~1,500 props on the S3 bucket Isaac Sim 6.0 ships
  its assets from, the corpus Arena's own SimReady search runs against.
- **SimReady Central**: the Hugging Face dataset ``nvidia/simready-assets``, a
  package per prop, CC-BY-4.0. It holds nearly every bucket prop again, and
  ~1,100 more. Only those more are indexed from it — a prop both have is the
  bucket's, which already works and is one layer rather than a package — and of
  those, only the ones that may be a single rigid body (:func:`central`).

A spec names one as ``registry_name: simready_usd_object`` with the asset's URL
in ``params.usd_path`` — the form Arena's own generator writes — so the index
is what turns a name into that URL. A Central URL is pinned to the commit the
index was built at, since the dataset's head moves every few days and has
withdrawn props. Building the index downloads nothing. A prop's picture is
fetched by :func:`fetch_thumbnails` when somebody asks for it, and the prop
itself when a spec naming it is resolved (:mod:`arena_composer.usd_assets`):
a bucket prop into composer's mirror, a Central one into the Hugging Face cache.

The bucket is plain S3 with anonymous listing, so its half of the index is one
recursive ``ListObjectsV2`` walk of the SimReady root: ~50 pages of a thousand
keys. An asset is recognised by its thumbnail sidecar — every published prop
has a ``.thumbs/256x256/<layer>.png`` beside its entry layer and nothing else in
the tree does — so the asset list falls out of the key list with no file opened.
The index holds names and keys only, a few hundred KB.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from isaaclab_arena.assets.simready_constants import ISAAC_SIMREADY_GA_S3_URL

#: The corpus Arena's own SimReady search runs against, so a pick made here
#: and one made there name the same files.
ROOT = ISAAC_SIMREADY_GA_S3_URL
_parsed = urllib.parse.urlparse(ROOT)
BUCKET = f"{_parsed.scheme}://{_parsed.netloc}"
#: ROOT as a bucket key prefix, `/`-terminated the way S3 names a folder.
PREFIX = _parsed.path.strip("/") + "/"

_S3 = "{http://s3.amazonaws.com/doc/2006-03-01/}"
_SIDECAR = "/.thumbs/256x256/"
_USD = (".usd", ".usda", ".usdc")

#: SimReady Central's dataset on Hugging Face.
CENTRAL = "nvidia/simready-assets"
#: What a Central prop's URL starts with, up to the commit it is pinned at.
CENTRAL_URL = f"https://huggingface.co/datasets/{CENTRAL}/resolve/"
#: The attribution CC-BY-4.0 asks for, shown beside every Central prop.
CENTRAL_CREDIT = f"NVIDIA SimReady Central (https://huggingface.co/datasets/{CENTRAL}), CC-BY-4.0"
#: SimReady Central's search, the one place a prop's rigid-body count can be
#: read without downloading it. See :func:`multi_body`.
CENTRAL_SEARCH = "https://nvidia-simready-central.hf.space/search_hybrid"
#: The most paths one ``paths-info`` request may ask about; a thousand is refused.
PATHS_INFO = 999
#: How wide a Central prop's picture is kept. Published at 1024 px whatever the
#: folder they are in says, ~850 KB each; the bucket's are 256 px.
PICTURE = 256

#: How many pictures are fetched at once. Each is a round trip of roughly a
#: second against a hundred kilobytes, so the wall clock here is latency and
#: not bandwidth: the whole catalogue takes a bit over a minute at this width
#: and several at a quarter of it.
WORKERS = 64


@dataclass(frozen=True)
class RemoteAsset:
    """One prop, as the index records it."""

    #: The asset's folder, e.g. ``Plastic_Basket_A01`` — the readable name. The
    #: layer's own is ``sm_misc_basket_plastic_a01_01``. A Central prop's
    #: package, e.g. ``paint_supplies_spraygun_b01``.
    name: str
    #: Folders between the root and the asset, e.g. ``Residential/Kitchen/Baskets``.
    #: A Central prop's is ``Central/`` and the first word of its name, since
    #: the dataset is one flat folder: ``Central/paint``.
    category: str
    #: Bucket key of the entry layer, or for a Central prop its path in the
    #: dataset, ``<package>/simready_usd/<layer>``.
    key: str
    #: The Central commit a Central prop is pinned at; empty for a bucket one.
    revision: str = ""

    @property
    def usd_path(self) -> str:
        """The URL a spec's ``params.usd_path`` carries."""
        return f"{CENTRAL_URL}{self.revision}/{self.key}" if self.revision else f"{BUCKET}/{self.key}"

    @property
    def credit(self) -> str:
        """The attribution its license asks for, or empty where it asks none here."""
        return CENTRAL_CREDIT if self.revision else ""


def thumbnail_url(usd_path: str) -> str:
    """The 256px sidecar published beside an entry layer.

    Derived from the layer's own URL rather than looked up, so whoever holds a
    spec entry's ``params.usd_path`` can ask for its picture without the index.
    Empty for a path with no folder to look in.
    """
    folder, slash, layer = usd_path.rpartition("/")
    return f"{folder}{_SIDECAR}{layer}.png" if slash else ""


def index_path() -> Path:
    # Imported here: usd_assets loads pxr, which the crawl has no use for.
    from arena_composer.usd_assets import cache_dir

    return cache_dir() / "simready-index.json"


def index_stamp() -> int | None:
    """When the index was last written, or None without one. Readers key
    their copy on it, so a rebuild by another process is picked up."""
    try:
        return index_path().stat().st_mtime_ns
    except FileNotFoundError:
        return None


def load() -> list[RemoteAsset]:
    """The index on disk, empty until :func:`build` has run.

    An index of another root — Arena moved to a newer bucket version — reads as
    absent, rather than offering files a resolve would fetch from the old one.

    The Central props are under a key of their own, beside the bucket's
    `assets`, so an index built before Central was indexed still reads, and
    an older composer reading one built since sees the bucket's props alone.
    """
    document = _document()
    if document is None:
        return []
    listed = _central_of(document)
    return [*(RemoteAsset(**entry) for entry in document["assets"]), *(listed.assets if listed else [])]


def _document() -> dict[str, Any] | None:
    """The index on disk, or None where there is none of this root."""
    try:
        document = json.loads(index_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    return document if document.get("root") == ROOT else None


@dataclass
class Central:
    """SimReady Central, as one build listed it."""

    #: The commit every prop is pinned at.
    revision: str
    #: The props offered.
    assets: list[RemoteAsset]
    #: The packages left out as more than one rigid body: kept in the index for
    #: a build whose search does not answer.
    multi_body: list[str]
    #: What did not go as it should, by the key `reindex_assets` reports it under.
    notes: dict[str, str] = field(default_factory=dict)


def _central_of(document: dict[str, Any]) -> Central | None:
    """The Central an index lists, or None for one that lists none."""
    if not (section := document.get("central")):
        return None
    revision = section["revision"]
    assets = [RemoteAsset(**entry, revision=revision) for entry in section["assets"]]
    return Central(revision, assets, section.get("multi_body", []))


#: How long SimReady Central's half of a build may take. A few seconds as a
#: rule; huggingface_hub's client waits on a stalled connection for ever, and
#: is Arena's too, so the build bounds it here rather than there.
CENTRAL_DEADLINE = 120.0


def build() -> tuple[list[RemoteAsset], dict[str, str]]:
    """List the bucket and SimReady Central and write the index: what it
    holds, and what did not go as it should, by the key `reindex_assets`
    reports it under. About a minute, and network only: nothing is
    downloaded. The pictures are the caller's to ask for, with
    :func:`fetch_thumbnails` — a viewer wants them, an agent searching by name
    does not.

    A Central that cannot be listed costs the build only Central: the index
    keeps the Central props the one before it had, or has none, and says why
    as `central_error`.
    """
    bucket = assets_from_keys(list_keys())
    before = _central_of(_document() or {})
    try:
        listed = _within(CENTRAL_DEADLINE, central, bucket, before.multi_body if before else None)
    except Exception as error:
        kept = f"keeps the {len(before.assets)} props it had from there" if before else "has none from there"
        reason = f"SimReady Central could not be listed, so the index {kept}: {type(error).__name__}: {error}"
        print(f"simready: {reason}", file=sys.stderr)
        listed = replace(before or Central("", [], []), notes={"central_error": reason})
    path = index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "root": ROOT,
        "built": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "assets": [_entry(asset) for asset in bucket],
    }
    if listed.revision:
        document["central"] = {
            "revision": listed.revision,
            "assets": [_entry(asset) for asset in listed.assets],
            "multi_body": listed.multi_body,
        }
    # Written beside and renamed over: the viewer and the MCP server may both
    # be reading, and neither can be handed half a file.
    partial = path.with_name(f"{path.name}.{os.getpid()}.partial")
    partial.write_text(json.dumps(document, indent=1), encoding="utf-8")
    os.replace(partial, path)
    return [*bucket, *listed.assets], listed.notes


def _within(seconds: float, work: Callable[..., Any], *args: Any) -> Any:
    """`work(*args)`, or a TimeoutError once `seconds` have passed. The work
    goes on in a daemon thread, which is left to finish or hang on its own
    without keeping the process from exiting."""
    done: dict[str, Any] = {}

    def run() -> None:
        try:
            done["value"] = work(*args)
        except BaseException as error:
            done["error"] = error

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        raise TimeoutError(f"no answer in {seconds:.0f} s")
    if "error" in done:
        raise done["error"]
    return done["value"]


def _entry(asset: RemoteAsset) -> dict[str, str]:
    """An asset as the index lists it. Without its revision, which a Central
    prop shares with every other, and a bucket entry's three keys alone are
    what an older composer can read."""
    return {"name": asset.name, "category": asset.category, "key": asset.key}


def picture(usd_path: str) -> str | None:
    """The picture published beside this layer, as a file on disk, or None
    where there is none on disk.

    A bucket prop's, or a registered asset's, is the sidecar's mirror. A
    Central prop's is kept apart, downscaled (:func:`_central_picture`).
    """
    if usd_path.startswith(CENTRAL_URL):
        path = _central_picture(usd_path)
        return str(path) if path.is_file() else None
    from arena_composer.usd_assets import mirrored

    return mirrored(thumbnail_url(usd_path))


def _central_picture(usd_path: str) -> Path:
    """Where a Central prop's picture is kept: under the asset cache, named by
    its layer and not the commit, so an index rebuilt at a newer one finds the
    pictures it has. One copy, 256 px — the published one is four times that
    across and nothing here draws it larger than 192 px."""
    return index_path().parent / "simready-central" / f"{usd_path.rsplit('/', 1)[-1]}.png"


def _fetch_central_picture(usd_path: str) -> None:
    """Fetch a Central prop's picture and keep it downscaled. ~850 KB over the
    wire, ~60 KB on disk."""
    from PIL import Image

    with urllib.request.urlopen(thumbnail_url(usd_path), timeout=60) as response:
        image = Image.open(io.BytesIO(response.read()))
        image.thumbnail((PICTURE, PICTURE))
    path = _central_picture(usd_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Renamed into place, as the index is: a picture half written would be on
    # disk, and so never fetched again.
    partial = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.partial")
    image.save(partial, format="PNG", optimize=True)
    os.replace(partial, path)


def fetch_thumbnails(usd_paths: Iterable[str | None]) -> int:
    """Fetch the picture published beside each of these layers, and say how
    many had to be fetched.

    Whatever is already on disk is left alone without asking the server
    whether it is current, so a second pass over the same assets is a stat
    each and no network at all. That is what makes this cheap to run at every
    startup, and what makes an interrupted crawl resumable.

    A picture counts as fetched once it is on disk, not once asking for it did
    not fail: a layer the bucket publishes nothing beside is skipped rather
    than raised, and one that is already a local file is handed back as it was
    given, so neither is counted however often it is offered. An asset that
    composes its USD has no path at all, and is passed over.
    """
    from arena_composer.usd_assets import localize

    def fetch(usd_path: str) -> bool:
        with contextlib.suppress(Exception):
            if usd_path.startswith(CENTRAL_URL):
                _fetch_central_picture(usd_path)
            else:
                localize(thumbnail_url(usd_path))
        return picture(usd_path) is not None

    # A set, because two assets may be listed under one layer, and every
    # duplicate here is a round trip.
    wanted = {path for path in usd_paths if path and thumbnail_url(path) and picture(path) is None}
    if not wanted:
        return 0
    with ThreadPoolExecutor(WORKERS) as pool:
        return sum(pool.map(fetch, wanted))


def list_keys(prefix: str = PREFIX) -> Iterator[str]:
    """Every key under `prefix`, a page of a thousand at a time."""
    token = None
    while True:
        query = {"list-type": "2", "prefix": prefix}
        if token:
            query["continuation-token"] = token
        with urllib.request.urlopen(f"{BUCKET}/?{urllib.parse.urlencode(query)}", timeout=60) as response:
            keys, token = parse_page(response.read())
        yield from keys
        if token is None:
            return


def parse_page(xml: bytes) -> tuple[list[str], str | None]:
    """One ``ListObjectsV2`` page: its keys, and the token for the page after
    it, or None on the last."""
    page = ET.fromstring(xml)
    keys = [key.text for key in page.iter(f"{_S3}Key") if key.text]
    truncated = page.findtext(f"{_S3}IsTruncated") == "true"
    return keys, page.findtext(f"{_S3}NextContinuationToken") if truncated else None


def assets_from_keys(keys: Iterable[str]) -> list[RemoteAsset]:
    """The assets a key listing holds: one per thumbnail sidecar whose layer is
    also listed. A sidecar left behind by a renamed layer would index a 404."""
    keys = set(keys)
    found = []
    for key in keys:
        if _SIDECAR not in key or not key.endswith(".png"):
            continue
        folder, sidecar = key.split(_SIDECAR, 1)
        layer = f"{folder}/{sidecar.removesuffix('.png')}"
        if not layer.endswith(_USD) or layer not in keys:
            continue
        category, _, name = folder.removeprefix(PREFIX).rpartition("/")
        found.append(RemoteAsset(name=name, category=category, key=layer))
    found.sort(key=lambda asset: asset.key)
    return found


def central(bucket: Iterable[RemoteAsset], known_multi_body: Iterable[str] | None = None) -> Central:
    """SimReady Central's head commit, and its props worth offering beside the
    `bucket`'s, pinned at that commit. About ten requests, a few seconds.

    A prop the bucket has too is the bucket's. They are told apart by the entry
    layer's file name, case aside: the bucket indexes ~1,500 of Central's
    ~2,600, and two of Central's layers are named in another case from their
    package.

    Arena spawns a SimReady prop as exactly one rigid body, and refuses the
    rest at resolve, so a prop :func:`multi_body` knows to have more is left
    out. One whose count is not known is offered. Where the search does not
    answer, the props left out are those `known_multi_body` names, the last
    index's, and the build says so as `central_filter`.
    """
    from huggingface_hub import HfApi
    from huggingface_hub.hf_api import RepoFolder

    notes = {}
    if (unwanted := multi_body()) is None:
        unwanted = set(known_multi_body or ())
        kept = (
            "the props the last index left out as more than one rigid body are left out again"
            if unwanted
            else "props are offered whatever their count of rigid bodies"
        )
        notes["central_filter"] = f"unavailable: SimReady Central's search did not answer, so {kept}"
    api = HfApi()
    revision = api.dataset_info(CENTRAL).sha
    root = api.list_repo_tree(CENTRAL, repo_type="dataset", revision=revision)
    taken = {_stem(asset.key) for asset in bucket}
    packages = [
        entry.path
        for entry in root
        if isinstance(entry, RepoFolder) and entry.path not in unwanted and _stem(_guess(entry.path)) not in taken
    ]
    found = [
        RemoteAsset(name=package, category=f"Central/{package.split('_', 1)[0]}", key=key, revision=revision)
        for package, key in _entry_layers(api, revision, packages).items()
        if _stem(key) not in taken
    ]
    return Central(revision, sorted(found, key=lambda asset: asset.key), sorted(unwanted), notes)


def _guess(package: str) -> str:
    """A Central package's entry layer as nearly every one is named."""
    return f"{package}/simready_usd/sm_{package}_01.usd"


def _stem(key: str) -> str:
    """A layer's file name without its suffix or case: what the bucket and
    Central share for the same prop."""
    return key.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()


def _entry_layers(api: Any, revision: str, packages: list[str]) -> dict[str, str]:
    """Each package's entry layer, by its path in the dataset.

    Nearly always ``simready_usd/sm_<package>_01.usd``, or ``.usda``. Both
    names are asked after at once, a thousand paths a request, where listing
    the dataset whole is ~200,000 files at a thousand a page. A package that
    has neither has its entry read from the list every package publishes of
    its own, which lands in the Hugging Face cache where Arena's loader takes
    it from; a package without that list is not offered.

    One request after another, two as a rule: a pool's threads would keep a
    process whose build ran out of time from exiting (:func:`_within`).
    """
    from huggingface_hub import hf_hub_download

    guesses = [_guess(package).removesuffix(".usd") + suffix for package in packages for suffix in (".usd", ".usda")]
    layers = {
        info.path.split("/", 1)[0]: info.path
        for start in range(0, len(guesses), PATHS_INFO)
        for info in api.get_paths_info(
            CENTRAL, guesses[start : start + PATHS_INFO], repo_type="dataset", revision=revision
        )
    }
    for package in packages:
        if package in layers:
            continue
        with contextlib.suppress(Exception):
            roots = f"{package}/.metadata/com.nvidia.simready.root_usds.json"
            listed = hf_hub_download(CENTRAL, roots, repo_type="dataset", revision=revision)
            layers[package] = f"{package}/{json.loads(Path(listed).read_text())['entries'][0]}"
    return layers


#: A Central package in a path SimReady Central's search returns.
_SEARCHED = re.compile(rf"/datasets/{re.escape(CENTRAL)}/resolve/[0-9a-f]{{40}}/([^/]+)/simready_usd/")


#: The most pages :func:`multi_body` reads: ten times what it takes today.
SEARCH_PAGES = 20


def multi_body() -> set[str] | None:
    """The Central packages SimReady Central's search knows to be more than
    one rigid body: ~950 paths in two pages, a second or two. None where the
    search did not answer to the end.

    A prop's count is in its entry layer's metadata, which only opening the
    layer reads, and the search has it indexed. That search is a service and
    not the dataset: it covers other datasets too, left out here, and lags
    this one by days, so a package it has not seen is simply not named. An
    answer naming nothing is taken for none: the dataset has hundreds.
    """
    found: set[str] = set()
    cursor, cursors = None, set()
    try:
        for _ in range(SEARCH_PAGES):
            query = {
                "hybrid_text_query": "",
                "file_extension_include": "usd*",
                "filter_by_properties_numeric": "simready_metadata_rigid_body_count>1",
                "search_path": f"*{CENTRAL}*",
                "limit": 500,
                **({"cursor": cursor} if cursor else {}),
            }
            body = json.dumps(query).encode()
            request = urllib.request.Request(CENTRAL_SEARCH, body, {"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=60) as response:
                page = json.load(response)
            found |= {match[1] for hit in page["hits"] if (match := _SEARCHED.search(hit["source"]["path"]))}
            if not page.get("has_more"):
                return found or None
            # A cursor missing, or one already followed, would page for ever.
            if not (cursor := page.get("next_cursor")) or cursor in cursors:
                break
            cursors.add(cursor)
    except Exception as error:
        print(f"simready: SimReady Central's rigid-body counts: {type(error).__name__}: {error}", file=sys.stderr)
        return None
    print(f"simready: SimReady Central's search paged on past {len(cursors) + 1} page(s); dropped", file=sys.stderr)
    return None


def main() -> int:
    """``python -m arena_composer.simready``: build the index and say how big it is."""
    assets, notes = build()
    print(f"{len(assets)} assets indexed at {index_path()}")
    for key, note in notes.items():
        print(f"{key}: {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""NVIDIA's SimReady props: ~1,500 physics-ready assets on a public bucket,
indexed once into a local file so they can be searched without a network and
picked into a spec.

A spec names one as ``registry_name: simready_usd_object`` with the asset's URL
in ``params.usd_path`` — the form Arena's own generator writes — so the index
is what turns a name into that URL. Building the index downloads nothing.
A prop's picture is fetched by :func:`fetch_thumbnails` when somebody asks for
it, and the prop itself when a spec naming it is resolved — both through the
same mirror every remote asset goes through (:mod:`arena_composer.usd_assets`).

The bucket is plain S3 with anonymous listing, so the index is one recursive
``ListObjectsV2`` walk of the SimReady root: ~50 pages of a thousand keys. An
asset is recognised by its thumbnail sidecar — every published prop has a
``.thumbs/256x256/<layer>.png`` beside its entry layer and nothing else in the
tree does — so the asset list falls out of the key list with no file opened.
The index holds names and keys only, a few hundred KB.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

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

#: How many pictures are fetched at once. Each is a round trip of roughly a
#: second against a hundred kilobytes, so the wall clock here is latency and
#: not bandwidth: the whole catalogue takes a bit over a minute at this width
#: and several at a quarter of it.
WORKERS = 64


@dataclass(frozen=True)
class RemoteAsset:
    """One prop on the bucket, as the index records it."""

    #: The asset's folder, e.g. ``Plastic_Basket_A01`` — the readable name. The
    #: layer's own is ``sm_misc_basket_plastic_a01_01``.
    name: str
    #: Folders between the root and the asset, e.g. ``Residential/Kitchen/Baskets``.
    category: str
    #: Bucket key of the entry layer.
    key: str

    @property
    def usd_path(self) -> str:
        """The URL a spec's ``params.usd_path`` carries."""
        return f"{BUCKET}/{self.key}"


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
    """
    try:
        document = json.loads(index_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    if document.get("root") != ROOT:
        return []
    return [RemoteAsset(**entry) for entry in document["assets"]]


def build() -> list[RemoteAsset]:
    """Crawl the bucket and write the index. About a minute, and network only:
    nothing is downloaded. The pictures are the caller's to ask for, with
    :func:`fetch_thumbnails` — a viewer wants them, an agent searching by name
    does not."""
    assets = assets_from_keys(list_keys())
    path = index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "root": ROOT,
        "built": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "assets": [asdict(asset) for asset in assets],
    }
    # Written beside and renamed over: the viewer and the MCP server may both
    # be reading, and neither can be handed half a file.
    partial = path.with_name(f"{path.name}.{os.getpid()}.partial")
    partial.write_text(json.dumps(document, indent=1), encoding="utf-8")
    os.replace(partial, path)
    return assets


def fetch_thumbnails(usd_paths: Iterable[str]) -> int:
    """Mirror the picture published beside each of these layers, and say how
    many had to be fetched.

    Whatever is already on disk is left alone without asking the server
    whether it is current, so a second pass over the same assets is a stat
    each and no network at all. That is what makes this cheap to run at every
    startup, and what makes an interrupted crawl resumable.

    A picture counts as fetched once it is on disk, not once asking for it did
    not fail: a layer the bucket publishes nothing beside is skipped rather
    than raised, and one that is already a local file is handed back as it was
    given, so neither is counted however often it is offered.
    """
    from arena_composer.usd_assets import localize, mirrored

    def fetch(url: str) -> bool:
        with contextlib.suppress(Exception):
            localize(url)
        return mirrored(url) is not None

    # A set, because two assets may be listed under one layer, and every
    # duplicate here is a round trip.
    wanted = {thumbnail_url(path) for path in usd_paths if path} - {""}
    wanted = {url for url in wanted if mirrored(url) is None}
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


def main() -> int:
    """``python -m arena_composer.simready``: build the index and say how big it is."""
    assets = build()
    print(f"{len(assets)} assets indexed at {index_path()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

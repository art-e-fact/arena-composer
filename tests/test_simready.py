"""The SimReady index: how a bucket listing becomes assets, that a page of the
listing is read the way S3 writes one, which of SimReady Central's props join
them, and how the pictures beside the assets are fetched. Off the network: the
Hub's and the search's answers are recordings."""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from huggingface_hub.hf_api import RepoFile, RepoFolder
from PIL import Image

from arena_composer import simready

PAGE = b"""<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Name>b</Name><Prefix>p/</Prefix>
<NextContinuationToken>tok==</NextContinuationToken><KeyCount>2</KeyCount><MaxKeys>2</MaxKeys>
<IsTruncated>true</IsTruncated>
<Contents><Key>p/a.usd</Key><Size>1</Size></Contents><Contents><Key>p/b.usd</Key><Size>2</Size></Contents>
</ListBucketResult>"""


def test_a_page_yields_its_keys_and_the_token_for_the_next():
    keys, token = simready.parse_page(PAGE)
    assert keys == ["p/a.usd", "p/b.usd"]
    assert token == "tok=="


def test_the_last_page_has_no_token():
    keys, token = simready.parse_page(PAGE.replace(b"true", b"false"))
    assert keys and token is None


def test_an_asset_is_a_sidecar_whose_layer_is_listed():
    root = simready.PREFIX
    keys = [
        f"{root}Residential/Kitchen/Baskets/Plastic_Basket_A01/sm_basket_a01_01.usd",
        f"{root}Residential/Kitchen/Baskets/Plastic_Basket_A01/.thumbs/256x256/sm_basket_a01_01.usd.png",
        f"{root}Residential/Kitchen/Baskets/Plastic_Basket_A01/Textures/sm_basket_a01_BaseColor.png",
        # A sidecar of a layer that is gone would index a 404.
        f"{root}Industrial/Tools/Wrench_A01/.thumbs/256x256/sm_wrench_a01.usd.png",
        f"{root}Industrial/Tools/Wrench_A01/metadata.json",
    ]
    (asset,) = simready.assets_from_keys(keys)
    assert asset.name == "Plastic_Basket_A01"
    assert asset.category == "Residential/Kitchen/Baskets"
    assert asset.usd_path == f"{simready.BUCKET}/{keys[0]}"
    assert simready.thumbnail_url(asset.usd_path) == f"{simready.BUCKET}/{keys[1]}"
    assert simready.thumbnail_url("sm_basket.usd") == "", "no folder, so nowhere to look"


REVISION = "faaa6cea782d370b0b73408674507ad9f7835484"
SPRAY_GUN = simready.RemoteAsset(
    name="paint_supplies_spraygun_b01",
    category="Central/paint",
    key="paint_supplies_spraygun_b01/simready_usd/sm_paint_supplies_spraygun_b01_01.usd",
    revision=REVISION,
)


def test_the_index_is_read_back_unless_its_root_has_moved(tmp_path: Path, monkeypatch):
    index = tmp_path / "index.json"
    monkeypatch.setattr(simready, "index_path", lambda: index)
    # Building an index fetches nothing: an agent searching by name has no use
    # for 170 MB of pictures.
    monkeypatch.setattr(simready, "fetch_thumbnails", lambda usd_paths: pytest.fail("the index fetched pictures"))
    monkeypatch.setattr(
        simready,
        "list_keys",
        lambda: [f"{simready.PREFIX}A/B/x.usd", f"{simready.PREFIX}A/B/.thumbs/256x256/x.usd.png"],
    )
    monkeypatch.setattr(simready, "central", lambda bucket: (REVISION, [SPRAY_GUN]))
    assert simready.load() == []
    built = simready.build()
    assert [asset.name for asset in built] == ["B", "paint_supplies_spraygun_b01"]
    assert simready.load() == built
    assert built[1].usd_path == (
        "https://huggingface.co/datasets/nvidia/simready-assets/resolve/"
        f"{REVISION}/paint_supplies_spraygun_b01/simready_usd/sm_paint_supplies_spraygun_b01_01.usd"
    ), "pinned, in the form Arena's Central loader takes"
    assert (built[0].credit, built[1].credit) == ("", simready.CENTRAL_CREDIT)

    # An older composer builds each of `assets` from the three keys it knows,
    # and reads nothing else: it has to find the bucket's props there as
    # before, and none of Central's.
    document = json.loads(index.read_text())
    assert [set(entry) for entry in document["assets"]] == [{"name", "category", "key"}]
    # And an index it built, with no Central at all, still reads.
    del document["central"]
    index.write_text(json.dumps(document))
    assert simready.load() == built[:1]

    # What is on disk names the old bucket's files, so it reads as no index.
    document["root"] = "https://elsewhere/Assets/Isaac/7.0/Isaac/SimReady"
    index.write_text(json.dumps(document))
    assert simready.load() == []


class _Hub:
    """The Hub's answers about SimReady Central, as recorded at REVISION on
    2026-10-27, cut down to five packages: one the bucket has too, one with a
    `.usda` entry, one more than a rigid body, and one whose entry is named
    in another case from its package."""

    PACKAGES = [
        "IV_pole_a03",
        "cleaning_barSoap_d01",
        "dishware_bowl_a01",
        "misc_basket_plastic_a01",
        "paint_supplies_spraygun_b01",
    ]
    FILES = [
        "IV_pole_a03/simready_usd/sm_IV_pole_a03_01.usd",
        "cleaning_barSoap_d01/simready_usd/sm_cleaning_barsoap_d01_01.usd",
        "dishware_bowl_a01/simready_usd/sm_dishware_bowl_a01_01.usda",
        "misc_basket_plastic_a01/simready_usd/sm_misc_basket_plastic_a01_01.usd",
        "paint_supplies_spraygun_b01/simready_usd/sm_paint_supplies_spraygun_b01_01.usd",
    ]

    def __init__(self) -> None:
        self.asked: list[list[str]] = []

    def dataset_info(self, repo_id):
        return SimpleNamespace(sha=REVISION)

    def list_repo_tree(self, repo_id, path_in_repo=None, *, recursive=False, revision=None, repo_type=None):
        assert (path_in_repo, recursive, revision) == (None, False, REVISION), "listed more than the root"
        return [RepoFolder(path=package, oid="") for package in self.PACKAGES]

    def get_paths_info(self, repo_id, paths, *, repo_type=None, revision=None):
        assert len(paths) <= simready.PATHS_INFO and revision == REVISION
        self.asked.append(list(paths))
        return [RepoFile(path=path, size=1, oid="") for path in paths if path in self.FILES]


def test_central_offers_what_the_bucket_has_not(tmp_path: Path, monkeypatch):
    """Central holds nearly every bucket prop again; those stay the bucket's.
    A prop known to be more than one rigid body is left out, since Arena
    spawns one only as exactly one; an entry named off the usual pattern is
    read from the package's own list of its entries."""
    hub = _Hub()
    monkeypatch.setattr("huggingface_hub.HfApi", lambda: hub)
    roots = tmp_path / "root_usds.json"
    roots.write_text(json.dumps({"format_version": "1.0", "entries": ["simready_usd/sm_cleaning_barsoap_d01_01.usd"]}))
    read: list[str] = []
    monkeypatch.setattr(
        "huggingface_hub.hf_hub_download", lambda repo_id, filename, **kwargs: read.append(filename) or str(roots)
    )
    monkeypatch.setattr(simready, "multi_body", lambda: {"IV_pole_a03"})
    folder = f"{simready.PREFIX}Residential/Kitchen/Baskets/Plastic_Basket_A01"
    layer = "sm_misc_basket_plastic_a01_01.usd"
    bucket = simready.assets_from_keys([f"{folder}/{layer}", f"{folder}/.thumbs/256x256/{layer}.png"])

    revision, found = simready.central(bucket)
    assert revision == REVISION
    assert [(asset.name, asset.category, asset.key) for asset in found] == [
        (
            "cleaning_barSoap_d01",
            "Central/cleaning",
            "cleaning_barSoap_d01/simready_usd/sm_cleaning_barsoap_d01_01.usd",
        ),
        ("dishware_bowl_a01", "Central/dishware", "dishware_bowl_a01/simready_usd/sm_dishware_bowl_a01_01.usda"),
        ("paint_supplies_spraygun_b01", "Central/paint", SPRAY_GUN.key),
    ]
    assert read == ["cleaning_barSoap_d01/.metadata/com.nvidia.simready.root_usds.json"], "one package off the pattern"
    assert sum(hub.asked, []) == [
        f"{package}/simready_usd/sm_{package}_01{suffix}"
        for package in ("cleaning_barSoap_d01", "dishware_bowl_a01", "paint_supplies_spraygun_b01")
        for suffix in (".usd", ".usda")
    ], "asked after a package already left out"


#: A page of SimReady Central's search, as recorded on 2026-10-27, cut down to
#: a hit in the dataset and one in another the search covers too.
SEARCHED = {
    "hits": [
        {"source": {"path": f"hf://huggingface.co/datasets/{path}"}}
        for path in (
            "nvidia/simready-assets/resolve/19512d4dd4b7c0868530efd3ec62277fa2dc1cde/IV_pole_a03/simready_usd/sm_IV_pole_a03_01.usd",
            "Comau/N220-27/resolve/6bc7c706854044358eab7c28cbab1f4daaa4753a/N-220-27/simready_usd/n-220-27.usd",
        )
    ],
    "has_more": True,
    "next_cursor": "next",
}


def test_multi_body_props_are_read_from_the_search_and_fail_soft(monkeypatch):
    """The search is a service in development, so a failure halfway is what
    it named before failing, and every prop it did not name stays offered."""
    asked: list[dict] = []

    def urlopen(request, timeout):
        asked.append(json.loads(request.data))
        if len(asked) > 1:
            raise OSError("503 Service Unavailable")
        return io.BytesIO(json.dumps(SEARCHED).encode())

    monkeypatch.setattr(simready.urllib.request, "urlopen", urlopen)
    assert simready.multi_body() == {"IV_pole_a03"}, "another dataset's package, or none"
    assert asked[0]["filter_by_properties_numeric"] == "simready_metadata_rigid_body_count>1"
    assert asked[1]["cursor"] == "next"


def _png(size: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGBA", (size, size), (200, 40, 40, 255)).save(buffer, format="PNG")
    return buffer.getvalue()


def test_a_central_picture_is_kept_once_and_small(tmp_path: Path, monkeypatch):
    """Central publishes its pictures at 1024 px, ~850 KB each. One copy is
    kept, at the bucket's 256, named by its layer and not the commit, so an
    index rebuilt at a newer commit finds what it has."""
    monkeypatch.setattr(simready, "index_path", lambda: tmp_path / "index.json")
    fetched: list[str] = []

    def urlopen(url, timeout):
        fetched.append(url)
        return io.BytesIO(_png(1024))

    monkeypatch.setattr(simready.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr("arena_composer.usd_assets.localize", lambda url: pytest.fail(f"mirrored {url}"))

    assert simready.picture(SPRAY_GUN.usd_path) is None
    assert simready.fetch_thumbnails([SPRAY_GUN.usd_path]) == 1
    assert fetched == [simready.thumbnail_url(SPRAY_GUN.usd_path)]
    kept = Path(simready.picture(SPRAY_GUN.usd_path))
    assert kept.parent == tmp_path / "simready-central"
    assert Image.open(kept).size == (simready.PICTURE, simready.PICTURE)
    assert [path.name for path in kept.parent.iterdir()] == [kept.name], "a partial file left behind"

    newer = SPRAY_GUN.usd_path.replace(REVISION, "0" * 40)
    assert simready.picture(newer) == str(kept)
    assert simready.fetch_thumbnails([newer]) == 0 and len(fetched) == 1


def test_a_picture_is_fetched_beside_its_layer(monkeypatch):
    """The sidecar is derived from the layer's own URL and not looked up, so an
    asset needs no second field to say where its picture is. One layer that
    publishes none must not cost the rest of the batch theirs."""
    fetched: list[str] = []

    def localize(url: str) -> str:
        if "missing" in url:
            raise FileNotFoundError(url)
        fetched.append(url)
        return url

    monkeypatch.setattr("arena_composer.usd_assets.localize", localize)
    # On disk once fetched, and not before.
    monkeypatch.setattr("arena_composer.usd_assets.mirrored", lambda url: url if url in fetched else None)

    got = simready.fetch_thumbnails(
        [
            "https://bucket/Baskets/Plastic_Basket_A01/sm_misc_basket_a01.usd",
            "https://bucket/a/missing.usd",
            "",
        ]
    )
    assert fetched == ["https://bucket/Baskets/Plastic_Basket_A01/.thumbs/256x256/sm_misc_basket_a01.usd.png"]
    assert got == 1, "a missing sidecar was counted as fetched"


def test_a_picture_already_on_disk_is_not_asked_about_again(monkeypatch):
    """What makes a second pass over the catalogue free, an interrupted crawl
    resumable, and the startup pass affordable: a mirrored copy is taken as
    current without a round trip to say so."""
    monkeypatch.setattr("arena_composer.usd_assets.localize", lambda url: pytest.fail(f"asked for {url}"))
    monkeypatch.setattr("arena_composer.usd_assets.mirrored", lambda url: f"/cache/{url}")

    assert simready.fetch_thumbnails(["https://bucket/a/x.usd", "https://bucket/a/x.usd"]) == 0


def test_a_local_layer_with_no_picture_is_never_counted_as_fetched(tmp_path: Path):
    """Some assets are registered from a directory another SDK downloaded, and
    `usd_assets.localize` hands such a path straight back without complaint.
    Counting that as a fetch would make every startup answer with a megabyte
    of prop list saying nothing had changed."""
    layer = tmp_path / "Broccoli008" / "Broccoli008.usd"
    layer.parent.mkdir()
    layer.touch()

    assert simready.fetch_thumbnails([str(layer)]) == 0


def test_a_layer_isaac_lab_copied_to_localize_goes_when_the_process_does(tmp_path: Path, monkeypatch):
    """Isaac Lab writes a layer whose references it localized into a fresh folder of the cache in
    every process; the composer's cache outlives the process, so its copies go at exit."""
    from arena_composer import usd_assets

    copy = tmp_path / "isaaclab_usd_0123" / "1" / "table.usd"
    copy.parent.mkdir(parents=True)
    copy.write_text("#usda 1.0\n")
    monkeypatch.setattr(usd_assets, "_cache_dir", tmp_path)
    monkeypatch.setattr(usd_assets, "_local_paths", {})
    monkeypatch.setattr("isaaclab.utils.assets.retrieve_file_path", lambda url, download_dir: str(copy))
    assert usd_assets.localize("https://host/table.usd") == str(copy)

    usd_assets._drop_copies()
    assert not copy.parent.parent.exists() and tmp_path.exists()


def test_a_udim_texture_left_on_the_server_has_its_tiles_fetched(tmp_path: Path, monkeypatch):
    """Isaac Lab 3.0 leaves a UDIM texture's reference a URL and its tiles on the server; they are
    fetched from 1001 up to the first missing one, and the reference names the local pattern."""
    from arena_composer import usd_assets

    asked = []

    def retrieve(url: str, download_dir: str) -> str:
        asked.append(url.rsplit(".", 2)[1])
        if url.endswith(".1003.png"):
            raise FileNotFoundError(url)
        return url

    monkeypatch.setattr(usd_assets, "_cache_dir", tmp_path)
    monkeypatch.setattr("isaaclab.utils.assets.retrieve_file_path", retrieve)
    local = usd_assets._udim("https://host/robot/textures/albedo.<UDIM>.png")
    assert asked == ["1001", "1002", "1003"]
    assert local == str(tmp_path / "https" / "host" / "robot" / "textures" / "albedo.<UDIM>.png")


def test_arenas_own_downloads_of_a_url_go_through_the_cache(tmp_path: Path, monkeypatch):
    """Arena calls Isaac Lab's `retrieve_file_path` with no directory for every object it builds,
    and since Isaac Lab 3.0 each call leaves a copy in the temporary directory; a URL, or a copy
    Isaac Lab made of one, goes through `localize` instead, once a process; another local file is
    copied into the cache once a process while it is unchanged; and a named directory is left as
    it was."""
    import types

    import isaaclab_arena.embodiments.robot_on_stand_utils as robot_on_stand

    from arena_composer import usd_assets

    calls = []
    monkeypatch.setattr("isaaclab.utils.assets.retrieve_file_path", lambda *args: calls.append(args) or "raw")
    module = types.ModuleType("arena_module")
    module.retrieve_file_path = sys.modules["isaaclab.utils.assets"].retrieve_file_path
    monkeypatch.setitem(sys.modules, "arena_module", module)
    monkeypatch.setattr(robot_on_stand, "retrieve_file_path", robot_on_stand.retrieve_file_path)
    monkeypatch.setattr(usd_assets, "localize", lambda url: f"cached {url}")
    usd_assets._copy_of.cache_clear()
    copies = {"/cache/isaaclab_usd_1/1/bowl.usd": "https://host/bowl.usd"}
    monkeypatch.setattr("isaaclab.utils.assets.unmirror_file_path", lambda path: copies.get(path, ""))
    local = tmp_path / "bowl.usd"
    local.write_text("#usda 1.0\n")

    usd_assets._route_retrievals()
    assert module.retrieve_file_path("https://host/bowl.usd") == "cached https://host/bowl.usd"
    assert module.retrieve_file_path("/cache/isaaclab_usd_1/1/bowl.usd") == "cached https://host/bowl.usd"
    assert module.retrieve_file_path(str(local)) == module.retrieve_file_path(str(local)) == "raw"
    os.utime(local, ns=(0, 0))
    module.retrieve_file_path(str(local))
    assert module.retrieve_file_path("https://host/bowl.usd", "/elsewhere") == "raw"
    cache = str(usd_assets._cache_dir)
    local_calls = [(str(local), cache, False)] * 2
    assert calls == [*local_calls, ("https://host/bowl.usd", "/elsewhere", False)]
    assert robot_on_stand.retrieve_file_path is usd_assets._kept_copy


def test_a_robot_on_a_stand_keeps_its_copy_and_reuses_it_while_the_server_has_not_changed(
    tmp_path: Path, monkeypatch
):
    """Arena writes a robot on a stand to a file other processes read, which references the copy
    `retrieve_file_path` returned: that copy is never removed, and a later process reuses it until
    the server reports another revision of the file; when it reports none, the last is reused."""
    from arena_composer import usd_assets

    revision: dict = {"hash": "a"}
    made = []

    def retrieve(url: str, download_dir: str) -> str:
        copy = tmp_path / f"isaaclab_usd_{len(made)}" / "1" / "robot.usd"
        copy.parent.mkdir(parents=True)
        copy.write_text("#usda 1.0\n")
        made.append(str(copy))
        return str(copy)

    monkeypatch.setattr(usd_assets, "_cache_dir", tmp_path)
    monkeypatch.setattr(usd_assets, "_copies", set())
    monkeypatch.setattr("isaaclab.utils.assets.retrieve_file_path", retrieve)
    monkeypatch.setattr("isaaclab.utils.assets._remote_fingerprint", lambda url: dict(revision) if revision else None)

    first = usd_assets._kept_copy("https://host/robot.usd")
    assert usd_assets._kept_copy("https://host/robot.usd") == first and made == [first]
    revision["hash"] = "b"
    second = usd_assets._kept_copy("https://host/robot.usd")
    assert second != first
    revision.clear()
    assert usd_assets._kept_copy("https://host/robot.usd") == second and len(made) == 2
    usd_assets._drop_copies()
    assert all(Path(copy).exists() for copy in made)
    assert not list((tmp_path / "kept").glob("*.tmp"))


def test_a_central_prop_is_read_from_the_hugging_face_cache(tmp_path: Path, monkeypatch):
    """A Central package is downloaded where Arena's Central loader downloads
    it, file for file, so neither pulls it twice and composer's own cache
    holds no copy. Once there, it is read with no network at all."""
    import huggingface_hub

    from arena_composer import usd_assets

    monkeypatch.setattr("huggingface_hub.constants.HF_HUB_CACHE", str(tmp_path / "hub"))
    monkeypatch.setattr(usd_assets, "_cache_dir", tmp_path / "composer")
    monkeypatch.setattr(usd_assets, "_local_paths", {})
    package = "paint_supplies_spraygun_b01"
    files = [
        f"{package}/com.nvidia.simready.packaging.json",
        f"{package}/.metadata/LICENSE.txt",
        f"{package}/simready_usd/payloads/base.usda",
        f"{package}/simready_usd/Textures/T_spraygun_BaseColor.png",
        SPRAY_GUN.key,
    ]
    downloaded: list[str] = []

    class Hub:
        def list_repo_tree(self, repo_id, path_in_repo=None, *, recursive=False, revision=None, repo_type=None):
            assert (repo_id, path_in_repo, recursive, revision) == (simready.CENTRAL, package, True, REVISION)
            return [RepoFile(path=path, size=1, oid="") for path in files] + [
                RepoFolder(path=f"{package}/simready_usd", oid="")
            ]

    def hf_hub_download(repo_id, filename, *, repo_type=None, revision=None):
        # Where the real one puts it, which is where `try_to_load_from_cache` looks.
        path = tmp_path / "hub" / f"datasets--{repo_id.replace('/', '--')}" / "snapshots" / revision / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#usda 1.0\n")
        downloaded.append(filename)
        return str(path)

    monkeypatch.setattr(huggingface_hub, "HfApi", Hub)
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", hf_hub_download)

    assert usd_assets.mirrored(SPRAY_GUN.usd_path) is None, "not cached yet"
    local = usd_assets.localize(SPRAY_GUN.usd_path)
    assert local == str(tmp_path / "hub" / "datasets--nvidia--simready-assets" / "snapshots" / REVISION / SPRAY_GUN.key)
    assert sorted(downloaded) == sorted(files), "the whole package, once"
    assert downloaded[-1] == SPRAY_GUN.key, "the entry last, so a cached entry means a whole package"
    assert not (tmp_path / "composer").exists(), "a copy in composer's own cache"
    assert usd_assets.mirrored(SPRAY_GUN.usd_path) == local

    # Another process, offline.
    monkeypatch.setattr(usd_assets, "_local_paths", {})
    monkeypatch.setattr(huggingface_hub, "HfApi", lambda: pytest.fail("asked the Hub about a cached package"))
    assert usd_assets.localize(SPRAY_GUN.usd_path) == local

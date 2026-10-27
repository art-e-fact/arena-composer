"""The SimReady index: how a bucket listing becomes assets, that a page of the
listing is read the way S3 writes one, and how the pictures beside the assets
are fetched."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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
    assert simready.load() == []
    built = simready.build()
    assert built[0].name == "B"
    assert simready.load() == built

    # What is on disk names the old bucket's files, so it reads as no index.
    document = json.loads(index.read_text())
    document["root"] = "https://elsewhere/Assets/Isaac/7.0/Isaac/SimReady"
    index.write_text(json.dumps(document))
    assert simready.load() == []


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

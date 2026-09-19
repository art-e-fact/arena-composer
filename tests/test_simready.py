"""The SimReady index: how a bucket listing becomes assets, and that a page of
the listing is read the way S3 writes one."""

from __future__ import annotations

import json
from pathlib import Path

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
    assert asset.thumbnail == f"{simready.BUCKET}/{keys[1]}"


def test_the_index_is_read_back_unless_its_root_has_moved(tmp_path: Path, monkeypatch):
    index = tmp_path / "index.json"
    monkeypatch.setattr(simready, "index_path", lambda: index)
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

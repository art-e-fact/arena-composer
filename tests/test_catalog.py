"""Search over both asset sources, and the constructor check that keeps a
validated spec buildable. Against the live registry, like the edits: a search
that stubs it tests the uninteresting half."""

from __future__ import annotations

import socket
from pathlib import Path

from arena_composer import catalog, simready


def test_a_remote_asset_is_found_with_the_params_to_add_it(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(simready, "index_path", lambda: tmp_path / "index.json")
    folder = f"{simready.PREFIX}Residential/Kitchen/Baskets/Plastic_Basket_A01"
    monkeypatch.setattr(
        simready,
        "list_keys",
        lambda: [f"{folder}/sm_misc_basket_plastic_a01_01.usd", f"{folder}/.thumbs/256x256/sm_misc_basket_plastic_a01_01.usd.png"],
    )
    simready.build()

    # The folder name, the category and the layer's file name all count.
    for query in ("plastic basket", "kitchen basket", "sm_misc"):
        (hit,) = [asset for asset in catalog.search(query) if asset.params]
        assert hit.name == "Plastic_Basket_A01", query
    assert hit.registry_name == "simready_usd_object"
    assert hit.role == "object" and hit.object_type == "rigid"
    assert hit.params == {"usd_path": hit.usd_path}
    assert hit.usd_path.startswith(simready.ROOT)


def test_without_an_index_search_is_the_registry(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(simready, "index_path", lambda: tmp_path / "none.json")
    assert catalog.remote() == ()
    assert catalog.search("banana")[0].registry_name == "banana_ycb_robolab"


def test_listing_assets_stays_off_the_network(monkeypatch):
    # Lightwheel's assets resolve their USD by asking its server when read.
    def offline(*args, **kwargs):
        raise OSError("no network in this test")

    monkeypatch.setattr(socket, "getaddrinfo", offline)
    catalog.catalog.cache_clear()
    microwave = next(asset for asset in catalog.catalog() if asset.registry_name == "microwave")
    assert microwave.usd_path is None


def test_only_the_generic_simready_object_needs_a_param():
    assert catalog.missing_params("simready_usd_object", {}) == ["usd_path"]
    assert catalog.missing_params("simready_usd_object", {"usd_path": "x.usd"}) == []
    assert catalog.missing_params("banana_ycb_robolab", {}) == []

"""What the viewer is told about assets, and when.

The registered assets are a fixed ~30 kB that rides every message. The SimReady
props are a megabyte that changes only when the index is rebuilt or a picture is
fetched, so they ride the message that changed them and no other.
"""

from __future__ import annotations

from pathlib import Path

from isaaclab.utils.assets import _mirror_path

from arena_composer import catalog, payload, simready, usd_assets


def test_a_prop_carries_its_picture_once_that_is_on_disk(tmp_path: Path, monkeypatch):
    """The viewer draws from a file and opens no socket, so it is given the
    mirrored path — and nothing at all until there is one to give."""
    # The index lives in the asset cache, so this moves both.
    monkeypatch.setattr(usd_assets, "_cache_dir", tmp_path)
    folder = f"{simready.PREFIX}Residential/Kitchen/Baskets/Plastic_Basket_A01"
    layer, sidecar = f"{folder}/sm_basket_a01.usd", f"{folder}/.thumbs/256x256/sm_basket_a01.usd.png"
    monkeypatch.setattr(simready, "list_keys", lambda: [layer, sidecar])
    simready.build()

    (prop,) = payload._remote()
    assert prop["name"] == "Plastic_Basket_A01"
    assert prop["registry_name"] == "simready_usd_object"
    assert prop["params"] == {"usd_path": f"{simready.BUCKET}/{layer}"}
    assert prop["thumbnail"] == "", "a picture nobody has fetched is not a path"

    # Where `view._thumbnails` would have put it. Taken from Isaac Lab's own
    # function, so a mirror layout that moves takes this with it.
    mirrored = Path(_mirror_path(f"{simready.BUCKET}/{sidecar}", str(tmp_path)))
    mirrored.parent.mkdir(parents=True, exist_ok=True)
    mirrored.write_bytes(b"not really a png")

    (prop,) = payload._remote()
    assert prop["thumbnail"] == str(mirrored)


def test_the_props_ride_only_the_message_that_changed_them(tmp_path: Path, monkeypatch):
    """An absent list and an empty one are different answers: absent leaves the
    panel's browser as it is, where empty would blank it."""
    monkeypatch.setattr(usd_assets, "_cache_dir", tmp_path)
    dump = {"assets": [], "spec": {}}

    assert payload.build_payload(dump, remote=False)["remote"] is None
    assert payload.build_payload(dump)["remote"] == [], "no index yet, but the list was asked for"
    # The registered half carries no parameters, which is what tells the panel
    # an entry naming one needs nothing beyond the registry name.
    assert all(asset["params"] == {} for asset in payload._vocabulary()["assets"])


def test_a_registered_asset_is_offered_a_picture_too(tmp_path: Path, monkeypatch):
    """A picture is published beside a layer, whoever published it, so nothing
    about asking for one is the props'. What stays theirs is `params`: a pick on
    a registered asset writes an entry carrying the registry name and nothing
    else, so the URL travels beside it and not in it."""
    monkeypatch.setattr(usd_assets, "_cache_dir", tmp_path)
    url = "https://bucket/Props/YCB/Axis_Aligned_Physics/003_cracker_box.usd"
    asset = catalog.Asset(
        registry_name="cracker_box",
        role="object",
        tags=["food"],
        object_type="rigid",
        name="cracker_box",
        usd_path=url,
    )

    encoded = payload._asset(asset)
    assert encoded["usd_path"] == url, "what the panel asks for a picture by"
    assert encoded["params"] == {}, "and what a pick writes, which is nothing"
    assert encoded["thumbnail"] == ""

    sidecar = Path(_mirror_path(simready.thumbnail_url(url), str(tmp_path)))
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_bytes(b"not really a png")
    assert payload._asset(asset)["thumbnail"] == str(sidecar)


def test_an_asset_with_no_published_usd_is_never_asked_about(tmp_path: Path, monkeypatch):
    """Some registered assets compose their USD instead of naming one. There is
    no layer to look beside, so they carry neither field and the panel leaves
    the space blank rather than asking forever."""
    monkeypatch.setattr(usd_assets, "_cache_dir", tmp_path)
    composed = catalog.Asset(
        registry_name="deformable_cube", role="object", tags=[], object_type="deformable",
        name="deformable_cube",
    )
    assert payload._asset(composed)["usd_path"] == ""
    assert payload._asset(composed)["thumbnail"] == ""


def test_an_asset_registered_from_a_local_directory_draws_its_own_sidecar(tmp_path: Path, monkeypatch):
    """Not every registered asset names a URL — some come from a directory
    another SDK has already downloaded. There is nothing to mirror for one of
    those, but the picture beside the layer is as much on disk as a mirrored
    one, and the panel has to be told where it is."""
    monkeypatch.setattr(usd_assets, "_cache_dir", tmp_path / "mirror")
    layer = tmp_path / "sdk" / "Broccoli008" / "Broccoli008.usd"
    sidecar = layer.parent / ".thumbs" / "256x256" / "Broccoli008.usd.png"
    sidecar.parent.mkdir(parents=True)

    asset = catalog.Asset(
        registry_name="broccoli", role="object", tags=[], object_type="rigid",
        name="broccoli", usd_path=str(layer),
    )
    assert payload._asset(asset)["thumbnail"] == "", "no picture beside the layer, so nothing to draw"

    sidecar.write_bytes(b"not really a png")
    assert payload._asset(asset)["thumbnail"] == str(sidecar)

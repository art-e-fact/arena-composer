"""What the viewer is told about assets, and when.

The registered assets are a fixed ~30 kB that rides every message. The SimReady
props are a megabyte that changes only when the index is rebuilt or a picture is
fetched, so they ride the message that changed them and no other.
"""

from __future__ import annotations

from pathlib import Path

from isaaclab.utils.assets import _mirror_path

from arena_composer import payload, simready, usd_assets


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

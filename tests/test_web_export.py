"""`web/export.py`: a payload's files copied out of the asset cache for the web viewer."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import msgpack

_spec = importlib.util.spec_from_file_location("web_export", Path(__file__).parents[1] / "web" / "export.py")
export = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(export)


def test_every_named_file_is_copied_and_renamed_and_a_missing_one_dropped(tmp_path: Path):
    """The browser can only ask the page's server, so every path the viewer loads has to name a
    copy beside the page, and one that names nothing must not leave the viewer asking for it."""
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "wood.png").write_bytes(b"wood")
    (cache / "basket.usd.png").write_bytes(b"basket")
    scene = {
        "geometries": [[{"positions": b"\x00\x01", "textures": {"base_color": str(cache / "wood.png"), "normal": "/gone.png"}}]],
        "vocabulary": {"assets": [{"thumbnail": ""}]},
        "remote": [{"thumbnail": str(cache / "basket.usd.png")}, {"thumbnail": "/gone.png"}],
    }
    payload = tmp_path / "scene.msgpack"
    payload.write_bytes(msgpack.packb(scene, use_bin_type=True))
    out = tmp_path / "site"

    exported = export.export(payload, out)
    sent = msgpack.unpackb(exported.read_bytes(), raw=False)

    node = sent["geometries"][0][0]
    assert list(node["textures"]) == ["base_color"], "a missing texture keeps its slot"
    assert (out / "assets" / node["textures"]["base_color"]).read_bytes() == b"wood"
    assert node["positions"] == b"\x00\x01", "a blob came back as something else"
    picture = sent["remote"][0]["thumbnail"]
    assert picture.endswith(".png") and (out / "assets" / picture).read_bytes() == b"basket"
    assert sent["remote"][1]["thumbnail"] == "" == sent["vocabulary"]["assets"][0]["thumbnail"]
    assert not list((out / "assets").glob("*.part"))

    # Exported again, from where it landed: names already under `assets` are kept, not dropped.
    assert msgpack.unpackb(export.export(exported, out).read_bytes(), raw=False) == sent

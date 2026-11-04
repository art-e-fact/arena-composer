"""Ready packed scenes for the web viewer: ``python web/export.py OUT scene.msgpack...``.

A payload names its textures, and the props' pictures, by absolute paths into this machine's asset
cache, where the viewer reads them off disk. A browser can only ask the page's server, so each file
named is copied into ``OUT/assets/`` under a name taken from its contents — scenes that share a
texture share the copy — and the payload is rewritten to name the copy, which Bevy fetches from
``assets/`` beside the page. It lands at ``OUT/<its name>.msgpack``, for the page's ``?payload=``.

A path that names no file is dropped: a texture slot goes, and a picture is left empty, which the
viewer draws none for.
"""

from __future__ import annotations

import functools
import hashlib
import sys
from pathlib import Path
from typing import Any

import msgpack


def export(payload: Path, out: Path) -> Path:
    """Copy what `payload` names into `out/assets`, and write it rewritten to `out`."""
    assets = out / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    scene = msgpack.unpackb(payload.read_bytes(), raw=False, strict_map_key=False)
    _localize(scene, functools.partial(_copy, assets))
    target = out / payload.name
    # As `payload.encode` packs it, so the viewer reads the same types back.
    target.write_bytes(msgpack.packb(scene, use_bin_type=True))
    return target


def _localize(node: Any, copy: Any) -> None:
    """Point every texture and picture under `node` at its copy, in place."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "textures" and isinstance(value, dict):
                node[key] = {slot: name for slot, path in value.items() if (name := copy(path))}
            elif key == "thumbnail" and isinstance(value, str):
                node[key] = copy(value)
            else:
                _localize(value, copy)
    elif isinstance(node, list):
        for value in node:
            _localize(value, copy)


@functools.cache
def _copy(assets: Path, path: str) -> str:
    """The name under `assets` of the file at `path`, copied there once; empty if there is none.

    A name already under `assets` is kept, so a payload exported once exports again unchanged.
    """
    source = Path(path)
    if path and not source.is_absolute() and (assets / path).is_file():
        return path
    if not path or not source.is_file():
        return ""
    data = source.read_bytes()
    # ponytail: named by content, so identical bytes used as colour in one slot and as data in
    # another would share one colour space (Bevy keys a load by its path); no file in the asset
    # caches is both. Put the source path in the name too if one ever is.
    name = hashlib.sha256(data).hexdigest()[:16] + source.suffix
    target = assets / name
    if not target.exists():
        # Under another name until whole, so an interrupted run leaves no half-copy behind this one.
        part = target.with_name(name + ".part")
        part.write_bytes(data)
        part.replace(target)
    return name


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__.splitlines()[0])
    out = Path(sys.argv[1])
    for payload in sys.argv[2:]:
        print(export(Path(payload), out))

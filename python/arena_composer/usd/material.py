"""The material bound to a prim, reduced to what a PBR viewer can use.

USD barely constrains how a surface is described, and these assets arrive in
three dialects that spell the same quantity differently: ``UsdPreviewSurface``,
Omniverse's OmniPBR/OmniSurface MDL shaders, and the occasional bare
``displayColor``. Rather than branch per dialect, every field below is a tuple
of input names tried in order — the first that resolves wins.

Colours are linear, as USD and MDL author them, and the viewer reads them so.

Texture paths come from ``Sdf.AssetPath.resolvedPath``, never ``.path``: a USD
authors them relative to the layer that wrote them, so only the resolved value
is anchored. Paths that do not exist are dropped here rather than passed on,
which is what keeps the viewer's asset server quiet. A UDIM path keeps its
``<UDIM>`` for :mod:`.extract` to split the mesh by.

What is deliberately not handled: MDL materials whose look lives in the ``.mdl``
file rather than in USD inputs. Reading those needs an MDL parser; they come out
with whatever constants the shader happens to author, usually flat grey.
"""

from __future__ import annotations

import glob
import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import numpy as np
from pxr import Sdf, UsdGeom, UsdShade

from ..usd_assets import cache_dir

# Input names to try, most specific dialect first. `diffuseColor` appears in
# both the colour and the texture list because UsdPreviewSurface uses one input
# for both — a constant when unconnected, a texture node when connected.
_BASE_COLOR = ("diffuseColor", "base_color_constant", "ColorAlbedo")
_OPACITY = ("opacity", "opacity_constant")
_METALLIC = ("metallic", "metallic_constant")
_ROUGHNESS = ("roughness", "reflection_roughness_constant")
_EMISSIVE = ("emissiveColor",)

# OmniPBR's own inputs, which SimPBR and the vendor modules built on OmniPBR
# keep, and which it reads differently from every other dialect: see `_albedo`
# and `_orm`.
_OMNIPBR_INPUTS = ("diffuse_color_constant", "diffuse_texture", "diffuse_tint", "ORM_texture")
_OMNIPBR_MODULES = ("OmniPBR", "SimPBR")

_BASE_COLOR_TEX = (
    "diffuseColor", "diffuse_texture", "base_color_texture", "diffuse_color_texture",
    "AlbedoTexture", "BaseColor",
)
_NORMAL_TEX = ("normal", "normalmap_texture", "normal_map_texture", "MainNormalInput", "Normal")
# Maps packing occlusion, roughness and metallic into one image, in glTF's
# channel order: R occlusion, G roughness, B metallic. Bevy reads both
# `metallic_roughness` and `occlusion` that way, so one file feeds both slots,
# except where OmniPBR's rules for its `ORM_texture` say otherwise (`_orm`).
# The names are self-describing in the assets that use them —
# `AO_R_Rough_G_Metallic_B_` resolves to `*_ao_r_m.png`, `MergeMapInput` to `*_ORM.png`.
_ORM_TEX = ("ORM_texture", "MergeMapInput", "AO_R_Rough_G_Metallic_B_")
# `metallic` belongs here but not in the occlusion list: UsdPreviewSurface
# connects it to a metallic-roughness pair whose red channel means nothing.
# `_preview_maps` then reads UsdPreviewSurface's pair again, by channel.
# OmniPBR's separate single-channel `metallic_texture` and
# `reflectionroughness_texture` cannot go here at all — handing over either makes
# the renderer read a roughness out of a metallic map's green channel. `_orm`
# packs the pair into one image instead.
_METALLIC_ROUGHNESS_TEX = ("metallic",) + _ORM_TEX
_EMISSIVE_TEX = ("emissiveColor", "Emission")
_OCCLUSION_TEX = ("occlusion", "ao_texture") + _ORM_TEX

# What a texture-reading shader calls the input holding its file path.
_FILE_INPUTS = ("file", "texture", "diffuse_texture", "normalmap_texture")


@dataclass(slots=True)
class Material:
    """A `StandardMaterial` in all but name. Colours are linear."""

    # The light grey a material comes out in when it says nothing this can read.
    base_color: tuple[float, float, float] = (0.6, 0.6, 0.6)
    opacity: float = 1.0
    metallic: float = 0.0
    roughness: float = 0.5
    emissive: tuple[float, float, float] = (0.0, 0.0, 0.0)
    double_sided: bool = False
    textures: dict[str, str] = field(default_factory=dict)
    # The colour slots whose texture holds linear values rather than sRGB (`_srgb`).
    linear: list[str] = field(default_factory=list)


def read_material(prim: Any, fallback: Any = None) -> Material:
    """The material bound to ``prim``, falling back to ``fallback``'s binding.

    ``fallback`` is the parent mesh: a `GeomSubset` that binds nothing inherits
    what its mesh bound, which is how a partially-subset mesh keeps one look.
    """
    # `doubleSided` is the gprim's, never the subset's — a `GeomSubset` is a set
    # of faces, not a surface of its own.
    material = Material(double_sided=_double_sided(fallback if fallback is not None else prim))
    shader = _surface_shader(_bound_material(prim) or _bound_material(fallback))
    if shader is None:
        _apply_display_color(material, prim if _display_color(prim) is not None else fallback)
        return material

    # OmniPBR draws its emission only with `enable_emission` on, and it is off
    # unless authored: the office table's frame authors a red glow it never shows.
    emissive, emissive_tex = _EMISSIVE, _EMISSIVE_TEX
    if _constant(shader, ("enable_emission",)) is True:
        emissive, emissive_tex = emissive + ("emissive_color",), emissive_tex + ("emissive_mask_texture",)

    for slot, names in (
        ("base_color", _BASE_COLOR_TEX),
        ("normal", _NORMAL_TEX),
        ("metallic_roughness", _METALLIC_ROUGHNESS_TEX),
        ("emissive", emissive_tex),
        ("occlusion", _OCCLUSION_TEX),
    ):
        if (file_input := _texture_input(shader, names)) is not None:
            material.textures[slot] = path = _resolve(file_input)
            if slot in ("base_color", "emissive") and not _srgb(file_input, path):
                material.linear.append(slot)
    # Lightwheel's restyled kitchens write their colour map into the normal-map
    # input too. Read as a normal map it tips every normal off the surface, and
    # greyed their cabinets; RTX draws them as if they had none.
    if material.textures.get("normal") == material.textures.get("base_color"):
        material.textures.pop("normal", None)

    omnipbr = any(shader.GetInput(name) for name in _OMNIPBR_INPUTS) or _mdl_module(shader).startswith(
        _OMNIPBR_MODULES
    )
    _albedo(shader, material, omnipbr)
    # Fractions, clamped: agibot's A2D authors a roughness of 45.25, which RTX
    # clamps and Bevy would take at its word.
    if (opacity := _scalar(_constant(shader, _OPACITY))) is not None:
        material.opacity = _unit(opacity)
    if (metallic := _scalar(_constant(shader, _METALLIC))) is not None:
        material.metallic = _unit(metallic)
    if (roughness := _scalar(_constant(shader, _ROUGHNESS))) is not None:
        material.roughness = _unit(roughness)
    if (color := _constant(shader, emissive)) is not None:
        material.emissive = _rgb(color)
    if omnipbr:
        _orm(shader, material)
    elif shader.GetIdAttr().Get() == "UsdPreviewSurface":
        _preview_maps(shader, material)
    return material


def _albedo(shader: UsdShade.Shader, material: Material, omnipbr: bool) -> None:
    """Set the base colour the way the shader's dialect combines a constant with a texture."""
    textured = "base_color" in material.textures
    if omnipbr:
        # OmniPBR takes the texture when it has one, scaled by `albedo_brightness`,
        # and `diffuse_color_constant` (0.2 unless authored) when it has not, then
        # multiplies by `diffuse_tint`. The constant never tints the texture.
        # ponytail: `albedo_add` and `albedo_desaturation` act on the texture too, which a
        # multiplier cannot (galileo grades its textures with them); a texture cache could.
        brightness = _scalar(_constant(shader, ("albedo_brightness",)))
        if not textured:
            constant = _constant(shader, ("diffuse_color_constant",))
            color = _rgb(0.2 if constant is None else constant)
        elif brightness == 0:
            # Nothing of the texture is left but the offset: the office table's chrome top.
            del material.textures["base_color"]
            color = _rgb(_scalar(_constant(shader, ("albedo_add",))) or 0.0)
        else:
            color = _rgb(1.0 if brightness is None else brightness)
        tint = _constant(shader, ("diffuse_tint",))
        material.base_color = tuple(c * t for c, t in zip(color, _rgb(1.0 if tint is None else tint)))
    elif (color := _constant(shader, _BASE_COLOR)) is not None:
        material.base_color = _rgb(color)
    elif textured:
        # A texture with no constant beside it is the colour itself, as
        # UsdPreviewSurface's `diffuseColor` connected to one is.
        material.base_color = (1.0, 1.0, 1.0)


def _orm(shader: UsdShade.Shader, material: Material) -> None:
    """OmniPBR's metallic, roughness and occlusion: what its maps are let do.

    It blends each constant toward a map's channel by an influence,
    `lerp(constant, map, influence)`. Under `enable_ORM_texture`, off unless authored
    in all but SimPBR, the map is the ORM one; otherwise it is a separate map for each,
    which Bevy needs packed into one (`_pack`). A map it is told to read and not given
    reads as 0: robolab's fruit run SimPBR with no ORM map, and RTX draws them glossy.
    Bevy can only multiply a map by a factor, which is exact where the influence is 1
    or the constant 0: Lightwheel's kitchens author both influences 1 on all but a few
    hundred of their 11k maps. Multiplying by the constants instead took every metal in
    them for a dielectric.
    """
    enabled = _constant(shader, ("enable_ORM_texture",))
    if enabled is None:
        enabled = _mdl_module(shader).startswith("SimPBR")
    metal = _scalar(_constant(shader, ("metallic_texture_influence",))) or 0.0
    rough = _scalar(_constant(shader, ("reflection_roughness_texture_influence",))) or 0.0
    orm = material.textures.pop("metallic_roughness", None)
    if enabled:
        maps = metal_map = rough_map = orm
    else:
        # The first channel of each.
        metal_map = (path, 0) if metal and (path := _texture(shader, ("metallic_texture",))) else None
        rough_map = (path, 0) if rough and (path := _texture(shader, ("reflectionroughness_texture",))) else None
        maps = _pack(metal_map, rough_map) if metal_map or rough_map else None
    material.metallic = (metal if metal_map else 0.0) + material.metallic * (1 - metal)
    material.roughness = (rough if rough_map else 0.0) + material.roughness * (1 - rough)
    if maps and (metal or rough):
        material.textures["metallic_roughness"] = maps
    # Occlusion darkens OmniPBR's colour only by `ao_to_diffuse`, 0 unless authored;
    # 2 of the library's 18k OmniPBR shaders do. It made every kitchen cabinet dark.
    if not _scalar(_constant(shader, ("ao_to_diffuse",))):
        material.textures.pop("occlusion", None)


def _preview_maps(shader: UsdShade.Shader, material: Material) -> None:
    """UsdPreviewSurface's metallic and roughness: a connected input takes a channel of its
    map in place of its constant.

    SimReady's props connect both to one map, metallic blue and roughness green, as
    Bevy reads it; with the factors left at the unconnected constants' defaults, 0 and
    0.5, none of their metal drew as metal. A map in any other layout is packed into
    that one (`_pack`): a few of them connect roughness alone, to a map's red channel.
    """
    maps = {}
    for name in ("metallic", "roughness"):
        shader_input = shader.GetInput(name)
        source = shader_input.GetConnectedSource() if shader_input else None
        if source and (file_input := _texture_input(shader, (name,))):
            # The reader's output names the channel: `r`, `g`, `b`, or `rgb` for red.
            maps[name] = (_resolve(file_input), max("rgba".find(source[1][:1]), 0))
    material.textures.pop("metallic_roughness", None)
    if not maps:
        return
    roughness = maps.get("roughness")
    if roughness and roughness[1] == 1 and maps.get("metallic") == (roughness[0], 2):
        material.textures["metallic_roughness"] = roughness[0]
    else:
        material.textures["metallic_roughness"] = _pack(maps.get("metallic"), roughness)
    for name in maps:
        setattr(material, name, 1.0)


def _pack(metallic: tuple[str, int] | None, roughness: tuple[str, int] | None) -> str:
    """Separate metallic and roughness maps packed as Bevy reads them: one image,
    roughness green and metallic blue.

    Each source is a file and the channel to take from it. A map left out is white, so
    its constant carries alone. A UDIM pair is packed tile by tile into a UDIM path of
    its own. The images are written once, under the asset cache, and found again by
    their sources' paths and modification times.
    """
    from PIL import Image

    sources = {"metallic": metallic, "roughness": roughness}
    files = {name: _tile_files(source[0]) for name, source in sources.items() if source}
    stamp = [(name, sources[name][1], [(f, os.stat(f).st_mtime_ns) for f in files[name].values()]) for name in files]
    key = hashlib.sha1(repr(stamp).encode()).hexdigest()[:16]
    udim = any("<UDIM>" in source[0] for source in sources.values() if source)
    packed = cache_dir() / "textures" / (f"{key}.<UDIM>.png" if udim else f"{key}.png")

    def channel(name: str, tile: str) -> Image.Image | None:
        if tile not in files.get(name, {}):
            return None
        image = Image.open(files[name][tile])
        pixels = np.asarray(image.convert("RGBA") if image.mode == "P" else image)
        pixels = pixels[..., min(sources[name][1], pixels.shape[2] - 1)] if pixels.ndim == 3 else pixels
        # 16-bit greys open as 16- or 32-bit integers; colour opens as 8-bit already.
        return Image.fromarray((pixels if pixels.dtype == np.uint8 else pixels / 257).astype(np.uint8))

    for tile in sorted(set().union(*(tiles.keys() for tiles in files.values()))):
        out = Path(str(packed).replace("<UDIM>", tile))
        if out.exists():
            continue
        metal, rough = channel("metallic", tile), channel("roughness", tile)
        size = max(c.size for c in (metal, rough) if c)
        white = Image.new("L", size, 255)
        metal, rough = (c.resize(size) if c else white for c in (metal, rough))
        out.parent.mkdir(parents=True, exist_ok=True)
        # Written aside and moved in, so a render running beside this never reads half a file.
        partial = out.with_suffix(f".{os.getpid()}.png")
        Image.merge("RGB", (white, rough, metal)).save(partial, compress_level=1)
        os.replace(partial, out)
    return str(packed)


def _tile_files(path: str) -> dict[str, str]:
    """The files a path names, by tile: each tile's of a UDIM path, else the one file, as tile ''."""
    if "<UDIM>" not in path:
        return {"": path}
    head, tail = path.split("<UDIM>")
    tiles = glob.glob(glob.escape(head) + "1[0-9][0-9][0-9]" + glob.escape(tail))
    return {tile[len(head) : len(tile) - len(tail)]: tile for tile in tiles}


def _mdl_module(shader: UsdShade.Shader) -> str:
    """The file name of the MDL module a shader runs, or '' for one that runs none."""
    asset = shader.GetSourceAsset("mdl")
    return os.path.basename(asset.path) if asset else ""


def _bound_material(prim: Any) -> UsdShade.Material | None:
    """The material bound to ``prim``, following inherited bindings up the tree."""
    if prim is None or not prim.IsValid():
        return None
    material = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial()[0]
    return material if material else None


def _surface_shader(material: UsdShade.Material | None) -> UsdShade.Shader | None:
    """The shader driving a material's surface, from whichever render context has one.

    The empty render context is the universal one, covering `UsdPreviewSurface`;
    Omniverse assets author only ``outputs:mdl:surface``, which
    `ComputeSurfaceSource` finds when asked for the ``mdl`` context by name.
    """
    if material is None:
        return None
    for context in ("", "mdl"):
        shader = material.ComputeSurfaceSource(context)[0]
        if shader:
            return shader
    return None


def _inputs(shader: UsdShade.Shader, names: tuple[str, ...]) -> Iterator[UsdShade.Input]:
    for name in names:
        if shader_input := shader.GetInput(name):
            yield shader_input


def _constant(shader: UsdShade.Shader, names: tuple[str, ...]) -> Any:
    """The first authored constant among ``names``; connected inputs are textures, not constants."""
    for shader_input in _inputs(shader, names):
        if shader_input.HasConnectedSource():
            continue
        value = shader_input.Get()
        if value is not None and not isinstance(value, Sdf.AssetPath):
            return value
    return None


def _texture(shader: UsdShade.Shader, names: tuple[str, ...]) -> str | None:
    """The file an input reads, whether it holds the path itself or connects to a reader."""
    file_input = _texture_input(shader, names)
    return _resolve(file_input) if file_input else None


def _texture_input(shader: UsdShade.Shader, names: tuple[str, ...]) -> UsdShade.Input | None:
    """The input holding the file an input reads: the input itself, as MDL authors it, or the
    file input of the texture reader it connects to."""
    for shader_input in _inputs(shader, names):
        if _resolve(shader_input):
            return shader_input
        if source := shader_input.GetConnectedSource():
            reader = UsdShade.Shader(source[0].GetPrim())
            for file_input in _inputs(reader, _FILE_INPUTS):
                if _resolve(file_input):
                    return file_input
    return None


def _srgb(file_input: UsdShade.Input, path: str) -> bool:
    """Whether a colour map holds sRGB, as RTX reads it: as the USD authors it, on the input or as
    a reader's `sourceColorSpace`, and where it says `auto` or nothing, by the file. MDL's default
    gamma and USD's `auto` both take 8 bits a channel for sRGB and more for linear: Lightwheel's
    16-bit floor tiles say nothing, and drew black where RTX draws them grey.
    """
    attribute = file_input.GetAttr()
    space = attribute.GetColorSpace() if attribute.HasColorSpace() else ""
    if not space and (source := UsdShade.Shader(file_input.GetPrim()).GetInput("sourceColorSpace")):
        space = source.Get() or ""
    if space in ("sRGB", "raw"):
        return space == "sRGB"
    # A UDIM path is its tiles', which share a format.
    try:
        with open(path.replace("<UDIM>", "1001"), "rb") as image:
            header = image.read(25)
    except OSError:
        return True
    # A PNG's bit depth is the 25th byte, in its header; anything else is taken for 8-bit.
    return not (header.startswith(b"\x89PNG") and len(header) > 24 and header[24] > 8)


def _resolve(shader_input: UsdShade.Input) -> str | None:
    """An existing local file for the asset path an input holds, or None.

    ``resolvedPath`` is the value USD anchored against the authoring layer; the
    authored string is only a fallback for paths that were already absolute.
    Anything that is not a readable file is dropped, so the viewer never gets
    handed a path it will fail to load.

    A UDIM path names one file per tile and none by its own name, so USD leaves
    it unresolved. It is anchored to the layer that authored it and kept, `<UDIM>`
    and all, while a tile of it exists.
    """
    asset_path = shader_input.Get()
    if not isinstance(asset_path, Sdf.AssetPath):
        return None
    if UsdShade.UdimUtils.IsUdimIdentifier(asset_path.path):
        stack = shader_input.GetAttr().GetPropertyStack()
        layer = next((spec.layer for spec in stack if spec.HasDefaultValue()), None)
        return (layer and UsdShade.UdimUtils.ResolveUdimPath(asset_path.path, layer)) or None
    for candidate in (asset_path.resolvedPath, asset_path.path):
        if candidate and os.path.isfile(candidate):
            return os.path.abspath(candidate)
    return None


def _display_color(prim: Any) -> Any:
    if prim is None or not prim.IsValid():
        return None
    colors = UsdGeom.Gprim(prim).GetDisplayColorAttr().Get()
    return colors[0] if colors else None


def _apply_display_color(material: Material, prim: Any) -> None:
    """`displayColor` is the last resort for a prim that binds no material at all."""
    if (color := _display_color(prim)) is not None:
        material.base_color = _rgb(color)


def _double_sided(prim: Any) -> bool:
    if prim is None or not prim.IsValid():
        return False
    return bool(UsdGeom.Gprim(prim).GetDoubleSidedAttr().Get())


def _unit(value: float) -> float:
    return min(max(value, 0.0), 1.0)


def _scalar(value: Any) -> float | None:
    """Coerce a scalar-ish USD value to a float; some dialects author a colour here."""
    if value is None:
        return None
    try:
        return float(value)
    except TypeError:
        return float(_rgb(value)[0])


def _rgb(value: Any) -> tuple[float, float, float]:
    """Coerce a colour-ish USD value — Vec3f, a float, or a one-element array — to RGB."""
    try:
        channels = [float(c) for c in value]
    except TypeError:
        return (float(value),) * 3
    if len(channels) == 1:
        return (channels[0],) * 3
    return (channels[0], channels[1], channels[2])

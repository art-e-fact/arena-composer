"""The material bound to a prim, reduced to what a PBR viewer can use.

USD barely constrains how a surface is described, and these assets arrive in
three dialects that spell the same quantity differently: ``UsdPreviewSurface``,
Omniverse's OmniPBR/OmniSurface MDL shaders, and the occasional bare
``displayColor``. Rather than branch per dialect, every field below is a tuple
of input names tried in order — the first that resolves wins.

Texture paths come from ``Sdf.AssetPath.resolvedPath``, never ``.path``: a USD
authors them relative to the layer that wrote them, so only the resolved value
is anchored. Paths that do not exist are dropped here rather than passed on,
which is what keeps the viewer's asset server quiet.

What is deliberately not handled: MDL materials whose look lives in the ``.mdl``
file rather than in USD inputs. Reading those needs an MDL parser; they come out
with whatever constants the shader happens to author, usually flat grey.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Iterator

from pxr import Sdf, UsdGeom, UsdShade

# Input names to try, most specific dialect first. `diffuseColor` appears in
# both the colour and the texture list because UsdPreviewSurface uses one input
# for both — a constant when unconnected, a texture node when connected.
_BASE_COLOR = (
    "diffuseColor", "diffuse_color_constant", "base_color_constant", "diffuse_tint", "ColorAlbedo"
)
_OPACITY = ("opacity", "opacity_constant")
_METALLIC = ("metallic", "metallic_constant")
_ROUGHNESS = ("roughness", "reflection_roughness_constant")
_EMISSIVE = ("emissiveColor", "emissive_color")

_BASE_COLOR_TEX = (
    "diffuseColor", "diffuse_texture", "base_color_texture", "diffuse_color_texture",
    "AlbedoTexture", "BaseColor",
)
_NORMAL_TEX = ("normal", "normalmap_texture", "normal_map_texture", "MainNormalInput", "Normal")
# Maps packing occlusion, roughness and metallic into one image, in glTF's
# channel order: R occlusion, G roughness, B metallic. Bevy reads both
# `metallic_roughness` and `occlusion` that way, so one file feeds both slots.
# The names are self-describing in the assets that use them —
# `AO_R_Rough_G_Metallic_B_` resolves to `*_ao_r_m.png`, `MergeMapInput` to `*_ORM.png`.
_ORM_TEX = ("ORM_texture", "MergeMapInput", "AO_R_Rough_G_Metallic_B_")
# `metallic` belongs here but not in the occlusion list: UsdPreviewSurface
# connects it to a metallic-roughness pair whose red channel means nothing.
# OmniPBR's separate single-channel `metallic_texture` and
# `reflectionroughness_texture` cannot go here at all — handing over either makes
# the renderer read a roughness out of a metallic map's green channel. Their
# scalar `metallic`/`roughness` constants are read instead, which is an
# approximation but not a wrong one. Packing the pair is a job for a texture cache.
_METALLIC_ROUGHNESS_TEX = ("metallic",) + _ORM_TEX
_EMISSIVE_TEX = ("emissiveColor", "emissive_mask_texture", "Emission")
_OCCLUSION_TEX = ("occlusion", "ao_texture") + _ORM_TEX

# What a texture-reading shader calls the input holding its file path.
_FILE_INPUTS = ("file", "texture", "diffuse_texture", "normalmap_texture")


@dataclass(slots=True)
class Material:
    """A `StandardMaterial` in all but name. Defaults match Bevy's own."""

    base_color: tuple[float, float, float] = (0.8, 0.8, 0.8)
    opacity: float = 1.0
    metallic: float = 0.0
    roughness: float = 0.5
    emissive: tuple[float, float, float] = (0.0, 0.0, 0.0)
    double_sided: bool = False
    textures: dict[str, str] = field(default_factory=dict)


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

    if (color := _constant(shader, _BASE_COLOR)) is not None:
        material.base_color = _rgb(color)
    if (opacity := _scalar(_constant(shader, _OPACITY))) is not None:
        material.opacity = opacity
    if (metallic := _scalar(_constant(shader, _METALLIC))) is not None:
        material.metallic = metallic
    if (roughness := _scalar(_constant(shader, _ROUGHNESS))) is not None:
        material.roughness = roughness
    if (emissive := _constant(shader, _EMISSIVE)) is not None:
        material.emissive = _rgb(emissive)

    for slot, names in (
        ("base_color", _BASE_COLOR_TEX),
        ("normal", _NORMAL_TEX),
        ("metallic_roughness", _METALLIC_ROUGHNESS_TEX),
        ("emissive", _EMISSIVE_TEX),
        ("occlusion", _OCCLUSION_TEX),
    ):
        if (path := _texture(shader, names)) is not None:
            material.textures[slot] = path
    return material


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
    for shader_input in _inputs(shader, names):
        # MDL puts the asset path straight on the shader input.
        if isinstance(value := shader_input.Get(), Sdf.AssetPath) and (path := _resolve(value)):
            return path
        source = shader_input.GetConnectedSource()
        if source and (path := _file_of(source[0])):
            return path
    return None


def _file_of(source: UsdShade.ConnectableAPI) -> str | None:
    """The resolved path a texture-reading shader points at."""
    shader = UsdShade.Shader(source.GetPrim())
    for shader_input in _inputs(shader, _FILE_INPUTS):
        if isinstance(value := shader_input.Get(), Sdf.AssetPath) and (path := _resolve(value)):
            return path
    return None


def _resolve(asset_path: Sdf.AssetPath) -> str | None:
    """An existing local file for an asset path, or None.

    ``resolvedPath`` is the value USD anchored against the authoring layer; the
    authored string is only a fallback for paths that were already absolute.
    Anything that is not a readable file is dropped, so the viewer never gets
    handed a path it will fail to load.
    """
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

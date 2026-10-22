"""Material parsing, against minimal USDAs with hand-checked expected output.

The pattern to copy: author the smallest stage that exercises one rule, write it
next to whatever files it references so OpenUSD's path resolution is exercised
too, and assert the exact values that come out. USD's shading side has no single
spelling for anything, so the parser is a pile of aliases and fallbacks that only
stays honest if each branch has a fixture pinning it.

TODO: cover the rest of the parser in this style.
  * OmniPBR's `albedo_add` and `albedo_desaturation`, once something reads them.
  * `displayColor` as the fallback for a prim that binds no material.
  * `GeomSubset` bindings: per-subset materials, and subset faces inheriting the
    mesh's own binding when they bind nothing.
  * A texture path that does not resolve, which must be dropped rather than sent.
  * The remaining texture slots — emissive, and opacity, which has no Bevy slot.
  * The shape gprims in `usd.shapes`, and `upAxis`/`metersPerUnit` handling and
    default-prim scale unbaking in `usd.extract`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arena_composer.usd import extract, open_stage

# A quad bound to a UsdPreviewSurface: one constant the shader authors itself,
# one texture reached through a connection and authored as a *relative* path.
PREVIEW_SURFACE = """#usda 1.0
(
    defaultPrim = "Asset"
    upAxis = "Z"
    metersPerUnit = 1
)

def Xform "Asset"
{
    def Mesh "Quad" (
        prepend apiSchemas = ["MaterialBindingAPI"]
    )
    {
        point3f[] points = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)]
        int[] faceVertexCounts = [4]
        int[] faceVertexIndices = [0, 1, 2, 3]
        texCoord2f[] primvars:st = [(0, 0), (1, 0), (1, 1), (0, 1)] (
            interpolation = "vertex"
        )
        rel material:binding = </Asset/Looks/Paint>
    }

    def Scope "Looks"
    {
        def Material "Paint"
        {
            token outputs:surface.connect = </Asset/Looks/Paint/Surface.outputs:surface>

            def Shader "Surface"
            {
                uniform token info:id = "UsdPreviewSurface"
                color3f inputs:diffuseColor.connect = </Asset/Looks/Paint/Albedo.outputs:rgb>
                float inputs:metallic = 0.75
                float inputs:roughness = 0.25
                token outputs:surface
            }

            def Shader "Albedo"
            {
                uniform token info:id = "UsdUVTexture"
                asset inputs:file = @./paint.png@
                color3f outputs:rgb
            }
        }
    }
}
"""


@pytest.fixture
def stage_dir(tmp_path: Path) -> Path:
    """A directory holding the USDA and the texture it points at, side by side."""
    (tmp_path / "asset.usda").write_text(PREVIEW_SURFACE, encoding="utf-8")
    # Content does not matter; only that the path resolves to a real file.
    (tmp_path / "paint.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    return tmp_path


def test_a_preview_surface_texture_resolves_against_its_layer(stage_dir: Path) -> None:
    """A relative texture path becomes the absolute one the layer anchors it to.

    This is the whole reason materials are read here rather than in the viewer:
    the authored string (``./paint.png``) means nothing to a renderer's asset
    server, and only OpenUSD knows which layer to resolve it against.
    """
    nodes = extract(open_stage(str(stage_dir / "asset.usda")))

    assert len(nodes) == 1, "one quad, one bound material, so one drawable piece"
    material = nodes[0].material
    assert material.textures["base_color"] == str(stage_dir / "paint.png")
    assert material.metallic == pytest.approx(0.75)
    assert material.roughness == pytest.approx(0.25)


def test_the_quad_is_two_triangles_with_its_uvs(stage_dir: Path) -> None:
    """Sanity check on the geometry the same fixture produces."""
    surface = extract(open_stage(str(stage_dir / "asset.usda")))[0].surface

    assert surface.triangles.shape == (2, 3)
    assert surface.positions.shape == (4, 3), "one position per face corner"
    assert surface.uvs is not None and surface.uvs.shape == (4, 2)


def test_v_is_flipped_into_the_renderers_convention(stage_dir: Path) -> None:
    """USD's `st` origin is bottom-left, Bevy's (and glTF's) is top-left.

    The fixture authors `st` at the corners of the unit square, so the expected
    output is exact. Without the flip a photogrammetry atlas samples the wrong
    band and the asset renders as black patches and radial smears.
    """
    surface = extract(open_stage(str(stage_dir / "asset.usda")))[0].surface

    # Authored (0,0) (1,0) (1,1) (0,1) at vertex interpolation, so per corner.
    assert surface.uvs.tolist() == [[0.0, 1.0], [1.0, 1.0], [1.0, 0.0], [0.0, 0.0]]


# A bespoke MDL material, the dialect the galileo backgrounds are authored in.
# Nothing here is a UsdPreviewSurface name: the surface output lives in the `mdl`
# render context, the texture paths sit directly on the shader inputs rather than
# behind a `UsdUVTexture`, and the inputs are PascalCase. The one packed map is
# named after its own channel layout.
MDL_SURFACE = """#usda 1.0
(
    defaultPrim = "Asset"
    upAxis = "Z"
    metersPerUnit = 1
)

def Xform "Asset"
{
    def Mesh "Quad" (
        prepend apiSchemas = ["MaterialBindingAPI"]
    )
    {
        point3f[] points = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)]
        int[] faceVertexCounts = [4]
        int[] faceVertexIndices = [0, 1, 2, 3]
        rel material:binding = </Asset/Looks/Props>
    }

    def Scope "Looks"
    {
        def Material "Props"
        {
            token outputs:mdl:surface.connect = </Asset/Looks/Props/Shader.outputs:out>

            def Shader "Shader"
            {
                uniform token info:implementationSource = "sourceAsset"
                uniform asset info:mdl:sourceAsset = @./MI_Props.mdl@
                uniform token info:mdl:sourceAsset:subIdentifier = "MI_Props"
                color3f inputs:ColorAlbedo = (0.3, 0.05, 0.01)
                asset inputs:BaseColor = @./T_Props_bc.png@
                asset inputs:Normal = @./T_Props_n.png@
                asset inputs:AO_R_Rough_G_Metallic_B_ = @./T_Props_ao_r_m.png@
                token outputs:out
            }
        }
    }
}
"""


@pytest.fixture
def mdl_dir(tmp_path: Path) -> Path:
    """The MDL stage beside the textures it names."""
    (tmp_path / "mdl.usda").write_text(MDL_SURFACE, encoding="utf-8")
    for name in ("T_Props_bc.png", "T_Props_n.png", "T_Props_ao_r_m.png"):
        (tmp_path / name).write_bytes(b"\x89PNG\r\n\x1a\n")
    return tmp_path


def test_an_mdl_surface_output_is_found_and_its_inputs_read(mdl_dir: Path) -> None:
    """`ComputeSurfaceSource` only finds this shader when asked for the `mdl` context.

    The galileo backgrounds put 1.4M triangles behind materials like this one.
    Reading nothing from them left every surface at the default grey.
    """
    material = extract(open_stage(str(mdl_dir / "mdl.usda")))[0].material

    assert material.base_color == pytest.approx((0.3, 0.05, 0.01))
    assert material.textures["base_color"] == str(mdl_dir / "T_Props_bc.png")
    assert material.textures["normal"] == str(mdl_dir / "T_Props_n.png")


def test_a_packed_orm_map_feeds_both_slots_that_read_it(mdl_dir: Path) -> None:
    """One image, two slots: Bevy reads occlusion from red and rough/metal from green/blue.

    The input name spells the layout out — occlusion in R, roughness in G,
    metallic in B — which is glTF's order and so Bevy's for both slots. A map
    that is only metallic-roughness must *not* reach the occlusion slot, since
    its red channel means nothing there.
    """
    material = extract(open_stage(str(mdl_dir / "mdl.usda")))[0].material

    orm = str(mdl_dir / "T_Props_ao_r_m.png")
    assert material.textures["metallic_roughness"] == orm
    assert material.textures["occlusion"] == orm


def test_a_preview_surface_texture_is_the_colour_itself(stage_dir: Path) -> None:
    """`diffuseColor` connected to a texture leaves no constant to multiply it by.

    It came out at the default grey times the texture, so every textured
    UsdPreviewSurface drew 40% darker than RTX draws it.
    """
    material = extract(open_stage(str(stage_dir / "asset.usda")))[0].material

    assert material.base_color == (1.0, 1.0, 1.0)


# A quad bound to OmniPBR, the MDL most of the library is authored in. `{inputs}`
# is where each test authors what it asserts on.
OMNIPBR = """#usda 1.0
(
    defaultPrim = "Asset"
)

def Xform "Asset"
{{
    def Mesh "Quad" (
        prepend apiSchemas = ["MaterialBindingAPI"]
    )
    {{
        point3f[] points = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)]
        int[] faceVertexCounts = [4]
        int[] faceVertexIndices = [0, 1, 2, 3]
        rel material:binding = </Asset/Looks/Omni>
    }}

    def Scope "Looks"
    {{
        def Material "Omni"
        {{
            token outputs:mdl:surface.connect = </Asset/Looks/Omni/Shader.outputs:out>

            def Shader "Shader"
            {{
                uniform token info:implementationSource = "sourceAsset"
                uniform asset info:mdl:sourceAsset = @OmniPBR.mdl@
                uniform token info:mdl:sourceAsset:subIdentifier = "OmniPBR"
                {inputs}
                token outputs:out
            }}
        }}
    }}
}}
"""


def omnipbr(tmp_path: Path, inputs: str):
    """The material of a quad whose OmniPBR authors ``inputs``, beside an `albedo.png`."""
    (tmp_path / "albedo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    usda = tmp_path / "omnipbr.usda"
    usda.write_text(OMNIPBR.format(inputs=inputs), encoding="utf-8")
    return extract(open_stage(str(usda)))[0].material


def test_omnipbr_draws_its_texture_rather_than_its_constant(tmp_path: Path) -> None:
    """`dex_cube` authors a blue `diffuse_color_constant` beside its texture.

    OmniPBR picks one or the other, and only the tint multiplies the texture.
    Multiplied by the blue, its yellow faces drew dark green.
    """
    material = omnipbr(
        tmp_path,
        """color3f inputs:diffuse_color_constant = (0.13, 0.37, 0.8)
                asset inputs:diffuse_texture = @./albedo.png@
                color3f inputs:diffuse_tint = (1, 0.5, 1)""",
    )

    assert material.textures["base_color"] == str(tmp_path / "albedo.png")
    assert material.base_color == pytest.approx((1, 0.5, 1))


def test_an_untextured_omnipbr_starts_from_its_dark_grey(tmp_path: Path) -> None:
    """The milk jug's body authors only a white tint over OmniPBR's 0.2 default.

    Taking the tint for the colour drew it white.
    """
    material = omnipbr(tmp_path, "color3f inputs:diffuse_tint = (1, 1, 1)")

    assert material.base_color == pytest.approx((0.2, 0.2, 0.2))


def test_omnipbr_at_zero_brightness_is_its_offset_alone(tmp_path: Path) -> None:
    """The office table's chrome top scales its texture to nothing and adds 0.11."""
    material = omnipbr(
        tmp_path,
        """asset inputs:diffuse_texture = @./albedo.png@
                float inputs:albedo_brightness = 0
                float inputs:albedo_add = 0.11""",
    )

    assert "base_color" not in material.textures
    assert material.base_color == pytest.approx((0.11, 0.11, 0.11))


@pytest.mark.parametrize("enabled, glow", [(0, (0, 0, 0)), (1, (1, 0.1, 0.1))])
def test_omnipbr_glows_only_with_emission_enabled(tmp_path: Path, enabled: int, glow: tuple) -> None:
    """The office table's frame authors a red `emissive_color` with emission off.

    RTX draws no glow; reading the colour anyway drew the frame salmon.
    """
    material = omnipbr(
        tmp_path,
        f"""color3f inputs:emissive_color = (1, 0.1, 0.1)
                bool inputs:enable_emission = {enabled}""",
    )

    assert material.emissive == pytest.approx(glow)


def test_a_roughness_past_one_is_clamped(tmp_path: Path) -> None:
    """agibot's A2D authors a roughness of 45.25, which drew the robot white."""
    material = omnipbr(tmp_path, "float inputs:reflection_roughness_constant = 45.25")

    assert material.roughness == 1.0

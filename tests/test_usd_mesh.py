"""Mesh parsing, against minimal USDAs with hand-checked expected output.

Same pattern as `test_usd_material.py`: the smallest stage that exercises one
rule, asserting exact values. Every fixture here is a reduction of something a
real asset in the Arena library authors — the comments name which one, because
each of these looks like a mistake in the fixture until you meet it in the wild.

TODO: cover the rest of the mesh reader in this style.
  * `leftHanded` orientation flipping the winding.
  * `uniform` and `constant` interpolation for normals and UVs.
  * `GeomSubset` splitting, including faces no subset claims.
  * Indexed primvars, which `ComputeFlattened` resolves through their indices.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from arena_composer.usd import extract, open_stage

# A single quad. `{extra}` is where each test injects the attribute under test,
# and `{normals}` the mesh's own `normals` attribute, so the fixtures differ by
# exactly the thing being asserted.
QUAD = """#usda 1.0
(
    defaultPrim = "Asset"
    upAxis = "Z"
    metersPerUnit = 1
)

def Xform "Asset"
{{
    def Mesh "Quad"
    {{
        point3f[] points = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)]
        int[] faceVertexCounts = [4]
        int[] faceVertexIndices = [0, 1, 2, 3]
        {normals}
        {extra}
    }}
}}
"""


# The fridge housing's `bottom` panel in Lightwheel's RoboCasa kitchens: a `Cube`
# given its thickness by a scale, and 70 of the 100 author that as zero.
FLAT_CUBE = """#usda 1.0
(
    defaultPrim = "Asset"
    upAxis = "Z"
    metersPerUnit = 1
)

def Xform "Asset"
{
    def Cube "bottom"
    {
        float3 xformOp:scale = (0.415, 0.38, 0)
        uniform token[] xformOpOrder = ["xformOp:scale"]
    }
}
"""


# Isaac's Seattle lab table, `table`: its default prim stands 0.55 m along x and
# is turned a quarter about z. The scale is `grey_bin_robolab`'s root's.
TURNED_ROOT = """#usda 1.0
(
    defaultPrim = "DemoTable"
    upAxis = "Z"
    metersPerUnit = 1
)

def Xform "DemoTable"
{
    double3 xformOp:translate = (0.55, 0, 0)
    float3 xformOp:rotateXYZ = (0, 0, 90)
    float3 xformOp:scale = (0.007, 0.007, 0.007)
    uniform token[] xformOpOrder = ["xformOp:translate", "xformOp:rotateXYZ", "xformOp:scale"]

    def Mesh "TableGeom"
    {
        point3f[] points = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)]
        int[] faceVertexCounts = [4]
        int[] faceVertexIndices = [0, 1, 2, 3]
    }
}
"""


def surface_of(tmp_path: Path, *, normals: str = "", extra: str = ""):
    """Extract the one surface from a QUAD stage filled in with these attributes."""
    usda = tmp_path / "asset.usda"
    usda.write_text(QUAD.format(normals=normals, extra=extra), encoding="utf-8")
    return extract(open_stage(str(usda)))[0].surface


def test_a_normals_primvar_is_read_when_the_attribute_is_absent(tmp_path: Path) -> None:
    """Exporters write `primvars:normals` instead of the `normals` attribute.

    A packing table in the Arena library authors twelve meshes this way. Reading
    only the attribute lost every one of them, and Bevy fell back to computing
    flat normals — a faceted table where a smooth one was authored.
    """
    surface = surface_of(
        tmp_path,
        extra="""normal3f[] primvars:normals = [(0, 0, 1), (0, 0, 1), (0, 1, 0), (0, 1, 0)] (
            interpolation = "faceVarying"
        )""",
    )

    assert surface.normals is not None, "the primvar is the only normals authored"
    assert surface.normals.tolist() == [[0, 0, 1], [0, 0, 1], [0, 1, 0], [0, 1, 0]]


def test_the_normals_primvar_wins_over_the_attribute(tmp_path: Path) -> None:
    """USD gives an authored primvar precedence over the attribute of the same name."""
    surface = surface_of(
        tmp_path,
        normals='normal3f[] normals = [(1, 0, 0), (1, 0, 0), (1, 0, 0), (1, 0, 0)]',
        extra="""normal3f[] primvars:normals = [(0, 0, 1), (0, 0, 1), (0, 0, 1), (0, 0, 1)] (
            interpolation = "faceVarying"
        )""",
    )

    assert surface.normals.tolist() == [[0, 0, 1]] * 4, "primvar wins, not (1, 0, 0)"


def test_a_non_numeric_primvar_falls_back_to_the_attribute(tmp_path: Path) -> None:
    """A kitchen scene authors `primvars:normals` as a `token[]` holding "faceVarying".

    An exporter wrote the interpolation into the value. Trusting the primvar by
    name alone made the whole asset raise on the conversion, losing 656 meshes;
    the type has to be checked so the real attribute is still found.
    """
    surface = surface_of(
        tmp_path,
        normals='normal3f[] normals = [(1, 0, 0), (1, 0, 0), (1, 0, 0), (1, 0, 0)]',
        extra='token[] primvars:normals = ["faceVarying"]',
    )

    assert surface.normals.tolist() == [[1, 0, 0]] * 4, "the token[] primvar is ignored"


# Two triangles folded 90 degrees along the edge from vertex 0 to vertex 1, authoring no
# normals, as Fourier's GR1T2 does for every mesh. USD's default subdivision scheme is
# `catmullClark`, so the fold is a smooth surface unless the scheme says `none`.
FOLD = """#usda 1.0
(
    defaultPrim = "Asset"
    upAxis = "Z"
    metersPerUnit = 1
)

def Xform "Asset"
{{
    def Mesh "Fold"
    {{
        point3f[] points = [(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1)]
        int[] faceVertexCounts = [3, 3]
        int[] faceVertexIndices = [0, 1, 2, 0, 3, 1]
        uniform token subdivisionScheme = "{scheme}"
    }}
}}
"""


def test_a_subdivision_surface_with_no_normals_is_shaded_smooth(tmp_path: Path) -> None:
    """GR1T2's meshes author no normals; left flat, the humanoid renders in light and dark patches.

    Hydra draws a `catmullClark` mesh smooth, so the corners a face shares with its
    neighbour take the average of their normals, and a `none` scheme is left to the
    renderer's flat ones.
    """
    def surface_with(scheme: str):
        usda = tmp_path / f"{scheme}.usda"
        usda.write_text(FOLD.format(scheme=scheme), encoding="utf-8")
        return extract(open_stage(str(usda)))[0].surface

    edge = np.sqrt(0.5)
    # Corners are 0, 1, 2 for the first face and 0, 3, 1 for the second.
    assert surface_with("catmullClark").normals == pytest.approx(
        np.array([[0, edge, edge], [0, edge, edge], [0, 0, 1], [0, edge, edge], [0, 1, 0], [0, edge, edge]]),
        abs=1e-6,
    )
    assert surface_with("none").normals is None, "a hard-edged mesh keeps the renderer's flat normals"


def test_non_finite_uvs_are_neutralised(tmp_path: Path) -> None:
    """Authored NaNs must not reach the renderer.

    The same packing table carries sixteen NaN UVs. They spread through tangent
    generation and texture sampling, so a whole crate renders corrupt rather
    than just the corners that were bad.
    """
    surface = surface_of(
        tmp_path,
        extra="""texCoord2f[] primvars:st = [(0, 0), (nan, nan), (1, 1), (0, 1)] (
            interpolation = "vertex"
        )""",
    )

    assert np.isfinite(surface.uvs).all()
    # The good corners still flip V normally; only the bad one is zeroed.
    assert surface.uvs.tolist() == [[0.0, 1.0], [0.0, 1.0], [1.0, 0.0], [0.0, 0.0]]


def test_the_default_prim_keeps_its_rotation_and_drops_its_translate_and_scale(tmp_path: Path) -> None:
    """The frame Arena bounds an asset in: from the default prim's origin, turned
    as it is turned, and at the spawn scale, which the viewer applies.

    Keeping the translate drew the table 0.55 m off the box the layout was
    solved by, so the props solved onto it stood beside it.
    """
    usda = tmp_path / "asset.usda"
    usda.write_text(TURNED_ROOT, encoding="utf-8")
    positions = extract(open_stage(str(usda)))[0].surface.positions

    # A quarter turn about z takes x to y and y to -x.
    assert positions.min(axis=0).round(6).tolist() == [-1, 0, 0]
    assert positions.max(axis=0).round(6).tolist() == [0, 1, 0]


def test_up_axis_and_units_are_drawn_as_authored(tmp_path: Path) -> None:
    """The factory peg and hole author `upAxis = "Y"` over geometry modelled Z-up.

    Isaac Lab references a USD without converting it, and Arena bounds it as
    authored. Turning the stage upright for its metadata laid the peg on its side.
    """
    usda = tmp_path / "asset.usda"
    stage = QUAD.format(normals="", extra="").replace('upAxis = "Z"', 'upAxis = "Y"')
    usda.write_text(stage.replace("metersPerUnit = 1", "metersPerUnit = 0.01"), encoding="utf-8")
    positions = extract(open_stage(str(usda)))[0].surface.positions

    assert positions.max(axis=0).tolist() == [1, 1, 0]


def test_a_cube_scaled_flat_keeps_the_normals_of_its_two_faces(tmp_path: Path) -> None:
    """A zero scale leaves no inverse to transform the normals by.

    Inverting it raised, and took the whole kitchen with it. The panel is still
    two faces, one down and one up; the four sides collapse to lines and carry
    no normal, since nothing of them is drawn.
    """
    usda = tmp_path / "asset.usda"
    usda.write_text(FLAT_CUBE, encoding="utf-8")
    surface = extract(open_stage(str(usda)))[0].surface

    assert surface.normals.tolist() == [[0, 0, -1]] * 4 + [[0, 0, 1]] * 4 + [[0, 0, 0]] * 16

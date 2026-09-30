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

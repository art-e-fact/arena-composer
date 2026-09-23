# USD extraction

Reads asset geometry and materials into the payload's frame — Z-up, metres, transforms baked into the
vertex positions. See [the payload contract](../../../AGENTS.md#the-payload).

`extract.py` draws only the stage's **default prim** subtree, which is what Isaac Lab composes in when
it spawns an asset; Kit-authored assets park viewport cameras, render settings and outsized collision
ground planes outside it. It also unbakes the default prim's own `xformOp:scale` (Isaac Lab's spawner
ignores it, and the resolver's bounding boxes are computed the same way), treats an **unauthored**
`upAxis`/`metersPerUnit` as Z-up metres rather than USD's Y-up centimetres, and descends into instance
proxies.

`material.py` resolves texture paths through `Sdf.AssetPath.resolvedPath`, never the authored string,
and drops paths that do not exist — otherwise the viewer's asset server logs a miss per texture. Every
field is a list of input names tried in order, because these assets mix `UsdPreviewSurface`,
OmniPBR/OmniSurface MDL, per-material bespoke MDL modules (PascalCase names such as `AlbedoTexture` and
`MainNormalInput`) and bare `displayColor`.

**Add a name to a slot's list only once an asset proves the mapping** — a texture named for its own
channel layout, or a filename suffix that says what it is. Guessing costs more than it saves: handing a
single-channel OmniPBR `metallic_texture` to Bevy's glTF-packed `metallic_roughness` slot makes it read
roughness out of a metallic map's green channel. Maps that genuinely pack occlusion/roughness/metallic
in glTF's channel order feed the `metallic_roughness` and `occlusion` slots from the same file.

`mesh.py` flattens everything to face corners, so nothing downstream branches on interpolation; flips V
into the renderer's convention (USD's `st` origin is bottom-left, glTF's and Bevy's is top-left); and
splits `GeomSubset` bindings into a piece apiece, since assets that shade per face subset — Isaac's
robots among them — come out untextured if you read only the gprim's own binding. Normals come from
`primvars:normals` before the `normals` attribute, which is both USD's precedence rule and the only way
to read exporters that write nothing else. Primvars are matched by name *and* type, and authored values
are checked for NaN: assets in this library do author a `token[]` where a float array belongs, and NaN
UVs that spread through tangent generation.

`shapes.py` tessellates the analytic gprims. Not optional — assets in use are modelled from `Cube` and
`Cylinder` prims.

## Known gaps

What currently doesn't draw. Check here before treating a render bug as new.

- MDL materials whose look lives in the `.mdl` file rather than in USD inputs — a material authoring
  only `project_uvw` and `world_or_object` has nothing to read.
- Prims that bind no material at all (~13% of the galileo backgrounds).
- Lights authored inside an asset.
- Deformable objects (`deformable_cube`, `deformable_surface`, `deformable_teddy_bear`). They spawn
  from a spawner config rather than a `usd_path`, and `payload.build_payload` draws their bounding box
  in flat blue instead. The teddy bear's spawner does load a USD, but that USD is authored in
  centimetres (`metersPerUnit` 0.01) with a spawn `scale` of 0.01. Arena's bounding box takes the
  scale as the only unit conversion. `_convention` applies `metersPerUnit` on top of it, so the
  extracted mesh would be drawn at 1/100 size. Any asset authored in non-metre units is exposed to this.
- Objects spawned from a shape config instead of a USD (`procedural_cube`, `procedural_table`,
  `sphere`). `resolve_layout.bound_spawned_shapes` bounds them by the shape and the payload draws that
  box, so `sphere` is a cube. Lights and `ground_plane` spawn no shape, and the resolver refuses them.
- The separate single-channel OmniPBR `metallic_texture`/`reflectionroughness_texture` pair, read as its
  scalar constants instead. Packing the two into one image needs a texture cache.

Sweeping the local asset cache is the cheap way to find more: extraction needs only `usd-core`, so a few
hundred assets run through `extract()` in a couple of minutes with no Kit bootstrap, and the failures
sort into "renders nothing", "renders grey" and "crashes". A `payloads/*.usda` fragment that yields no
nodes is not a failure — those are materials-only or physics-only sublayers that mean nothing opened
standalone.

## Tests

`pytest arena_composer/tests` covers this directory against minimal USDAs written inline:
`test_usd_material.py` for the shading dialects, `test_usd_mesh.py` for the geometry reader. Every
fixture is a reduction of something a real asset authors — name that asset in a comment when you add
one, or the fixture reads as a mistake. Each file has a TODO at the top listing what is still uncovered.

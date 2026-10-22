# USD extraction

Reads asset geometry and materials into the payload's frame — Z-up, metres, transforms baked into the
vertex positions. See [the payload contract](../../../docs/architecture.md#the-payload).

`extract.py` draws only the stage's **default prim** subtree, which is what Isaac Lab composes in when
it spawns an asset; Kit-authored assets park viewport cameras, render settings and outsized collision
ground planes outside it. It draws in the frame Arena bounds an asset in: from the default prim's
origin, keeping that prim's own rotation and unbaking its translate and its `xformOp:scale` (the spawn
scale replaces it, and the viewer applies that). Isaac Lab's spawner replaces the root's rotation too,
with the asset's initial pose, so where a root authors one, Arena's box and this drawing are turned
against the sim: in the cache, `table` (a quarter turn about z, and the 0.55 m translate that made it
draw off its box), `kitchen` and `kitchen_with_open_drawer` (15°), and
`computer_mouse_hot3d_robolab` (a quarter turn about x). That is Arena's to fix. It reads every stage
as Z-up metres, ignoring `upAxis` and `metersPerUnit` whether authored or not, because Isaac Lab
references a USD unconverted and Arena bounds it as authored: the factory `peg` and `hole` author
Y-up over Z-up geometry, and Arena's composed robots author neither. It descends into instance
proxies.

A robot is drawn at its **start pose**, not the pose its file authors. `payload.py` hands `extract` the
`joint_pos` the resolver reads off the robot's `init_state`, and each link moves as Arena's own posing
(`isaaclab_arena.utils.usd.articulation`) moves it for `EmbodimentBase.get_bounding_box`, the box the
robot is placed by. That posing sets each link from its parent through the joint's two frames rather
than where the file puts it, turns a revolute joint and slides a prismatic one by its value, holds a
fixed, spherical or D6 joint at zero, and full-matches each key as an Isaac Lab regex
(`iiwa7_joint_(1|2|7)`). It is the one import of Arena here; reading an asset unposed takes `usd-core`
alone. Arena's box is looser than the drawing: it bounds each gprim by its local box's corners, which
stand off a link turned off its axes — 5 cm at the Franka's `panda_link4`. To check a robot against
its box, box the drawn prims the same way: for kuka_allegro, franka_joint_pos, droid_abs_joint_pos,
g1_wbc_joint and gr1_joint that agrees to under a millimetre.

`material.py` resolves texture paths through `Sdf.AssetPath.resolvedPath`, never the authored string,
and drops paths that do not exist — otherwise the viewer's asset server logs a miss per texture. A
UDIM path (`<UDIM>`, an image per tile of UV space) is the exception: USD resolves it to nothing, so
it is anchored with `UsdShade.UdimUtils` and kept while a tile exists, and `extract` splits the mesh
into a piece per tile, each with its own tile's images and its UVs moved into the unit square. Every
field is a list of input names tried in order, because these assets mix `UsdPreviewSurface`,
OmniPBR/OmniSurface MDL, per-material bespoke MDL modules (PascalCase names such as `AlbedoTexture` and
`MainNormalInput`) and bare `displayColor`. Colours are linear, as all of them author them. A colour
map is sRGB or linear as its input, or its reader's `sourceColorSpace`, says; where it says `auto` or
nothing, MDL and USD both go by the file, 8 bits a channel being sRGB and more linear (`_srgb`), and
the payload lists the colour slots that are linear. Bevy decodes every 16-bit PNG to one format
whatever it is told, so `spawn.rs` narrows those to 8 bits in the colour space the material asked for.

OmniPBR, and SimPBR and the vendor modules built on it, are the one dialect that does not multiply its
constant colour into its texture: it draws the texture when it has one, scaled by `albedo_brightness`,
or `diffuse_color_constant` when it has not, 0.2 unless authored, and multiplies either by
`diffuse_tint` (`_albedo`). Its `emissive_color` counts only under `enable_emission`, off unless
authored. Its `ORM_texture` counts only under `enable_ORM_texture` (off unless authored, but for
SimPBR), blending each constant toward the map's channel by `metallic_texture_influence` and
`reflection_roughness_texture_influence`; Bevy multiplies instead, which is exact where an influence
is 1, as Lightwheel authors both (`_orm`). With the ORM map off, a separate `metallic_texture` and
`reflectionroughness_texture` take its place, packed into one image under the asset cache
(`_pack`), because Bevy reads both from one. A map it is told to read and not given reads as 0, as
MDL reads a missing texture: robolab's fruit run SimPBR with no ORM map, and RTX draws them glossy.
Its occlusion reaches the colour only through `ao_to_diffuse`, which almost nothing authors, so it is
not drawn. All of this is what RTX does with
`OmniPBR.mdl`; check a new rule against an Isaac Sim render of the asset, not against what looks
right. A vendor module wrapping OmniPBR sets its own defaults in its `.mdl`, which nothing here
reads: the inputs the USD authors are all there is.

UsdPreviewSurface's `metallic` and `roughness`, connected, take a channel of their map in place of
the constant (`_preview_maps`). SimReady's props connect both to one map in Bevy's own layout, blue
and green, which goes as it is with both factors 1; any other layout is packed into that one.

Lightwheel's restyled kitchens write a colour map into the ORM and normal-map inputs as well. The
ORM one RTX reads as it is, so the composer does too. The normal map is dropped where it is the
colour map's own file: read as normals, it tipped the cabinets' shading grey.

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
to read exporters that write nothing else. A mesh that authors none and keeps USD's default
`catmullClark` scheme gets smooth, area-weighted vertex normals computed here, as Hydra shades it;
left to the renderer, the corner-flattened mesh is shaded one flat normal per triangle, which is what
made Fourier's GR1T2 render in blocky light and dark patches. Primvars are matched by name *and*
type, and authored values
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
- Mirroring transforms (a negative determinant). `_place` does not reverse their winding, so a mesh
  under one is culled from outside unless its material is double-sided. No cached asset authors one.
- Deformable objects (`deformable_cube`, `deformable_surface`, `deformable_teddy_bear`). They spawn
  from a spawner config rather than a `usd_path`, and `payload.build_payload` draws their bounding box
  in flat blue instead.
- Objects spawned from a shape config instead of a USD (`procedural_cube`, `procedural_table`,
  `sphere`). Arena bounds them by the shape and the payload draws that box, so `sphere` is a cube. Lights and `ground_plane` spawn no shape: they keep their configured pose
  and an empty box, the payload draws nothing for them, and a relation naming one is refused.
- OmniPBR's `albedo_add` and `albedo_desaturation`, which offset and grey a texture where a material
  can only multiply it. The galileo backgrounds grade their textures with them. Writing the graded
  image under the asset cache, as `_pack` does, would draw them.

Sweeping the local asset cache is the cheap way to find more: extraction needs only `usd-core`, so a few
hundred assets run through `extract()` in a couple of minutes with no Kit bootstrap, and the failures
sort into "renders nothing", "renders grey" and "crashes". A `payloads/*.usda` fragment that yields no
nodes is not a failure — those are materials-only or physics-only sublayers that mean nothing opened
standalone.

## Tests

`pytest` covers this directory against minimal USDAs written inline:
`test_usd_material.py` for the shading dialects, `test_usd_mesh.py` for the geometry reader, and
`test_payload.py` for posing, against Arena's own box (it needs Arena, so CI sets it aside). Every
fixture is a reduction of something a real asset authors — name that asset in a comment when you add
one, or the fixture reads as a mistake. Each file has a TODO at the top listing what is still uncovered.

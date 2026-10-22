//! Turning the payload's geometry into entities under an asset's placement.
//!
//! There is no USD here. Every convention that needed OpenUSD to resolve —
//! up axis, unit scale, xform-op order, instancing, `GeomSubset` bindings,
//! texture path anchoring — was resolved by `arena_composer.usd`, which bakes
//! the result into vertex positions in the asset's own frame. What arrives is a
//! flat list of triangle meshes, so this is a mesh upload and a material build.

use bevy::asset::RenderAssetUsages;
use bevy::image::ImageLoaderSettings;
use bevy::mesh::{Indices, PrimitiveTopology};
use bevy::prelude::*;
use bevy::render::render_resource::TextureFormat;

use crate::scene::Node;

/// The prim a node was projected from, kept so a picked entity can be named.
#[derive(Component)]
pub struct PrimPath(pub String);

/// Keeps 16-bit greyscale textures drawable: see [`widen_integer_textures`].
pub fn plugin(app: &mut App) {
    app.add_systems(
        Update,
        widen_integer_textures.run_if(resource_changed::<Assets<Image>>),
    );
}

/// Bevy decodes a 16-bit greyscale PNG (Lightwheel's `marble_5_0.png`) as `R16Uint`, or `Rg16Uint` with
/// alpha, and `StandardMaterial` cannot sample an integer texture: the material fails to build and the
/// frame logs an error. `Rgba16Unorm` would keep the depth, but needs a wgpu feature Bevy does not ask
/// for, so this repacks them as 8-bit RGBA. It runs in `Update`, after the loader has inserted the image
/// and before the render world extracts it, so the first frame that draws it already sees a float one.
///
/// Read as sRGB, which is right for the only slot that has met one, `base_color`: a greyscale colour map.
// ponytail: a 16-bit greyscale in a data slot (normal, occlusion) would be gamma-decoded; give it its own
// format when an asset has one.
fn widen_integer_textures(mut images: ResMut<Assets<Image>>) {
    let integer: Vec<_> = images
        .iter()
        .filter(|(_, image)| {
            matches!(
                image.texture_descriptor.format,
                TextureFormat::R16Uint | TextureFormat::Rg16Uint
            )
        })
        .map(|(id, _)| id)
        .collect();
    for id in integer {
        if let Some(mut image) = images.get_mut(id) {
            widen(&mut image);
        }
    }
}

fn widen(image: &mut Image) {
    let channels = if image.texture_descriptor.format == TextureFormat::R16Uint {
        1
    } else {
        2
    };
    let Some(data) = image.data.take() else {
        return;
    };
    // Little-endian u16s, so the high byte is the second of each pair.
    let rgba = data
        .chunks_exact(2 * channels)
        .flat_map(|texel| {
            let (grey, alpha) = (texel[1], if channels == 2 { texel[3] } else { 255 });
            [grey, grey, grey, alpha]
        })
        .collect();
    image.data = Some(rgba);
    image.texture_descriptor.format = TextureFormat::Rgba8UnormSrgb;
}

/// Spawn one entity per node, parented under `placement`, and return them by
/// node: `None` for a node with nothing to draw.
///
/// `placement` keeps its own `Transform` — the solved pose, in Arena's frame —
/// and the nodes need none of their own.
pub fn spawn_geometry(world: &mut World, nodes: &[Node], placement: Entity) -> Vec<Option<Entity>> {
    nodes
        .iter()
        .map(|node| {
            let mesh = build_mesh(node)?;
            let mesh = world.resource_mut::<Assets<Mesh>>().add(mesh);
            let material = build_material(node, world);
            let piece = world.spawn((
                Mesh3d(mesh),
                MeshMaterial3d(material),
                PrimPath(node.path.clone()),
                ChildOf(placement),
            ));
            Some(piece.id())
        })
        .collect()
}

/// Upload one node's triangles, or `None` when it carries nothing drawable.
fn build_mesh(node: &Node) -> Option<Mesh> {
    let positions = node.positions();
    let indices = node.indices();
    if positions.is_empty() || indices.len() < 3 {
        return None;
    }

    let mut mesh = Mesh::new(
        PrimitiveTopology::TriangleList,
        RenderAssetUsages::default(),
    );
    mesh.insert_attribute(Mesh::ATTRIBUTE_POSITION, positions);
    if let Some(uvs) = node.uvs() {
        mesh.insert_attribute(Mesh::ATTRIBUTE_UV_0, uvs);
    }
    mesh.insert_indices(Indices::U32(indices));

    match node.normals() {
        Some(normals) => mesh.insert_attribute(Mesh::ATTRIBUTE_NORMAL, normals),
        // A USD need not author normals; smooth ones are a better guess than
        // the flat black an absent attribute would shade to.
        None => mesh.compute_normals(),
    }
    // A normal map is sampled in tangent space, so Bevy drops the map entirely
    // unless the mesh carries tangents. They need UVs, hence the failure path.
    if node.textures.contains_key("normal") && mesh.generate_tangents().is_err() {
        debug!("{}: no tangents, normal map will not apply", node.path);
    }
    Some(mesh)
}

fn build_material(node: &Node, world: &mut World) -> Handle<StandardMaterial> {
    let server = world.resource::<AssetServer>().clone();
    let [red, green, blue] = node.base_color;
    let [er, eg, eb] = node.emissive;

    let mut material = StandardMaterial {
        // Linear, as USD and MDL author colours: read as sRGB, OmniPBR's 0.2 grey drew as 0.03.
        base_color: Color::linear_rgba(red, green, blue, node.opacity),
        perceptual_roughness: node.roughness,
        metallic: node.metallic,
        emissive: LinearRgba::rgb(er, eg, eb),
        // Bevy back-face culls by default, so a double-sided surface has to
        // turn culling off as well as set the flag.
        double_sided: node.double_sided,
        cull_mode: (!node.double_sided).then_some(bevy::render::render_resource::Face::Back),
        alpha_mode: if node.opacity < 1.0 {
            AlphaMode::Blend
        } else {
            AlphaMode::Opaque
        },
        ..default()
    };

    // Paths are absolute and were checked to exist before they were sent, which
    // is what keeps the asset server from logging a miss per texture.
    //
    // Colour maps are authored in sRGB, but normal, metallic/roughness and
    // occlusion maps hold measurements, not colour. Bevy's image loader assumes
    // sRGB for both, so the data ones have to say otherwise or every value comes
    // back gamma-decoded — which on a normal map tilts the whole surface and
    // reads as smeared, streaky shading.
    let texture = |slot: &str, srgb: bool| {
        node.textures.get(slot).map(|path| {
            server
                .load_builder()
                .with_settings(move |settings: &mut ImageLoaderSettings| settings.is_srgb = srgb)
                .load::<Image>(path.clone())
        })
    };
    material.base_color_texture = texture("base_color", true);
    material.emissive_texture = texture("emissive", true);
    material.normal_map_texture = texture("normal", false);
    material.metallic_roughness_texture = texture("metallic_roughness", false);
    material.occlusion_texture = texture("occlusion", false);

    world
        .resource_mut::<Assets<StandardMaterial>>()
        .add(material)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn triangle() -> Node {
        Node {
            positions: [0.0f32, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
                .iter()
                .flat_map(|f| f.to_le_bytes())
                .collect(),
            indices: [0u32, 1, 2].iter().flat_map(|i| i.to_le_bytes()).collect(),
            base_color: [1.0, 0.0, 0.0],
            opacity: 1.0,
            ..default()
        }
    }

    /// A 16-bit greyscale PNG arrives from Bevy's loader as `R16Uint`, which `StandardMaterial` cannot
    /// sample; it has to come out as a float format with the grey in all three colour channels.
    #[test]
    fn a_16_bit_greyscale_texture_is_widened_to_rgba8() {
        let mut image = Image::new(
            bevy::render::render_resource::Extent3d {
                width: 2,
                height: 1,
                depth_or_array_layers: 1,
            },
            bevy::render::render_resource::TextureDimension::D2,
            [0x00u16, 0xff00].iter().flat_map(|v| v.to_le_bytes()).collect(),
            TextureFormat::R16Uint,
            RenderAssetUsages::default(),
        );
        widen(&mut image);
        assert_eq!(image.texture_descriptor.format, TextureFormat::Rgba8UnormSrgb);
        assert_eq!(image.data.as_deref(), Some(&[0, 0, 0, 255, 0xff, 0xff, 0xff, 255][..]));
    }

    /// A node with positions and indices but no normals still uploads, with
    /// normals computed rather than left absent.
    #[test]
    fn a_node_without_normals_still_builds_a_mesh() {
        let mesh = build_mesh(&triangle()).expect("a drawable mesh");
        assert_eq!(mesh.count_vertices(), 3);
        assert!(mesh.attribute(Mesh::ATTRIBUTE_NORMAL).is_some());
    }

    /// Degenerate nodes are skipped instead of producing an empty draw call.
    #[test]
    fn a_node_without_geometry_is_dropped() {
        assert!(build_mesh(&Node::default()).is_none());
        let mut node = triangle();
        node.indices.clear();
        assert!(build_mesh(&node).is_none());
    }

    /// The whole draw path minus the window: decode a real packed scene, build
    /// every mesh and material, and check the result is renderable — geometry
    /// present, and every texture path pointing at a file that exists.
    ///
    /// Ignored because it needs a payload to read. Make one first:
    /// `arena-resolve-layout <spec>.yaml --payload scene.msgpack`, then
    /// `ARENA_COMPOSER_PAYLOAD=scene.msgpack cargo test -- --ignored`.
    #[test]
    #[ignore = "needs a payload from arena-resolve-layout --payload"]
    fn a_real_payload_builds_renderable_geometry() {
        use crate::scene::Scene;

        let path = std::env::var("ARENA_COMPOSER_PAYLOAD")
            .unwrap_or_else(|_| "scene.msgpack".to_string());
        let bytes = std::fs::read(&path)
            .unwrap_or_else(|e| panic!("{path}: {e} — run arena-resolve-layout --payload first"));
        let scene = Scene::from_msgpack(&bytes).expect("decoding the payload");

        let mut app = App::new();
        app.add_plugins((
            MinimalPlugins,
            bevy::asset::AssetPlugin {
                unapproved_path_mode: bevy::asset::UnapprovedPathMode::Allow,
                ..default()
            },
        ))
        .init_asset::<Mesh>()
        .init_asset::<StandardMaterial>()
        .init_asset::<Image>();

        let mut drawn = 0;
        for asset in scene.assets() {
            let nodes = scene.geometry_of(asset);
            if nodes.is_empty() {
                continue;
            }
            let placement = app.world_mut().spawn(asset.transform()).id();
            spawn_geometry(app.world_mut(), nodes, placement);
            drawn += 1;
        }
        assert!(drawn > 0, "the payload placed nothing");

        let world = app.world_mut();
        let meshes: Vec<_> = world
            .query::<&Mesh3d>()
            .iter(world)
            .map(|mesh| mesh.0.clone())
            .collect();
        let triangles: usize = {
            let assets = world.resource::<Assets<Mesh>>();
            meshes
                .iter()
                .filter_map(|handle| assets.get(handle))
                .filter_map(|mesh| mesh.indices())
                .map(|indices| indices.len() / 3)
                .sum()
        };
        assert!(triangles > 0, "meshes uploaded but carry no triangles");

        // Guards the optional-blob wiring: if `Option<Vec<u8>>` silently
        // decoded as `None`, every mesh would fall back to computed normals
        // and lose its UVs, and nothing else here would notice.
        let nodes: Vec<_> = scene
            .assets()
            .iter()
            .flat_map(|asset| scene.geometry_of(asset))
            .collect();
        assert!(
            nodes.iter().filter(|node| node.normals.is_some()).count() > nodes.len() / 2,
            "most nodes should carry authored normals"
        );
        assert!(
            nodes.iter().any(|node| node.uvs.is_some()),
            "no node carried texture coordinates"
        );

        // Python drops texture paths that do not resolve, so anything that
        // arrived here must be loadable.
        let mut textures = 0;
        for asset in scene.assets() {
            for node in scene.geometry_of(asset) {
                for path in node.textures.values() {
                    assert!(
                        std::path::Path::new(path).is_file(),
                        "payload carries an unresolved texture: {path}"
                    );
                    textures += 1;
                }
            }
        }
        println!(
            "{drawn} assets, {} meshes, {triangles} triangles, {textures} textures",
            meshes.len()
        );
    }

    /// Transparency has to reach the alpha mode, or a translucent surface
    /// draws as an opaque one.
    #[test]
    fn opacity_selects_the_alpha_mode() {
        // Enough of an app to hold assets and hand out a server; no renderer.
        let mut app = App::new();
        app.add_plugins((MinimalPlugins, bevy::asset::AssetPlugin::default()))
            .init_asset::<StandardMaterial>()
            .init_asset::<Image>();

        let mut node = triangle();
        node.opacity = 0.5;
        node.double_sided = true;
        let handle = build_material(&node, app.world_mut());

        let materials = app.world().resource::<Assets<StandardMaterial>>();
        let material = materials.get(&handle).expect("the material was added");
        assert!(matches!(material.alpha_mode, AlphaMode::Blend));
        assert_eq!(material.base_color.alpha(), 0.5);
        assert!(material.double_sided && material.cull_mode.is_none());
    }

    /// The payload's colours are linear; read as sRGB, every constant colour drew darker.
    #[test]
    fn base_color_is_linear() {
        let mut app = App::new();
        app.add_plugins((MinimalPlugins, bevy::asset::AssetPlugin::default()))
            .init_asset::<StandardMaterial>()
            .init_asset::<Image>();

        let mut node = triangle();
        node.base_color = [0.2, 0.2, 0.2];
        let handle = build_material(&node, app.world_mut());

        let materials = app.world().resource::<Assets<StandardMaterial>>();
        let material = materials.get(&handle).expect("the material was added");
        assert_eq!(material.base_color.to_linear().red, 0.2);
    }
}

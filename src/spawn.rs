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
use std::collections::HashSet;

use crate::scene::Node;

/// The prim a node was projected from, kept so a picked entity can be named.
#[derive(Component)]
pub struct PrimPath(pub String);

/// Keeps 16-bit textures drawable, and in the right colour space: see [`narrow_16_bit_textures`].
pub fn plugin(app: &mut App) {
    app.init_resource::<SrgbImages>().add_systems(
        Update,
        narrow_16_bit_textures.run_if(resource_changed::<Assets<Image>>),
    );
}

/// The images a material asked for as sRGB colour. A 16-bit PNG decodes to one format whatever the
/// loader was told, so this is how [`narrow_16_bit_textures`] knows which it was asked for.
#[derive(Resource, Default)]
struct SrgbImages(HashSet<AssetId<Image>>);

/// Bevy decodes a 16-bit PNG to a format of its own, whatever the loader was told. An RGB one becomes
/// `Rgba16Unorm`, which has no sRGB variant, so a colour map drew as if it were linear: washed out, as
/// robolab's fruit and Lightwheel's microwaves and toasters did. A greyscale one (Lightwheel's
/// `marble_5_0.png`) becomes `R16Uint`, or `Rg16Uint` with alpha, which `StandardMaterial` cannot sample
/// at all. This repacks each as 8-bit RGBA, sRGB where a material asked for it so. It runs in `Update`,
/// after the loader has inserted the image and before the render world extracts it, so the first frame
/// that draws it already sees the 8-bit one.
fn narrow_16_bit_textures(mut images: ResMut<Assets<Image>>, srgb: Res<SrgbImages>) {
    let wide: Vec<_> = images
        .iter()
        .filter(|(_, image)| {
            matches!(
                image.texture_descriptor.format,
                TextureFormat::R16Uint | TextureFormat::Rg16Uint | TextureFormat::Rgba16Unorm
            )
        })
        .map(|(id, _)| id)
        .collect();
    for id in wide {
        if let Some(mut image) = images.get_mut(id) {
            narrow(&mut image, srgb.0.contains(&id));
        }
    }
}

fn narrow(image: &mut Image, srgb: bool) {
    let channels = match image.texture_descriptor.format {
        TextureFormat::R16Uint => 1,
        TextureFormat::Rg16Uint => 2,
        _ => 4,
    };
    let Some(data) = image.data.take() else {
        return;
    };
    // Little-endian u16s, so the high byte is the second of each pair.
    let rgba = data
        .chunks_exact(2 * channels)
        .flat_map(|texel| {
            let high = |channel: usize| texel[2 * channel + 1];
            match channels {
                1 => [high(0), high(0), high(0), 255],
                2 => [high(0), high(0), high(0), high(1)],
                _ => [high(0), high(1), high(2), high(3)],
            }
        })
        .collect();
    image.data = Some(rgba);
    image.texture_descriptor.format = if srgb {
        TextureFormat::Rgba8UnormSrgb
    } else {
        TextureFormat::Rgba8Unorm
    };
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
    // Colour maps are mostly authored in sRGB, but normal, metallic/roughness and
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
    let colour = |slot: &str| !node.linear.iter().any(|linear| linear == slot);
    material.base_color_texture = texture("base_color", colour("base_color"));
    material.emissive_texture = texture("emissive", colour("emissive"));
    material.normal_map_texture = texture("normal", false);
    material.metallic_roughness_texture = texture("metallic_roughness", false);
    material.occlusion_texture = texture("occlusion", false);
    let mut srgb = world.get_resource_or_init::<SrgbImages>();
    for (slot, handle) in [("base_color", &material.base_color_texture), ("emissive", &material.emissive_texture)] {
        if let Some(handle) = handle.as_ref().filter(|_| colour(slot)) {
            srgb.0.insert(handle.id());
        }
    }

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

    fn image(texels: &[u16], format: TextureFormat) -> Image {
        let channels = if format == TextureFormat::R16Uint { 1 } else { 4 };
        Image::new(
            bevy::render::render_resource::Extent3d {
                width: (texels.len() / channels) as u32,
                height: 1,
                depth_or_array_layers: 1,
            },
            bevy::render::render_resource::TextureDimension::D2,
            texels.iter().flat_map(|v| v.to_le_bytes()).collect(),
            format,
            RenderAssetUsages::default(),
        )
    }

    /// A 16-bit greyscale PNG arrives from Bevy's loader as `R16Uint`, which `StandardMaterial` cannot
    /// sample; it has to come out as a float format with the grey in all three colour channels.
    #[test]
    fn a_16_bit_greyscale_texture_is_narrowed_to_rgba8() {
        let mut grey = image(&[0x00, 0xff00], TextureFormat::R16Uint);
        narrow(&mut grey, true);
        assert_eq!(grey.texture_descriptor.format, TextureFormat::Rgba8UnormSrgb);
        assert_eq!(grey.data.as_deref(), Some(&[0, 0, 0, 255, 0xff, 0xff, 0xff, 255][..]));
    }

    /// A 16-bit RGB PNG arrives as `Rgba16Unorm`, which has no sRGB variant: as a colour map it has to
    /// come out sRGB, or it draws washed out, and as a data map linear.
    #[test]
    fn a_16_bit_colour_texture_is_narrowed_to_srgb_and_a_data_one_to_linear() {
        let texel = [0x1234, 0x5678, 0x9abc, 0xffff];
        let mut colour = image(&texel, TextureFormat::Rgba16Unorm);
        narrow(&mut colour, true);
        assert_eq!(colour.texture_descriptor.format, TextureFormat::Rgba8UnormSrgb);
        assert_eq!(colour.data.as_deref(), Some(&[0x12, 0x56, 0x9a, 0xff][..]));
        let mut data = image(&texel, TextureFormat::Rgba16Unorm);
        narrow(&mut data, false);
        assert_eq!(data.texture_descriptor.format, TextureFormat::Rgba8Unorm);
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

    /// A colour map the payload calls linear is not narrowed to sRGB when it turns out 16-bit, as
    /// Lightwheel's floor tiles are; one it does not is.
    #[test]
    fn only_a_colour_map_not_called_linear_is_asked_for_as_srgb() {
        let mut app = App::new();
        app.add_plugins((
            MinimalPlugins,
            bevy::asset::AssetPlugin {
                unapproved_path_mode: bevy::asset::UnapprovedPathMode::Allow,
                ..default()
            },
        ))
        .init_asset::<StandardMaterial>()
        .init_asset::<Image>();

        let mut node = triangle();
        node.textures.insert("base_color".into(), "/tiles.png".into());
        node.textures.insert("emissive".into(), "/glow.png".into());
        node.linear = vec!["base_color".into()];
        let handle = build_material(&node, app.world_mut());

        let material = app.world().resource::<Assets<StandardMaterial>>().get(&handle).unwrap().clone();
        let srgb = &app.world().resource::<SrgbImages>().0;
        assert!(!srgb.contains(&material.base_color_texture.unwrap().id()));
        assert!(srgb.contains(&material.emissive_texture.unwrap().id()));
    }
}

//! Lighting up an object in the view: what the pointer is over, in the view or
//! in the graph, and what the right-click menu is about.
//!
//! The object's own surfaces are tinted, and its silhouette is traced, through
//! whatever stands in front: the tint shows what of it is in sight, the
//! silhouette where the rest of it is. The background is traced alone — the
//! pointer is over it whenever it rests on a wall or the floor, and a tint
//! would light the whole room. An object reference draws nothing of its own,
//! so it keeps its box.
//!
//! Gizmos are drawn afresh every frame, but this is state: a piece's material
//! swapped for a tinted copy, and the piece put on the [`MASK`] layer, which a
//! camera of its own draws into an image that an overlay over the window edges
//! into the silhouette. So the callers ask, by id, every frame they want it
//! ([`Highlight::show`]), and [`apply`] lights what was asked and puts back
//! what no longer is.

use std::collections::HashMap;

use bevy::asset::embedded_asset;
use bevy::camera::visibility::{RenderLayers, VisibilitySystems};
use bevy::camera::{CameraUpdateSystems, RenderTarget};
use bevy::core_pipeline::tonemapping::Tonemapping;
use bevy::prelude::*;
use bevy::render::render_resource::{AsBindGroup, Extent3d, TextureFormat};
use bevy::shader::ShaderRef;
use bevy::ui_render::prelude::{MaterialNode, UiMaterial, UiMaterialPlugin};
use bevy::window::PrimaryWindow;
use bevy_egui::EguiPostUpdateSet;
use bevy_panorbit_camera::{PanOrbitCamera, PanOrbitCameraSystemSet};

use super::relations::SUBJECT;
use crate::layout::AssetId;
use crate::scene::Asset;

/// The layer the mask camera draws. Far above [`crate::layout::view_layers`],
/// which the headless renders number from 1.
const MASK: usize = 30;

/// How far the tint pulls a surface's colour towards [`SUBJECT`], and how much
/// of it the surface gives off besides, so a piece in shade lights up too. A
/// `StandardMaterial`'s emission skips the camera's exposure: 1 is white.
const TINT: f32 = 0.4;
const GLOW: f32 = 0.12;

/// The silhouette's width, in the window's logical pixels.
const OUTLINE_PX: f32 = 2.5;

pub fn plugin(app: &mut App) {
    embedded_asset!(app, "highlight.wesl");
    app.add_plugins(UiMaterialPlugin::<Silhouette>::default())
        .init_resource::<Highlight>()
        // After egui's pass, where the callers ask; after the orbit camera has
        // moved, and before Bevy builds the projection and decides what is
        // visible from where it now stands.
        .add_systems(
            PostUpdate,
            apply
                .after(EguiPostUpdateSet::ProcessOutput)
                .after(PanOrbitCameraSystemSet)
                .before(CameraUpdateSystems)
                .before(VisibilitySystems::CheckVisibility),
        );
}

#[derive(Resource, Default)]
pub struct Highlight {
    /// Asked for this frame, by id.
    wanted: HashMap<String, Look>,
    /// The pieces lit now, and what each was before.
    lit: HashMap<Entity, Before>,
    mask: Option<Mask>,
}

struct Before {
    material: Handle<StandardMaterial>,
    layers: Option<RenderLayers>,
    look: Look,
}

/// How a piece is lit: tinted towards a colour, traced, or both.
#[derive(Clone, Copy, PartialEq, Debug)]
struct Look {
    tint: Option<Srgba>,
    trace: bool,
}

/// The camera that draws the lit pieces alone, the image it draws them into,
/// and the overlay that reads it.
struct Mask {
    camera: Entity,
    image: Handle<Image>,
    overlay: Entity,
    material: Handle<Silhouette>,
}

impl Highlight {
    /// Light `asset` this frame. One with no geometry of its own has nothing
    /// to light: [`super::relations::Painter::light`] draws its box instead.
    pub fn show(&mut self, asset: &Asset) {
        let tint = (asset.role != "background").then_some(SUBJECT);
        self.wanted.insert(asset.id.clone(), Look { tint, trace: true });
    }

    /// Tint `asset` towards `color` this frame, untraced — unless it is lit
    /// already, which wins.
    pub fn tint(&mut self, asset: &Asset, color: Srgba) {
        if asset.geometry.is_some() && asset.role != "background" {
            self.wanted.entry(asset.id.clone()).or_insert(Look { tint: Some(color), trace: false });
        }
    }
}

/// The silhouette overlay: the edge of what the mask camera drew, `width`
/// pixels out, in `color`.
#[derive(Asset, TypePath, AsBindGroup, Clone)]
pub struct Silhouette {
    #[uniform(0)]
    color: LinearRgba,
    /// In `x`, in physical pixels. A vector, since WebGL2 takes no uniform
    /// smaller than 16 bytes.
    #[uniform(1)]
    width: Vec4,
    #[texture(2)]
    mask: Handle<Image>,
}

impl UiMaterial for Silhouette {
    fn fragment_shader() -> ShaderRef {
        "embedded://arena_composer/editor/highlight.wesl".into()
    }
}

#[allow(clippy::too_many_arguments, clippy::type_complexity)]
fn apply(
    mut highlight: ResMut<Highlight>,
    placements: Query<(&AssetId, &Children)>,
    mut pieces: Query<(&mut MeshMaterial3d<StandardMaterial>, Option<&RenderLayers>)>,
    mut materials: ResMut<Assets<StandardMaterial>>,
    mut images: ResMut<Assets<Image>>,
    mut silhouettes: ResMut<Assets<Silhouette>>,
    main: Option<Single<(Entity, &Camera, &Projection), With<PanOrbitCamera>>>,
    window: Option<Single<&Window, With<PrimaryWindow>>>,
    mut cameras: Query<(&mut Camera, &mut Projection), Without<PanOrbitCamera>>,
    mut overlays: Query<&mut Visibility, With<MaterialNode<Silhouette>>>,
    mut commands: Commands,
) {
    let highlight = &mut *highlight;
    let wanted: HashMap<Entity, Look> = placements
        .iter()
        .filter_map(|(id, children)| Some((children, *highlight.wanted.get(&id.0)?)))
        .flat_map(|(children, look)| children.iter().map(move |piece| (piece, look)))
        .filter(|(piece, _)| pieces.contains(*piece))
        .collect();
    highlight.wanted.clear();

    // Put back what is no longer asked for, or asked for in another look. A
    // piece despawned with its layout has nothing to put back.
    let gone: Vec<Entity> = highlight
        .lit
        .iter()
        .filter(|(piece, before)| wanted.get(piece) != Some(&before.look))
        .map(|(piece, _)| *piece)
        .collect();
    // What each had before it was lit, for one lit again in another look below:
    // its layers are put back by a command, still to land when they are read.
    let mut put_back = HashMap::new();
    for piece in gone {
        let before = highlight.lit.remove(&piece).expect("a lit piece");
        if let Ok((mut material, _)) = pieces.get_mut(piece) {
            material.0 = before.material;
            match &before.layers {
                Some(layers) => commands.entity(piece).try_insert(layers.clone()),
                None => commands.entity(piece).try_remove::<RenderLayers>(),
            };
        }
        put_back.insert(piece, before.layers);
    }
    for (&piece, &look) in &wanted {
        if highlight.lit.contains_key(&piece) {
            continue;
        }
        let Ok((mut material, layers)) = pieces.get_mut(piece) else { continue };
        let layers = put_back.remove(&piece).unwrap_or_else(|| layers.cloned());
        let before = Before { material: material.0.clone(), layers, look };
        if let Some(color) = look.tint
            && let Some(original) = materials.get(&material.0)
        {
            let mut tinted = original.clone();
            // The tint's alpha is how strongly: a reference is tinted lighter.
            tinted.base_color = tinted.base_color.mix(&color.with_alpha(1.0).into(), TINT * color.alpha);
            tinted.emissive += LinearRgba::from(color.with_alpha(1.0)) * GLOW * color.alpha;
            material.0 = materials.add(tinted);
        }
        if look.trace {
            commands.entity(piece).try_insert(before.layers.clone().unwrap_or_default().with(MASK));
        }
        highlight.lit.insert(piece, before);
    }

    let Some(main) = main else { return };
    let (main, camera, projection) = *main;
    let mask = highlight.mask.get_or_insert_with(|| {
        let image = images.add(Image::new_target_texture(1, 1, TextureFormat::Rgba8UnormSrgb, None));
        let material = silhouettes.add(Silhouette { color: SUBJECT.into(), width: Vec4::ZERO, mask: image.clone() });
        // Under the main camera, so it stands where that one does. Its alpha
        // is the mask: an opaque surface writes 1, the clear colour 0.
        let camera = commands
            .spawn((
                Camera3d::default(),
                Camera { order: -1, clear_color: ClearColorConfig::Custom(Color::NONE), ..default() },
                RenderTarget::Image(image.clone().into()),
                RenderLayers::layer(MASK),
                Msaa::Off,
                Tonemapping::None,
                ChildOf(main),
            ))
            .id();
        let overlay = commands
            .spawn((
                Node {
                    position_type: PositionType::Absolute,
                    width: Val::Percent(100.0),
                    height: Val::Percent(100.0),
                    ..default()
                },
                MaterialNode(material.clone()),
                Visibility::Hidden,
            ))
            .id();
        Mask { camera, image, overlay, material }
    });
    // The mask is the window's size, pixel for pixel, and the picture the main
    // camera draws: its projection, and the part of it the panel leaves.
    if let Some(size) = camera.physical_target_size()
        && images.get(&mask.image).is_some_and(|image| image.size() != size)
    {
        let extent = Extent3d { width: size.x, height: size.y, depth_or_array_layers: 1 };
        images.get_mut(&mask.image).expect("the mask").resize(extent);
        if let Some(mut silhouette) = silhouettes.get_mut(&mask.material) {
            silhouette.width.x = OUTLINE_PX * window.map_or(1.0, |window| window.scale_factor());
        }
    }
    // Neither draws while nothing is traced.
    let on = highlight.lit.values().any(|before| before.look.trace);
    if let Ok((mut mask_camera, mut mask_projection)) = cameras.get_mut(mask.camera) {
        mask_camera.is_active = on;
        mask_camera.sub_camera_view = camera.sub_camera_view;
        *mask_projection = projection.clone();
    }
    if let Ok(mut visibility) = overlays.get_mut(mask.overlay) {
        *visibility = if on { Visibility::Inherited } else { Visibility::Hidden };
    }
}

#[cfg(test)]
mod tests {
    use bevy::ecs::system::RunSystemOnce;

    use super::*;

    /// A lit object's pieces take a tinted copy of their material and the mask
    /// layer, a lit background's the layer alone, and both get back what they
    /// had once no longer asked for.
    #[test]
    fn a_piece_is_lit_while_asked_for_and_put_back_after() {
        let mut world = World::new();
        world.init_resource::<Assets<StandardMaterial>>();
        world.init_resource::<Assets<Image>>();
        world.init_resource::<Assets<Silhouette>>();
        world.init_resource::<Highlight>();
        let original = world.resource_mut::<Assets<StandardMaterial>>().add(StandardMaterial::default());
        let mut piece_of = |id: &str| {
            let placement = world.spawn(AssetId(id.into())).id();
            world.spawn((MeshMaterial3d(original.clone()), ChildOf(placement))).id()
        };
        let (bowl, kitchen) = (piece_of("bowl"), piece_of("kitchen"));
        let lit = |world: &World, piece| {
            let material = &world.get::<MeshMaterial3d<StandardMaterial>>(piece).expect("a piece").0;
            let layers = world.get::<RenderLayers>(piece);
            let masked = layers.is_some_and(|layers| layers.intersects(&RenderLayers::layer(MASK)));
            (*material != original, masked)
        };

        let mut wanted = world.resource_mut::<Highlight>();
        wanted.wanted.insert("bowl".into(), Look { tint: Some(SUBJECT), trace: true });
        wanted.wanted.insert("kitchen".into(), Look { tint: None, trace: true });
        world.run_system_once(apply).expect("apply runs");
        assert_eq!(lit(&world, bowl), (true, true), "the bowl is tinted and traced");
        assert_eq!(lit(&world, kitchen), (false, true), "the background is traced alone");
        let layers = world.get::<RenderLayers>(bowl).expect("layers");
        assert!(layers.intersects(&RenderLayers::default()), "still drawn by the main camera");

        world.run_system_once(apply).expect("apply runs");
        assert_eq!(lit(&world, bowl), (false, false), "the bowl is put back");
        assert_eq!(lit(&world, kitchen), (false, false), "the background is put back");

        // Traced, then tinted alone, as a hovered reference is once the pointer
        // leaves it, and then let go: the trace's layer goes with the first look.
        let mut wanted = world.resource_mut::<Highlight>();
        wanted.wanted.insert("bowl".into(), Look { tint: Some(SUBJECT), trace: true });
        world.run_system_once(apply).expect("apply runs");
        let mut wanted = world.resource_mut::<Highlight>();
        wanted.wanted.insert("bowl".into(), Look { tint: Some(SUBJECT), trace: false });
        world.run_system_once(apply).expect("apply runs");
        assert_eq!(lit(&world, bowl), (true, false), "tinted, no longer traced");
        world.run_system_once(apply).expect("apply runs");
        assert_eq!(lit(&world, bowl), (false, false), "put back, untraced");
    }
}

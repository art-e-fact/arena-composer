//! The viewer: a windowed Bevy app that draws one resolved layout.
//!
//! Bare bones on purpose — it opens each asset's USD, projects it under the
//! pose the solver picked, and gives you an orbit camera to look at it. There
//! is no editing, no re-resolve, and no sim.

use bevy::asset::{AssetPlugin, UnapprovedPathMode};
use bevy::prelude::*;
use bevy_panorbit_camera::{PanOrbitCamera, PanOrbitCameraPlugin};
use openusd::usd::Stage;
use usd_bevy::UsdPlugin;

use crate::scene::{Asset, Scene, arena_to_bevy};
use crate::spawn::spawn_asset;

/// The scene the app was launched with, kept so `setup` can read it.
#[derive(Resource)]
struct ResolvedScene(Scene);

/// Parent of everything in the layout; holds the Z-up → Y-up correction so
/// every asset below it keeps the pose the solver reported, unaltered.
#[derive(Component)]
pub struct ArenaRoot;

/// The spec node a placement came from.
#[derive(Component)]
pub struct AssetId(pub String);

pub fn run(scene: Scene) {
    App::new()
        .add_plugins((
            DefaultPlugins
                .set(WindowPlugin {
                    primary_window: Some(Window {
                        title: format!("arena · {}", scene.env_name),
                        ..default()
                    }),
                    ..default()
                })
                // A USD's textures sit next to it out in the asset cache, and
                // Bevy refuses paths outside its own asset root by default.
                .set(AssetPlugin {
                    unapproved_path_mode: UnapprovedPathMode::Allow,
                    ..default()
                }),
            PanOrbitCameraPlugin,
            // Route registry plus the projection caches that let a repeated
            // prim share the first one's GPU meshes. Deliberately *not*
            // `LiveStagePlugin`: each asset is its own stage, and none of them
            // is edited here.
            UsdPlugin,
        ))
        // `UsdPlugin` only installs the built-in registry if none is present,
        // which is how this one gets to replace its material route.
        .insert_resource(crate::material::registry())
        .insert_resource(ResolvedScene(scene))
        .add_systems(Startup, setup)
        .run();
}

/// Exclusive because projecting a stage spawns entities with components the
/// schema registry picks at runtime, which needs `&mut World`.
fn setup(world: &mut World) {
    let scene = world.resource::<ResolvedScene>().0.clone();

    let root = world
        .spawn((
            ArenaRoot,
            Transform::from_rotation(arena_to_bevy()),
            Visibility::default(),
        ))
        .id();

    for asset in &scene.assets {
        let Some(usd_path) = asset.usd_to_draw() else {
            continue;
        };
        let placement = world
            .spawn((
                AssetId(asset.id.clone()),
                asset.transform(),
                Visibility::default(),
                ChildOf(root),
            ))
            .id();
        match Stage::open(usd_path) {
            Ok(stage) => spawn_asset(world, &stage, placement),
            // One unreadable asset should not cost the whole scene.
            Err(error) => warn!("{}: cannot open {usd_path}: {error}", asset.id),
        }
    }

    // Arena builds the env with a dome light when neither the spec nor the
    // background USD supplies one. Anything the background *does* supply came
    // in with its geometry above, so this only stands in for the injected one.
    if scene.lighting.auto_injected_dome_light {
        world.spawn((
            DirectionalLight {
                illuminance: 6_000.0,
                ..default()
            },
            Transform::default().looking_to(Vec3::new(-0.4, -1.0, -0.6), Vec3::Y),
        ));
    }

    spawn_camera(world, &scene);
    spawn_hud(world, &scene);
}

fn spawn_camera(world: &mut World, scene: &Scene) {
    let (eye, focus) = framing(scene);
    world.spawn((
        Camera3d::default(),
        Transform::from_translation(eye).looking_at(focus, Vec3::Y),
        // Radius, yaw and pitch are left unset so the controller derives them
        // from the transform above.
        PanOrbitCamera { focus, ..default() },
        // Per-camera in Bevy 0.19, not a resource. Lifts the shadowed side of
        // an asset enough to read its silhouette.
        AmbientLight {
            brightness: 400.0,
            ..default()
        },
    ));
}

/// Where to put the camera, in Bevy's frame: the background's suggestion when
/// it has one, else a three-quarter view framing every asset's bounds.
fn framing(scene: &Scene) -> (Vec3, Vec3) {
    let to_bevy = arena_to_bevy();
    if let Some(viewer) = &scene.viewer {
        return (
            to_bevy * Vec3::from(viewer.eye),
            to_bevy * Vec3::from(viewer.lookat),
        );
    }
    let Some((min, max)) = bounds(scene) else {
        return (Vec3::new(2.0, 1.5, 2.0), Vec3::ZERO);
    };
    let focus = (min + max) / 2.0;
    // 1.6x the diagonal keeps the whole layout inside a default 45° FOV with
    // room to spare, and never collapses onto a single-point scene.
    let distance = (max - min).length().max(0.5) * 1.6;
    (
        focus + Vec3::new(0.7, 0.55, 1.0).normalize() * distance,
        focus,
    )
}

/// The layout's world-space bounds in Bevy's frame, from the local bounding
/// boxes the resolver reported.
fn bounds(scene: &Scene) -> Option<(Vec3, Vec3)> {
    let to_bevy = arena_to_bevy();
    let mut min = Vec3::splat(f32::INFINITY);
    let mut max = Vec3::splat(f32::NEG_INFINITY);
    let mut any = false;
    for asset in scene.assets.iter().filter(|a| a.usd_to_draw().is_some()) {
        for corner in corners(asset) {
            let point = to_bevy * asset.transform().transform_point(corner);
            min = min.min(point);
            max = max.max(point);
            any = true;
        }
    }
    any.then_some((min, max))
}

/// The eight corners of an asset's local bounding box.
fn corners(asset: &Asset) -> impl Iterator<Item = Vec3> {
    let (lo, hi) = (Vec3::from(asset.bbox_min), Vec3::from(asset.bbox_max));
    (0..8).map(move |i| {
        Vec3::new(
            if i & 1 == 0 { lo.x } else { hi.x },
            if i & 2 == 0 { lo.y } else { hi.y },
            if i & 4 == 0 { lo.z } else { hi.z },
        )
    })
}

fn spawn_hud(world: &mut World, scene: &Scene) {
    let drawn = scene
        .assets
        .iter()
        .filter(|a| a.usd_to_draw().is_some())
        .count();
    world.spawn((
        Text::new(format!(
            "{}  ·  {drawn}/{} assets drawn  ·  drag orbit · right-drag pan · wheel zoom",
            scene.env_name,
            scene.assets.len(),
        )),
        TextFont::from_font_size(11.0),
        TextColor(Color::srgb(0.6, 0.61, 0.64)),
        Node {
            position_type: PositionType::Absolute,
            left: px(10),
            bottom: px(8),
            ..default()
        },
    ));
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scene_json(assets: &str) -> Scene {
        Scene::from_json(&format!(r#"{{"env_name":"t","assets":[{assets}]}}"#)).unwrap()
    }

    /// A 1 m cube sitting 1 m up in Arena's frame is 1 m up in Bevy's too,
    /// and the camera looks at its centre rather than the world origin.
    #[test]
    fn framing_centres_on_the_layout() {
        let scene = scene_json(
            r#"{"id":"a","role":"object","local_usd_path":"/x.usd",
                "position_xyz":[0,0,1],"rotation_xyzw":[0,0,0,1],
                "bbox_min":[-0.5,-0.5,-0.5],"bbox_max":[0.5,0.5,0.5]}"#,
        );
        let (eye, focus) = framing(&scene);
        assert!(focus.abs_diff_eq(Vec3::Y, 1e-5), "focus {focus}");
        assert!(eye.distance(focus) > 1.0, "camera sits off the subject");
    }

    /// The frame conversion, end to end: a pose the solver reports in Arena's
    /// Z-up world has to come out at the matching point of Bevy's Y-up one.
    /// The USD path is deliberately bogus — this is about where the placement
    /// lands, not what hangs off it.
    #[test]
    fn a_solved_pose_lands_in_the_bevy_frame() {
        let scene = scene_json(
            r#"{"id":"a","role":"object","local_usd_path":"/does-not-exist.usd",
                "position_xyz":[1,2,3],"rotation_xyzw":[0,0,0,1],
                "bbox_min":[0,0,0],"bbox_max":[0,0,0]}"#,
        );
        let mut app = App::new();
        app.add_plugins(bevy::transform::TransformPlugin)
            .insert_resource(ResolvedScene(scene))
            .add_systems(Startup, setup);
        app.update();

        let world = app.world_mut();
        let placed = world
            .query::<(&AssetId, &GlobalTransform)>()
            .iter(world)
            .map(|(id, transform)| (id.0.clone(), transform.translation()))
            .collect::<Vec<_>>();
        let [(id, translation)] = placed.as_slice() else {
            panic!("expected exactly one placement, got {placed:?}");
        };
        assert_eq!(id, "a");
        // Arena (x, y, z) → Bevy (x, z, -y).
        assert!(
            translation.abs_diff_eq(Vec3::new(1.0, 3.0, -2.0), 1e-5),
            "{translation}"
        );
    }

    /// An asset with nothing to draw must not drag the framing towards the
    /// origin — an empty scene falls back to a fixed view instead.
    #[test]
    fn undrawable_assets_are_left_out_of_the_bounds() {
        let scene = scene_json(
            r#"{"id":"ref","role":"object_reference","local_usd_path":"/x.usd",
                "position_xyz":[10,10,10],"rotation_xyzw":[0,0,0,1],
                "bbox_min":[0,0,0],"bbox_max":[1,1,1]}"#,
        );
        assert!(bounds(&scene).is_none());
    }
}

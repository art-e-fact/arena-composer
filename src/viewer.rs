//! The viewer: a windowed Bevy app that draws the layout it is sent.
//!
//! Bare bones on purpose — it uploads the geometry the payload carries under
//! the pose the solver picked, and gives you an orbit camera to look at it.
//! There is no editing and no sim. A resolve happens in the process that sends
//! the scenes; this one only ever draws the newest.

use std::sync::Mutex;
use std::sync::mpsc::{Receiver, TryRecvError};

use bevy::asset::{AssetPlugin, UnapprovedPathMode};
use bevy::prelude::*;
use bevy_panorbit_camera::{PanOrbitCamera, PanOrbitCameraPlugin};

use crate::layout::{ArenaRoot, View, ambient, framing, spawn_lighting, spawn_scene};
use crate::scene::Scene;

/// The scene the app was launched with, kept so `setup` can read it.
#[derive(Resource)]
struct ResolvedScene(Scene);

/// Scenes sent after the first, newest last. `Mutex` because a resource has to
/// be `Sync` and a `Receiver` is not.
#[derive(Resource)]
struct SceneUpdates(Mutex<Receiver<Scene>>);

/// The status line, marked so a reload can rewrite it.
#[derive(Component)]
struct Hud;

pub fn run(scene: Scene, updates: Receiver<Scene>) {
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
                // Textures sit next to their USD out in the asset cache, and
                // Bevy refuses paths outside its own asset root by default.
                .set(AssetPlugin {
                    unapproved_path_mode: UnapprovedPathMode::Allow,
                    ..default()
                }),
            PanOrbitCameraPlugin,
        ))
        .insert_resource(ResolvedScene(scene))
        .insert_resource(SceneUpdates(Mutex::new(updates)))
        .add_systems(Startup, setup)
        .add_systems(Update, apply_updates)
        .run();
}

/// Exclusive because building meshes and materials needs `&mut World` to reach
/// both asset collections while spawning.
fn setup(world: &mut World) {
    let scene = world
        .remove_resource::<ResolvedScene>()
        .expect("a scene to open the window on")
        .0;
    spawn_scene(world, &scene);
    spawn_lighting(world, &scene);
    spawn_camera(world, &scene);
    spawn_hud(world, &scene);
}

/// Draw the newest scene that has arrived, and close the window once no more
/// will — the sender drops the channel when it is done with us.
///
/// The camera is left where the viewer put it: re-framing on every save is the
/// thing that makes a reload loop unusable.
fn apply_updates(world: &mut World) {
    let mut newest = None;
    let ended = loop {
        let received = world
            .resource::<SceneUpdates>()
            .0
            .lock()
            .expect("the scene channel")
            .try_recv();
        match received {
            // Only the last one is worth drawing; edits outrun a resolve.
            Ok(scene) => newest = Some(scene),
            Err(TryRecvError::Empty) => break false,
            Err(TryRecvError::Disconnected) => break true,
        }
    };

    if let Some(scene) = newest {
        // The new root goes up before the old one comes down: textures are
        // shared by path, so the overlap is what keeps them off the disk. Mesh
        // and material assets are rebuilt either way — `Assets::add` mints a
        // new one per call — and the old ones are freed with their handles.
        info!("drawing {} ({} assets)", scene.env_name, scene.assets.len());
        let root = spawn_scene(world, &scene);
        let stale: Vec<Entity> = world
            .query_filtered::<Entity, With<ArenaRoot>>()
            .iter(world)
            .filter(|entity| *entity != root)
            .collect();
        for entity in stale {
            // Children go with their parent.
            world.entity_mut(entity).despawn();
        }
        if let Ok(mut text) = world
            .query_filtered::<&mut Text, With<Hud>>()
            .single_mut(world)
        {
            text.0 = hud_line(&scene);
        }
    }

    if ended {
        world.write_message(AppExit::Success);
    }
}

fn spawn_camera(world: &mut World, scene: &Scene) {
    let framed = framing(View::Default, scene);
    world.spawn((
        Camera3d::default(),
        framed.transform,
        framed.projection,
        // Radius, yaw and pitch are left unset so the controller derives them
        // from the transform above.
        PanOrbitCamera {
            focus: framed.focus,
            ..default()
        },
        ambient(),
    ));
}

fn spawn_hud(world: &mut World, scene: &Scene) {
    world.spawn((
        Hud,
        Text::new(hud_line(scene)),
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

/// What the status line says about a scene.
fn hud_line(scene: &Scene) -> String {
    let drawn = scene
        .assets
        .iter()
        .filter(|asset| !scene.geometry_of(asset).is_empty())
        .count();
    format!(
        "{}  ·  {drawn}/{} assets drawn  ·  drag orbit · right-drag pan · wheel zoom",
        scene.env_name,
        scene.assets.len(),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::layout::AssetId;
    use crate::scene::{Asset, Node as GeometryNode};

    /// One asset at `position`, pointing at a geometry entry. The node itself
    /// is degenerate — no test here uploads a mesh.
    fn scene(position: [f32; 3]) -> Scene {
        Scene {
            env_name: "t".into(),
            assets: vec![Asset {
                id: "a".into(),
                role: "object".into(),
                position_xyz: position,
                geometry: Some(0),
                ..default()
            }],
            geometries: vec![vec![GeometryNode::default()]],
            ..default()
        }
    }

    /// The frame conversion, end to end: a pose the solver reports in Arena's
    /// Z-up world has to come out at the matching point of Bevy's Y-up one.
    #[test]
    fn a_solved_pose_lands_in_the_bevy_frame() {
        let mut app = App::new();
        app.add_plugins(bevy::transform::TransformPlugin)
            .insert_resource(ResolvedScene(scene([1.0, 2.0, 3.0])))
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

    /// A scene that arrives after startup replaces what is drawn, and leaves
    /// the camera looking wherever it was pointed.
    #[test]
    fn a_later_scene_replaces_the_layout() {
        let (sender, updates) = std::sync::mpsc::channel();
        let mut app = App::new();
        app.add_plugins(bevy::transform::TransformPlugin)
            .insert_resource(ResolvedScene(scene([1.0, 0.0, 0.0])))
            .insert_resource(SceneUpdates(Mutex::new(updates)))
            .add_systems(Startup, setup)
            .add_systems(Update, apply_updates);
        app.update();

        let world = app.world_mut();
        let camera = world
            .query_filtered::<Entity, With<Camera3d>>()
            .iter(world)
            .next()
            .expect("a camera at startup");

        sender
            .send(scene([0.0, 0.0, 5.0]))
            .expect("the viewer still listening");
        app.update();

        let world = app.world_mut();
        let roots: Vec<Entity> = world
            .query_filtered::<Entity, With<ArenaRoot>>()
            .iter(world)
            .collect();
        assert_eq!(roots.len(), 1, "the old layout is still in the world");

        let placed: Vec<Vec3> = world
            .query_filtered::<&GlobalTransform, With<AssetId>>()
            .iter(world)
            .map(|transform| transform.translation())
            .collect();
        // Arena (0, 0, 5) is Bevy (0, 5, 0): the second scene, not the first.
        assert_eq!(placed.len(), 1, "{placed:?}");
        assert!(placed[0].abs_diff_eq(Vec3::new(0.0, 5.0, 0.0), 1e-5), "{}", placed[0]);

        let cameras: Vec<Entity> = world
            .query_filtered::<Entity, With<Camera3d>>()
            .iter(world)
            .collect();
        assert_eq!(cameras, vec![camera], "the camera was rebuilt");
    }
}

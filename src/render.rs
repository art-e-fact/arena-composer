//! One-shot offscreen renders: draw a scene from named views, write a PNG each,
//! exit.
//!
//! Same scene, same camera code as the window (see [`crate::layout`]) — what
//! differs is that there is no window, no orbit controller and no second frame.
//! An agent asking "did that edit land where I meant" gets a picture back
//! without a display server, and the process is gone a second later.
//!
//! Every view renders into its own image target in the same frame, so N views
//! cost one process start rather than N.

use std::path::PathBuf;
use std::time::Duration;

use bevy::app::ScheduleRunnerPlugin;
use bevy::asset::{AssetPlugin, LoadState, UnapprovedPathMode};
use bevy::camera::RenderTarget;
use bevy::prelude::*;
use bevy::render::RenderPlugin;
use bevy::render::render_resource::TextureFormat;
use bevy::render::view::screenshot::{Screenshot, ScreenshotCaptured, save_to_disk};
use bevy::window::ExitCondition;
use bevy::winit::WinitPlugin;

use crate::layout::{View, ambient, framing, spawn_lighting, spawn_scene};
use crate::scene::Scene;

/// Frames to render after the textures have settled, before capturing. Gives
/// every camera a pass to upload its meshes and build its bind groups.
///
/// Small because `synchronous_pipeline_compilation` (see [`run`]) already
/// removes the reason a first frame comes back empty.
const SETTLE_FRAMES: u32 = 3;

/// Frames to wait for textures before giving up and capturing anyway. A render
/// that returns a flat-shaded picture beats one that never returns; at the
/// runner's 60 Hz this is ten seconds.
const LOAD_TIMEOUT_FRAMES: u32 = 600;

/// What to draw, and where to put it.
#[derive(Resource)]
struct Request {
    scene: Scene,
    shots: Vec<(View, PathBuf)>,
    width: u32,
    height: u32,
}

/// The view one camera was placed for, and the file it owes.
///
/// Carried on the camera rather than paired up by index: query iteration order
/// is not spawn order, and a mix-up here would label every picture wrong.
#[derive(Component)]
struct Shot {
    view: View,
    path: PathBuf,
}

/// How far along the capture is.
#[derive(Resource, Default)]
struct Progress {
    frames: u32,
    /// Frame the textures settled on, once they have.
    ready: Option<u32>,
    captured: bool,
    saved: usize,
    wanted: usize,
}

/// Draw `scene` once from each of `shots`, writing a PNG to each path.
///
/// Blocks until every image is on disk. Must be called on the main thread —
/// not for a window's sake, there is none, but because wgpu's device is built
/// here and the app owns the process from this point.
pub fn run(scene: Scene, shots: Vec<(View, PathBuf)>, width: u32, height: u32) {
    App::new()
        .add_plugins((
            DefaultPlugins
                .set(WindowPlugin {
                    // No window at all: with `WinitPlugin` gone there is nothing
                    // to create one, and `DontExit` keeps the app alive without.
                    primary_window: None,
                    exit_condition: ExitCondition::DontExit,
                    ..default()
                })
                // Textures sit next to their USD out in the asset cache, and
                // Bevy refuses paths outside its own asset root by default.
                .set(AssetPlugin {
                    unapproved_path_mode: UnapprovedPathMode::Allow,
                    ..default()
                })
                // Bevy specializes and compiles a pipeline the first time it
                // is needed, and draws nothing until it is ready. A window
                // covers that in the frames nobody looks at; a one-shot render
                // would capture the empty frames instead, so it waits.
                .set(RenderPlugin {
                    synchronous_pipeline_compilation: true,
                    ..default()
                })
                // Panics where there is no display server, which is exactly
                // where this front end is meant to run.
                .disable::<WinitPlugin>(),
            // Stands in for winit's runner, which was what drove the loop.
            ScheduleRunnerPlugin::run_loop(Duration::from_secs_f64(1.0 / 60.0)),
        ))
        .insert_resource(Request {
            scene,
            shots,
            width,
            height,
        })
        .init_resource::<Progress>()
        .add_plugins(crate::spawn::plugin)
        .add_systems(Startup, setup)
        .add_systems(Update, capture)
        .run();
}

/// Exclusive because building meshes and materials needs `&mut World` to reach
/// both asset collections while spawning.
fn setup(world: &mut World) {
    let request = world.remove_resource::<Request>().expect("a scene to draw");
    spawn_scene(world, &request.scene);
    spawn_lighting(world, &request.scene);
    world.resource_mut::<Progress>().wanted = request.shots.len();

    for (view, path) in &request.shots {
        // One target per view. The screenshot plugin skips duplicate targets,
        // so sharing an image between two cameras would silently drop a shot.
        let target = world.resource_mut::<Assets<Image>>().add(
            Image::new_target_texture(
                request.width,
                request.height,
                TextureFormat::Rgba8UnormSrgb,
                None,
            ),
        );
        let framed = framing(*view, &request.scene);
        world.spawn((
            Camera3d::default(),
            RenderTarget::Image(target.into()),
            framed.transform,
            framed.projection,
            ambient(),
            Shot {
                view: *view,
                path: path.clone(),
            },
        ));
    }
}

/// Wait for the textures, let the cameras draw, then capture every view at once
/// and exit when the files are down.
fn capture(
    mut commands: Commands,
    server: Res<AssetServer>,
    materials: Res<Assets<StandardMaterial>>,
    shots: Query<(&RenderTarget, &Shot)>,
    mut progress: ResMut<Progress>,
    mut exit: MessageWriter<AppExit>,
) {
    progress.frames += 1;

    if progress.captured {
        // `save_to_disk` writes the file from the observer itself, so a scene
        // that has been counted is a scene that is on disk.
        if progress.saved == progress.wanted {
            exit.write(AppExit::Success);
        }
        return;
    }

    if progress.ready.is_none() {
        let timed_out = progress.frames >= LOAD_TIMEOUT_FRAMES;
        if timed_out {
            warn!("textures still loading after {} frames", progress.frames);
        }
        if !timed_out && !textures_settled(&server, &materials) {
            return;
        }
        progress.ready = Some(progress.frames);
    }

    // Capturing on the frame the cameras first exist would grab an empty
    // target: meshes and bind groups are prepared for a camera that has
    // already been extracted at least once.
    if progress.frames < progress.ready.unwrap_or(0) + SETTLE_FRAMES {
        return;
    }

    for (target, shot) in shots.iter() {
        info!("capturing {:?} to {}", shot.view, shot.path.display());
        commands
            .spawn(Screenshot(target.clone()))
            .observe(save_to_disk(shot.path.clone()))
            .observe(|_: On<ScreenshotCaptured>, mut progress: ResMut<Progress>| {
                progress.saved += 1;
            });
    }
    progress.captured = true;
}

/// Whether every texture the scene's materials point at has finished loading —
/// or failed, which no amount of further waiting will fix.
///
/// Without this the render races the asset server and comes back untextured:
/// nothing in a one-shot app forces a second frame the way an open window does.
fn textures_settled(server: &AssetServer, materials: &Assets<StandardMaterial>) -> bool {
    materials
        .iter()
        .flat_map(|(_, material)| {
            [
                &material.base_color_texture,
                &material.emissive_texture,
                &material.normal_map_texture,
                &material.metallic_roughness_texture,
                &material.occlusion_texture,
            ]
        })
        .flatten()
        .all(|handle| {
            !matches!(
                server.get_load_state(handle.id()),
                Some(LoadState::NotLoaded | LoadState::Loading)
            )
        })
}

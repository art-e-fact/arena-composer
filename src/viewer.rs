//! The viewer: a windowed Bevy app that draws the layout it is sent.
//!
//! It uploads the geometry the payload carries under the pose the solver
//! picked, and gives you an orbit camera to look at it. There is no sim, and
//! no solve: a resolve happens in the process that sends the scenes, and this
//! one only ever draws the newest.
//!
//! Editing is [`crate::editor`]'s, and is the same arrangement — the panel
//! sends an edit up the pipe and draws the scene that comes back, so nothing
//! here writes a spec either.

use std::path::PathBuf;
use std::sync::Mutex;
use std::sync::mpsc::{Receiver, TryRecvError};

use bevy::asset::{AssetPlugin, UnapprovedPathMode};
use bevy::prelude::*;
use bevy::render::view::screenshot::{Capturing, Screenshot, save_to_disk};
use bevy_panorbit_camera::{PanOrbitCamera, PanOrbitCameraPlugin};

use crate::layout::{ArenaRoot, AssetId, Placed, View, framing, sky, spawn_lighting, spawn_scene};
use crate::scene::{Asset, Scene};

/// The scene the app was launched with, kept so `setup` can read it.
#[derive(Resource)]
struct ResolvedScene(Scene);

/// The last layout Python sent, kept whole after it is drawn: another of the
/// layouts its solve drew is drawn from it without asking Python again, and
/// one whose object set draws a member the others do not needs its geometry.
/// `shown` is the layout on screen, none until the first of it is drawn.
#[derive(Resource)]
struct Solved {
    scene: Scene,
    shown: Option<usize>,
}

/// Scenes sent after the first, newest last. `Mutex` because a resource has to
/// be `Sync` and a `Receiver` is not.
#[derive(Resource)]
struct SceneUpdates(Mutex<Receiver<Scene>>);

/// The status line, marked so a reload can rewrite it.
#[derive(Component)]
struct Hud;

/// What the window clears to: dark enough for a white robot on light tables to
/// read against, and not the grey the editor panel is drawn in.
const BACKDROP: Color = Color::srgb(57.0 / 255.0, 65.0 / 255.0, 79.0 / 255.0); // #39414f

pub fn run(scene: Scene, updates: Receiver<Scene>) {
    let mut app = App::new();
    app.insert_resource(ClearColor(BACKDROP))
        .add_plugins((
            DefaultPlugins
                .set(WindowPlugin {
                    primary_window: Some(Window {
                        title: format!("arena · {}", scene.env_name),
                        resolution: resolution(),
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
        // In `run` and not in `setup`: the editor needs a render context to
        // draw into, and `setup`'s tests build a world without one.
        .add_plugins(crate::editor::plugin)
        // Nothing unless `ARENA_VIEWER_TOUR` names a storyboard. After the
        // window is set up, which a tour resizes.
        .add_plugins(crate::tour::plugin)
        .add_plugins(crate::spawn::plugin)
        .insert_resource(ResolvedScene(scene))
        .insert_resource(SceneUpdates(Mutex::new(updates)))
        .add_systems(Startup, setup)
        .add_systems(Update, apply_updates);

    // `ARENA_VIEWER_SCREENSHOT=out.png` — draw a while, write the window to
    // that path, quit. The way to look at the UI without a person at the
    // keyboard; it is the whole window, so it shows nothing but this app.
    if let Some(path) = std::env::var_os("ARENA_VIEWER_SCREENSHOT") {
        app.insert_resource(ScreenshotPath(path.into()))
            .add_systems(Update, screenshot);
    }

    // A tour that cannot be played says so in the exit code, which is what
    // `docs/video/record.sh` stops on. The app, and the recording with it, is
    // finished by the time `run` returns.
    if let AppExit::Error(code) = app.run() {
        std::process::exit(code.get().into());
    }
}

/// `ARENA_VIEWER_SIZE=1440x900` opens the window that size, for a screenshot
/// of a known size. It is in logical pixels, so a display scaled 2× doubles the
/// picture; `docs/screenshots/shoot.sh` draws on one at 1×.
fn resolution() -> bevy::window::WindowResolution {
    let size = std::env::var("ARENA_VIEWER_SIZE").unwrap_or_default();
    let parsed = size.split_once('x').and_then(|(width, height)| Some((width.parse().ok()?, height.parse().ok()?)));
    parsed.map_or_else(default, |(width, height)| bevy::window::WindowResolution::new(width, height))
}

/// Where [`screenshot`] writes, when the environment asked for one.
#[derive(Resource)]
struct ScreenshotPath(PathBuf);

fn screenshot(
    mut commands: Commands,
    mut frame: Local<u32>,
    path: Res<ScreenshotPath>,
    capturing: Query<(), With<Capturing>>,
    mut exit: MessageWriter<AppExit>,
) {
    // Long enough for the geometry to upload and its shaders to compile;
    // neither happens on the frame the scene arrives.
    const SETTLE: u32 = 30;
    *frame += 1;
    match *frame {
        SETTLE => {
            commands
                .spawn(Screenshot::primary_window())
                .observe(save_to_disk(path.0.clone()));
        }
        // The capture is written on a task; leaving before it lands writes
        // nothing.
        n if n > SETTLE && capturing.is_empty() => {
            exit.write(AppExit::Success);
        }
        _ => {}
    }
}

/// Exclusive because building meshes and materials needs `&mut World` to reach
/// both asset collections while spawning.
fn setup(world: &mut World) {
    let scene = world
        .remove_resource::<ResolvedScene>()
        .expect("a scene to open the window on")
        .0;
    spawn_lighting(world, &scene);
    spawn_camera(world, &scene);
    spawn_hud(world, &scene);
    show_in_form(world, &scene);
    take_from_all(world, std::slice::from_ref(&scene));
    world.insert_resource(Solved { scene, shown: None });
    turn_page(world);
}

/// Hand a scene to the editor panel, where there is one.
///
/// Absent in the render path, and in the tests below — both draw a scene
/// without ever showing a form.
fn show_in_form(world: &mut World, scene: &Scene) {
    if let Some(mut form) = world.get_resource_mut::<crate::editor::Form>() {
        form.take(scene);
    }
}

/// Keep whatever any of `scenes` says about the assets, and every error any of
/// them carries.
///
/// Apart from [`show_in_form`] because these answer different messages: a
/// crawl that finishes while an edit is in flight lands on a message of its
/// own, which need not be the one the panel is drawn from, and so can a
/// refusal.
fn take_from_all(world: &mut World, scenes: &[Scene]) {
    let Some(mut form) = world.get_resource_mut::<crate::editor::Form>() else {
        return;
    };
    for scene in scenes {
        form.take_assets(scene);
        form.take_errors(scene);
        form.take_layouts(scene);
    }
}

/// Draw the newest scene that has arrived, and close the window once no more
/// will — the sender drops the channel when it is done with us.
///
/// The camera is left where the viewer put it: re-framing on every save is the
/// thing that makes a reload loop unusable.
fn apply_updates(world: &mut World) {
    let mut received = Vec::new();
    let ended = loop {
        let message = world
            .resource::<SceneUpdates>()
            .0
            .lock()
            .expect("the scene channel")
            .try_recv();
        match message {
            Ok(scene) => received.push(scene),
            Err(TryRecvError::Empty) => break false,
            Err(TryRecvError::Disconnected) => break true,
        }
    };

    // Only a message without a layout answers the panel: it carries the file
    // as it read when it was sent, where a layout carries the spec its own
    // solve began from. An edit made during those seconds leaves that copy
    // behind, and taking it would walk the form back to an older value until
    // the next solve landed. `view._draw` sends a freshly read spec behind
    // every layout, and that is what lands here. The panel is opened on a
    // layout's own spec once, in `setup`.
    if let Some(scene) = received.iter().rev().find(|scene| scene.assets.is_none()) {
        show_in_form(world, scene);
    }
    take_from_all(world, &received);

    // The newest of each kind rather than simply the newest: an accepted edit
    // is answered twice, with the spec and then with the layout it solves to,
    // so the last message need not be the last one carrying a layout.
    if let Some(newest) = received.iter().rposition(|scene| scene.assets.is_some()) {
        let scene = received.swap_remove(newest);
        info!("drawing {} ({} assets)", scene.env_name, scene.assets().len());
        world.insert_resource(Solved { scene, shown: None });
    }
    turn_page(world);

    if ended {
        world.write_message(AppExit::Success);
    }
}

/// Draw the layout the panel's pager is on, where it is not the one drawn: a
/// page turned, or a new solve landed.
///
/// Another layout of the same solve stands the same pieces somewhere else,
/// wherever each asset draws what it drew before, so those are moved rather
/// than built again and a page turns at once, however heavy the scene. A new
/// solve is drawn afresh, and so is a layout whose object set draws another
/// member.
fn turn_page(world: &mut World) {
    let page = world.get_resource::<crate::editor::Form>().map_or(0, |form| form.page);
    let Some(solved) = world.get_resource::<Solved>() else {
        return;
    };
    // A payload packed before there were samples carries its one layout as
    // `assets` alone.
    let page = page.min(solved.scene.layouts.len().saturating_sub(1));
    if solved.shown == Some(page) {
        return;
    }
    world.resource_scope(|world, mut solved: Mut<Solved>| {
        let Solved { scene, shown } = &mut *solved;
        let moved = match scene.layouts.get(page) {
            Some(layout) => {
                let moved = shown.is_some() && same_pieces(scene.assets(), &layout.assets);
                scene.assets = Some(layout.assets.clone());
                moved
            }
            None => false,
        };
        let scene = &*scene;
        if moved {
            let mut placements = world.query::<(&AssetId, &mut Transform)>();
            for (id, mut transform) in placements.iter_mut(world) {
                if let Some(asset) = scene.assets().iter().find(|asset| asset.id == id.0) {
                    *transform = asset.transform();
                }
            }
            world.insert_resource(Placed(scene.assets().to_vec()));
        } else {
            // The new root goes up before the old one comes down: textures are
            // shared by path, so the overlap is what keeps them off the disk.
            // Mesh and material assets are rebuilt either way — `Assets::add`
            // mints a new one per call — and the old ones are freed with their
            // handles.
            let root = spawn_scene(world, scene, &[]);
            let stale: Vec<Entity> = world
                .query_filtered::<Entity, With<ArenaRoot>>()
                .iter(world)
                .filter(|entity| *entity != root)
                .collect();
            for entity in stale {
                // Children go with their parent.
                world.entity_mut(entity).despawn();
            }
        }
        if let Ok(mut text) = world
            .query_filtered::<&mut Text, With<Hud>>()
            .single_mut(world)
        {
            text.0 = hud_line(scene);
        }
        *shown = Some(page);
    });
}

/// Whether `to` draws the pieces `from` does, each asset its own, so that
/// moving them is all it takes to go from one to the other.
fn same_pieces(from: &[Asset], to: &[Asset]) -> bool {
    from.len() == to.len() && from.iter().zip(to).all(|(a, b)| a.id == b.id && a.geometry == b.geometry)
}

fn spawn_camera(world: &mut World, scene: &Scene) {
    let framed = framing(View::Default, scene);
    let sky = sky(world);
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
        sky,
    ));
}

/// The status line, in the window's top-left corner: the asset browser docks
/// along the foot of the view, and the error toasts take its bottom-right.
fn spawn_hud(world: &mut World, scene: &Scene) {
    world.spawn((
        Hud,
        Text::new(hud_line(scene)),
        TextFont::from_font_size(11.0),
        TextColor(Color::srgb(0.6, 0.61, 0.64)),
        Node {
            position_type: PositionType::Absolute,
            left: px(10),
            top: px(8),
            ..default()
        },
    ));
}

/// What the status line says about a scene. ASCII only: it is drawn in Bevy's
/// bundled font, which draws anything past that as a box.
fn hud_line(scene: &Scene) -> String {
    let drawn = scene
        .assets()
        .iter()
        .filter(|asset| !scene.geometry_of(asset).is_empty())
        .count();
    format!(
        "{}  |  {drawn}/{} assets drawn  |  drag orbit, right-drag pan, wheel zoom, click pick, double-click focus, right-click menu",
        scene.env_name,
        scene.assets().len(),
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
            assets: Some(vec![Asset {
                id: "a".into(),
                role: "object".into(),
                position_xyz: position,
                geometry: Some(0),
                ..default()
            }]),
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

    /// An accepted edit is answered twice — with the spec as soon as it is
    /// written, and with the layout it solves to seconds later — so both kinds
    /// can land in one frame, in either order. Neither may mask the other: a
    /// spec that arrives last must not empty the viewport, and a layout that
    /// arrives first must still be drawn.
    #[test]
    fn a_spec_and_a_layout_in_one_frame_both_land() {
        let (sender, updates) = std::sync::mpsc::channel();
        let mut app = App::new();
        app.add_plugins(bevy::transform::TransformPlugin)
            .init_resource::<crate::editor::Form>()
            .insert_resource(ResolvedScene(scene([1.0, 0.0, 0.0])))
            .insert_resource(SceneUpdates(Mutex::new(updates)))
            .add_systems(Startup, setup)
            .add_systems(Update, apply_updates);
        app.update();

        // The solve for the previous edit, then the next edit answered at once.
        sender.send(scene([0.0, 0.0, 5.0])).expect("the viewer still listening");
        let mut pending = Scene {
            solving: Some(true),
            ..default()
        };
        pending.spec.env_name = "edited".into();
        sender.send(pending).expect("the viewer still listening");
        app.update();

        let world = app.world_mut();
        let placed: Vec<Vec3> = world
            .query_filtered::<&GlobalTransform, With<AssetId>>()
            .iter(world)
            .map(|transform| transform.translation())
            .collect();
        // Arena (0, 0, 5) is Bevy (0, 5, 0): the layout, not the spec after it.
        assert_eq!(placed.len(), 1, "the layout was torn down: {placed:?}");
        assert!(placed[0].abs_diff_eq(Vec3::new(0.0, 5.0, 0.0), 1e-5), "{}", placed[0]);

        let form = world.resource::<crate::editor::Form>();
        assert_eq!(form.spec.env_name, "edited", "the form kept the older spec");
        assert!(form.solving, "the panel has nothing to say the view is behind");
    }

    /// The prop list answers a crawl, not an edit, so the message carrying it
    /// need not be the one the panel is drawn from. Taking only that one would
    /// drop a list that landed in the same frame as an edit's answer — and the
    /// browser would go on waiting for a crawl that had already finished.
    #[test]
    fn a_prop_list_lands_whichever_message_carried_it() {
        use crate::spec::CatalogAsset;

        let (sender, updates) = std::sync::mpsc::channel();
        let mut app = App::new();
        app.add_plugins(bevy::transform::TransformPlugin)
            .init_resource::<crate::editor::Form>()
            .insert_resource(ResolvedScene(scene([1.0, 0.0, 0.0])))
            .insert_resource(SceneUpdates(Mutex::new(updates)))
            .add_systems(Startup, setup)
            .add_systems(Update, apply_updates);
        app.update();

        // The crawl's answer, and then an edit's — the panel takes the second.
        let crawled = Scene {
            remote: Some(vec![CatalogAsset {
                name: "Plastic_Basket_A01".into(),
                ..default()
            }]),
            ..default()
        };
        sender.send(crawled).expect("the viewer still listening");
        let mut edited = Scene::default();
        edited.spec.env_name = "edited".into();
        sender.send(edited).expect("the viewer still listening");
        app.update();

        let form = app.world().resource::<crate::editor::Form>();
        assert_eq!(form.spec.env_name, "edited", "the panel took the wrong message");
        let names: Vec<&str> = form.remote.iter().map(|asset| asset.name.as_str()).collect();
        assert_eq!(names, ["Plastic_Basket_A01"], "the prop list was dropped");
        assert!(!form.indexing, "a message that said nothing about a crawl started one");
    }

    /// Whether a crawl is running is Python's answer, and it is kept until
    /// Python says otherwise. A message that says nothing about one — an edit
    /// answered while the crawl runs, or the pictures it asked for landing —
    /// must not put the button back before the crawl is done.
    #[test]
    fn a_crawl_runs_until_python_says_it_stopped() {
        let (sender, updates) = std::sync::mpsc::channel();
        let mut app = App::new();
        app.add_plugins(bevy::transform::TransformPlugin)
            .init_resource::<crate::editor::Form>()
            .insert_resource(ResolvedScene(scene([1.0, 0.0, 0.0])))
            .insert_resource(SceneUpdates(Mutex::new(updates)))
            .add_systems(Startup, setup)
            .add_systems(Update, apply_updates);
        app.update();

        let said = |indexing: Option<bool>| Scene {
            indexing,
            ..default()
        };
        sender.send(said(Some(true))).expect("the viewer still listening");
        app.update();
        assert!(app.world().resource::<crate::editor::Form>().indexing);

        // An edit's answer, and a batch of pictures: neither ends the crawl.
        sender.send(said(None)).expect("the viewer still listening");
        app.update();
        assert!(app.world().resource::<crate::editor::Form>().indexing, "the crawl was cut short");

        sender.send(said(Some(false))).expect("the viewer still listening");
        app.update();
        assert!(!app.world().resource::<crate::editor::Form>().indexing);
    }

    /// A solve of one asset drawing a layout at each of `xs`, along Arena's x,
    /// each layout's asset drawing the geometry `pieces` gives it.
    fn solved(xs: &[f32], pieces: &[usize]) -> Scene {
        let layout = |x: f32, geometry: usize| crate::scene::Layout {
            assets: vec![Asset {
                id: "a".into(),
                role: "object".into(),
                position_xyz: [x, 0.0, 0.0],
                geometry: Some(geometry),
                ..default()
            }],
            ..default()
        };
        let layouts: Vec<_> = xs.iter().zip(pieces).map(|(&x, &geometry)| layout(x, geometry)).collect();
        Scene {
            env_name: "t".into(),
            assets: Some(layouts[0].assets.clone()),
            layouts,
            geometries: vec![vec![GeometryNode::default()], vec![GeometryNode::default()]],
            ..default()
        }
    }

    /// The window on `scene`, with a panel to page with.
    fn paging(scene: Scene) -> (App, std::sync::mpsc::Sender<Scene>) {
        let (sender, updates) = std::sync::mpsc::channel();
        let mut app = App::new();
        app.add_plugins(bevy::transform::TransformPlugin)
            .init_resource::<crate::editor::Form>()
            .insert_resource(ResolvedScene(scene))
            .insert_resource(SceneUpdates(Mutex::new(updates)))
            .add_systems(Startup, setup)
            .add_systems(Update, apply_updates);
        app.update();
        (app, sender)
    }

    /// Where the one asset stands, in Arena's x, and the root it hangs under.
    fn drawn(app: &mut App) -> (f32, Entity) {
        let world = app.world_mut();
        let placed: Vec<(f32, Entity)> = world
            .query_filtered::<(&GlobalTransform, &ChildOf), With<AssetId>>()
            .iter(world)
            .map(|(transform, parent)| (transform.translation().x, parent.parent()))
            .collect();
        let [(x, root)] = placed.as_slice() else { panic!("expected one placement, got {placed:?}") };
        let in_placed = world.resource::<Placed>().0[0].position_xyz[0];
        assert_eq!(in_placed, *x, "the relations are drawn against another layout");
        (*x, *root)
    }

    /// A solve opens on its first layout, and the pager turns to the others.
    /// They stand the same pieces elsewhere, so a turn moves what is drawn and
    /// builds nothing.
    #[test]
    fn a_page_turns_by_moving_what_is_drawn() {
        let (mut app, _sender) = paging(solved(&[1.0, 2.0], &[0, 0]));
        let (x, root) = drawn(&mut app);
        assert_eq!(x, 1.0);

        app.world_mut().resource_mut::<crate::editor::Form>().page = 1;
        app.update();
        assert_eq!(drawn(&mut app), (2.0, root), "the layout was built again to move it");
    }

    /// A layout whose object set draws another member draws other pieces, so
    /// turning to it builds them.
    #[test]
    fn a_page_drawing_another_member_is_built_afresh() {
        let (mut app, _sender) = paging(solved(&[1.0, 2.0], &[0, 1]));
        let (_, root) = drawn(&mut app);

        app.world_mut().resource_mut::<crate::editor::Form>().page = 1;
        app.update();
        let (x, rebuilt) = drawn(&mut app);
        assert_eq!(x, 2.0);
        assert_ne!(rebuilt, root, "the other member was not drawn");
    }

    /// An edit's solve keeps the page: the same env, solved again, moves only
    /// as far as the edit moved it.
    #[test]
    fn a_new_solve_stays_on_the_page() {
        let (mut app, sender) = paging(solved(&[1.0, 2.0], &[0, 0]));
        app.world_mut().resource_mut::<crate::editor::Form>().page = 1;
        app.update();

        sender.send(solved(&[3.0, 4.0], &[0, 0])).expect("the viewer still listening");
        app.update();
        assert_eq!(drawn(&mut app).0, 4.0);
        assert_eq!(app.world().resource::<crate::editor::Form>().page, 1);
    }

    /// A layout carries the spec its solve began from, so an edit made while it
    /// was solving leaves that copy behind. The panel keeps the newer spec it
    /// was answered with, and the late layout is still drawn.
    #[test]
    fn a_late_solve_does_not_take_the_panel_back() {
        let (sender, updates) = std::sync::mpsc::channel();
        let mut app = App::new();
        app.add_plugins(bevy::transform::TransformPlugin)
            .init_resource::<crate::editor::Form>()
            .insert_resource(ResolvedScene(scene([1.0, 0.0, 0.0])))
            .insert_resource(SceneUpdates(Mutex::new(updates)))
            .add_systems(Startup, setup)
            .add_systems(Update, apply_updates);
        app.update();

        // The second edit, answered from the file, and then the solve for the
        // first, carrying the spec as it read before that edit was made.
        let mut edited = Scene {
            solving: Some(true),
            ..default()
        };
        edited.spec.env_name = "second".into();
        sender.send(edited).expect("the viewer still listening");
        let mut solved = scene([0.0, 0.0, 5.0]);
        solved.spec.env_name = "first".into();
        sender.send(solved).expect("the viewer still listening");
        app.update();

        let world = app.world_mut();
        let placed = world
            .query_filtered::<&GlobalTransform, With<AssetId>>()
            .iter(world)
            .count();
        assert_eq!(placed, 1, "the layout that arrived last was not drawn");

        let form = world.resource::<crate::editor::Form>();
        assert_eq!(form.spec.env_name, "second", "an older spec took the panel back");
        // The spec `view._draw` sends behind the layout is what ends the solve,
        // so the spinner stays on until that lands.
        assert!(form.solving, "the layout answered the panel");
    }
}

//! The editor panel: the authored spec as a form, docked on the right.
//!
//! Drawn in immediate mode, so the panel *is* the last spec Python sent — there
//! is no widget tree to keep in step with it, and nothing to rebuild when a new
//! one arrives. What little state the panel keeps of its own is UI state and
//! nothing else: a search string, the number currently under the pointer, and
//! the pictures the asset browser has uploaded.
//!
//! Nothing here edits the spec it draws. A control sends a [`Request`] up the
//! pipe, Python writes the YAML and answers with it, and that answer is what
//! the panel draws from. That is why there is nothing to reconcile, and why a
//! refused edit needs no undo: the spec never changed.
//!
//! The layout takes a second answer, seconds later, because solving it is the
//! slow half — so [`Form::solving`] is true in between, and a spinner over the
//! 3D view says so. The panel itself is answered from the spec alone and needs
//! no such notice; only the view it sits beside is still catching up.
//!
//! Nothing here knows the name of a relation kind or the parameters it takes
//! either. [`Vocabulary`] arrives in the payload carrying a
//! [`Field`](crate::spec::Field) per parameter, inferred from Arena's
//! registries, so a relation somebody registers themselves gets a form without
//! a change to this code.
//!
//! Every control that adds something picks from what the registries offer — an
//! object from the asset list, a relation from a kind menu — so there is no
//! "add now, fix the invalid parts after". What a menu writes is an entry Arena
//! already accepts.
//!
//! Hovering a relation draws what it asks for over the layout, and a toggle on
//! each keeps it drawn; see [`relations`].

mod browser;
mod drop;
mod form;
mod relations;

use std::collections::HashMap;

use bevy::camera::SubCameraView;
use bevy::prelude::*;
use bevy_egui::input::EguiWantsInput;
use bevy_egui::{EguiPlugin, EguiPostUpdateSet, EguiPrimaryContextPass, egui};
use bevy_panorbit_camera::{PanOrbitCamera, PanOrbitCameraSystemSet};

use crate::layout::Placed;
use crate::scene::Scene as Payload;
use crate::spec::{CatalogAsset, Request, Spec, Vocabulary};
use crate::wire;

pub fn plugin(app: &mut App) {
    app.add_plugins(EguiPlugin::default())
        .init_resource::<Form>()
        .init_resource::<PointerOverPanel>()
        .init_resource::<Placed>()
        .init_gizmo_group::<relations::RelationGizmos>()
        // The panel belongs in egui's own pass, not in `Update`: that is where
        // a context exists to draw into.
        .add_systems(EguiPrimaryContextPass, (form::draw, drop::drop_onto_the_layout).chain())
        // Between the two, so the camera acts on the pass that just ran:
        // `ProcessOutput` is where egui publishes what this frame's panel made
        // of the pointer, and the camera set is what reads `enabled` after. In
        // `Update` both of those are still a frame away.
        .add_systems(
            PostUpdate,
            keep_the_camera_off_the_panel
                .after(EguiPostUpdateSet::ProcessOutput)
                .before(PanOrbitCameraSystemSet),
        );
    relations::configure(&mut app.world_mut().resource_mut::<GizmoConfigStore>());
}

/// Whether the pointer was over the panel when it was last drawn.
///
/// egui will not answer this for us. `bevy_egui` builds a root `Ui` and throws
/// it away — the Bevy schedule runs in its place — so a panel has to be docked
/// into a `Ui` of our own on the background layer, as `form::draw` does. For
/// that layer `Context::is_pointer_over_egui` answers out of the *root* ui's
/// leftover space, which nothing ever shrinks, so it reads false over the panel
/// and every `EguiWantsInput` field with it. Anything on a layer above the
/// background — a combo popup, a menu — egui still hit-tests correctly.
#[derive(Resource, Default)]
struct PointerOverPanel(bool);

/// The spec the panel draws.
#[derive(Resource, Default)]
pub struct Form {
    pub spec: Spec,
    pub vocabulary: Vocabulary,
    /// The SimReady props, kept from whichever message last carried them.
    /// Most carry none — see [`crate::scene::Scene::remote`].
    pub remote: Vec<CatalogAsset>,
    pub owners: HashMap<String, String>,
    pub errors: Vec<String>,
    /// Whether the layout under the panel is still catching up with this spec.
    pub solving: bool,
    /// Bumped when a different spec lands, or an edit is refused. A half-made
    /// edit is kept against the spec it was made on and dropped when that spec
    /// is replaced, so a number does not snap back to its old value between
    /// letting go of it and the spec carrying it coming back.
    pub revision: u64,
    /// Whether a crawl of the bucket is running. Python's answer, kept from
    /// whichever message last carried one.
    pub indexing: bool,
}

impl Form {
    /// Take the parts of `scene` the panel draws.
    ///
    /// Every message carries all of them but the prop list, layout or not: an
    /// accepted edit is answered with the spec as soon as it is written and
    /// again with the layout it solves to, and a refused one with the spec left
    /// untouched. So there is no half a message to guard against here. The prop
    /// list is the one part that travels apart, and is [`Form::take_assets`]'s.
    pub fn take(&mut self, scene: &Payload) {
        // Many messages carry the spec unchanged — a crawl's answer, pictures
        // landing, a solve catching up — and one of those must not drop a name
        // being typed. A refusal carries it unchanged too, and must: the value
        // it refused goes back to the one the spec still holds.
        let refused = scene.errors.as_ref().is_some_and(|errors| !errors.is_empty());
        if refused || scene.spec != self.spec {
            self.revision += 1;
        }

        self.spec = scene.spec.clone();
        self.vocabulary = scene.vocabulary.clone();
        self.owners = scene.owners.clone();
        if let Some(errors) = &scene.errors {
            self.errors = errors.clone();
        }
        if let Some(solving) = scene.solving {
            self.solving = solving;
        }
    }

    /// Keep what `scene` says about the assets, where it says anything.
    ///
    /// Apart from [`Form::take`] because the two answer different messages: the
    /// props are a megabyte that changes only when the index is rebuilt or a
    /// picture lands, so the message carrying them need not be the one the
    /// panel is drawn from. The two halves move apart as well — a crawl says it
    /// has started long before it has a list to show for it.
    pub fn take_assets(&mut self, scene: &Payload) {
        if let Some(remote) = &scene.remote {
            self.remote = remote.clone();
        }
        if let Some(indexing) = scene.indexing {
            self.indexing = indexing;
        }
    }

    /// Every asset a spec can name, registered and remote alike.
    pub fn assets(&self) -> impl Iterator<Item = &CatalogAsset> {
        self.vocabulary.assets.iter().chain(&self.remote)
    }

    /// Which file a top-level key lives in, for the badge beside its group.
    fn owner(&self, key: &str) -> &str {
        self.owners.get(key).map_or("", String::as_str)
    }
}

/// Hand one request to Python.
///
/// Straight down stdout rather than through a channel and a thread: a request
/// is tens of bytes against a 64 KiB pipe with Python in a read loop, so the
/// write cannot block long enough to be worth getting off this thread.
// ponytail: blocking write on the render thread; channel + thread if an intent
// ever outruns Python.
fn send(request: Request) {
    if let Err(error) = wire::write_request(&mut std::io::stdout().lock(), &request) {
        error!("request: {error:#}");
    }
}

/// The layout camera's picture, off-centre so that its middle is the middle of
/// what a `panel` on the right leaves of the `window`, both in one unit.
///
/// The window shows the right-hand part of a picture one panel wider, whose
/// centre sits half a panel left of the window's. Only the ratios count, so egui
/// points serve as well as pixels. Bevy culls against the projection without
/// this offset, which leaves out only the right half of the strip under the
/// panel.
fn beside_the_panel(window: egui::Vec2, panel: f32) -> SubCameraView {
    let (width, height, panel) = (window.x.round() as u32, window.y.round() as u32, panel.round() as u32);
    SubCameraView {
        full_size: UVec2::new(width + panel, height),
        offset: Vec2::new(panel as f32, 0.0),
        size: UVec2::new(width, height),
    }
}

/// Keep the orbit camera off the panel.
///
/// `PanOrbitCamera` claims a drag on the frame the button goes down, checking
/// only that the cursor is inside the camera's viewport — it knows nothing
/// about the panel drawn over it, so the panel has to take the camera away.
///
/// Not `bevy_panorbit_camera`'s own `bevy_egui` feature, which exists to do
/// exactly this: it is built against a different `bevy_egui` than the one we
/// draw with, so turning it on compiles a second egui and gates the camera on a
/// context nothing ever draws into. See the note in `arena_composer/AGENTS.md`.
fn keep_the_camera_off_the_panel(
    mouse: Res<ButtonInput<MouseButton>>,
    egui: Res<EguiWantsInput>,
    over_panel: Res<PointerOverPanel>,
    camera: Option<Single<&mut PanOrbitCamera>>,
) {
    let Some(mut camera) = camera else { return };
    let held = mouse.get_pressed().next().is_some() && mouse.get_just_pressed().next().is_none();
    // The two halves of "egui is busy with the pointer": the panel, which only
    // [`PointerOverPanel`] knows about, and everything drawn above it, which
    // only egui does.
    let busy = over_panel.0 || egui.wants_any_pointer_input();
    let Some(takes) = camera_takes_input(held, busy) else {
        return;
    };
    if camera.enabled != takes {
        camera.enabled = takes;
    }
}

/// Whether the camera takes pointer input from here on, or `None` to leave the
/// last answer standing.
///
/// A drag belongs to whatever the button went down over, for as long as it is
/// held. Without that latch a drag begun on the panel would orbit the moment it
/// left the panel, and one begun on the view would stop the moment it crossed
/// onto it.
fn camera_takes_input(mid_drag: bool, pointer_taken: bool) -> Option<bool> {
    (!mid_drag).then_some(!pointer_taken)
}

#[cfg(test)]
mod tests {
    use bevy::camera::CameraProjection;
    use bevy::prelude::{PerspectiveProjection, Vec3};
    use bevy_egui::egui;

    use super::{Form, Payload, beside_the_panel, camera_takes_input};

    /// What the camera looks at, straight ahead of it, is drawn in the middle
    /// of what the panel leaves.
    #[test]
    fn the_focus_is_drawn_mid_view() {
        let (width, panel) = (1280.0, 360.0);
        let sub_view = beside_the_panel(egui::vec2(width, 720.0), panel);
        let clip = PerspectiveProjection::default().get_clip_from_view_for_sub(&sub_view);
        let ndc = clip.project_point3(Vec3::NEG_Z);
        let drawn_at = (ndc.x + 1.0) / 2.0 * width;
        assert!((drawn_at - (width - panel) / 2.0).abs() < 0.5, "{drawn_at}");
    }

    /// Only a new spec or a refusal ends a half-made edit. Anything else —
    /// here, pictures landing — leaves it, along with what the panel was
    /// saying about the last edit and the solve behind it.
    #[test]
    fn a_message_about_assets_leaves_the_edit_in_hand() {
        let mut form = Form::default();
        let mut answer = Payload {
            errors: Some(vec![]),
            solving: Some(true),
            ..Default::default()
        };
        answer.spec.env_name = "edited".into();
        form.take(&answer);
        let revision = form.revision;

        let mut pictures = Payload::default();
        pictures.spec.env_name = "edited".into();
        form.take(&pictures);
        assert_eq!(form.revision, revision, "pictures landing dropped the edit in hand");
        assert!(form.solving, "pictures landing stopped the spinner");

        let refused = Payload {
            errors: Some(vec!["objects.0: named by a task".into()]),
            ..pictures.clone()
        };
        form.take(&refused);
        assert!(form.revision > revision, "a refused value would stay on screen");
        assert_eq!(form.errors, ["objects.0: named by a task"]);

        form.take(&pictures);
        assert_eq!(form.errors, ["objects.0: named by a task"], "the refusal was wiped");
    }

    /// Crossing the edge mid-drag settles nothing: an orbit that wanders over
    /// the panel keeps orbiting, and a slider drag that wanders off it keeps
    /// editing.
    #[test]
    fn a_drag_belongs_to_whatever_it_started_on() {
        let mid_drag = true;
        assert_eq!(camera_takes_input(mid_drag, true), None);
        assert_eq!(camera_takes_input(mid_drag, false), None);
    }

    /// Between drags the pointer decides, so the press that begins the next one
    /// goes to whatever it is over.
    #[test]
    fn between_drags_the_pointer_decides() {
        let mid_drag = false;
        assert_eq!(camera_takes_input(mid_drag, true), Some(false), "over the panel");
        assert_eq!(camera_takes_input(mid_drag, false), Some(true), "over the view");
    }
}


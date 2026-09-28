//! The editor panel: the authored spec as a form, docked on the right, with
//! the asset browser docked under the 3D view beside it.
//!
//! Drawn in immediate mode, so the panel *is* the last spec Python sent — there
//! is no widget tree to keep in step with it, and nothing to rebuild when a new
//! one arrives. What little state the panel keeps of its own is UI state and
//! nothing else: a search string, the number or name currently being edited,
//! and the asset pictures it has uploaded.
//!
//! Every entry of the spec is drawn as a [`card`], after Blender's modifier
//! stack: a header naming the entry — its id, typed into where it stands, or a
//! relation's kind — and a body of [`controls`] that folds away under it.
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
mod card;
mod controls;
mod drop;
mod form;
mod relations;
mod thumbnails;

use std::collections::HashMap;

use bevy::camera::SubCameraView;
use bevy::camera::primitives::Frustum;
use bevy::camera::visibility::VisibilitySystems;
use bevy::math::primitives::ViewFrustum;
use bevy::prelude::*;
use bevy_egui::input::EguiWantsInput;
use bevy_egui::{EguiPlugin, EguiPostUpdateSet, EguiPrimaryContextPass, egui};
use bevy_panorbit_camera::{PanOrbitCamera, PanOrbitCameraSystemSet};

use crate::layout::Placed;
use crate::scene::Scene as Payload;
use crate::spec::{CatalogAsset, Request, Spec, Vocabulary};

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
            (
                keep_the_camera_off_the_panel
                    .after(EguiPostUpdateSet::ProcessOutput)
                    .before(PanOrbitCameraSystemSet),
                // After Bevy has built the frustum it would cull with, and
                // before anything is culled against it.
                cull_to_the_picture
                    .after(VisibilitySystems::UpdateFrusta)
                    .before(VisibilitySystems::CheckVisibility),
            ),
        );
    relations::configure(&mut app.world_mut().resource_mut::<GizmoConfigStore>());
}

/// Whether the pointer was over the panel or the asset browser's dock when
/// they were last drawn.
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

/// How many error toasts stay open at once.
const MAX_TOASTS: usize = 5;

/// The spec the panel draws.
#[derive(Resource, Default)]
pub struct Form {
    pub spec: Spec,
    pub vocabulary: Vocabulary,
    /// The SimReady props, kept from whichever message last carried them.
    /// Most carry none — see [`crate::scene::Scene::remote`].
    pub remote: Vec<CatalogAsset>,
    pub owners: HashMap<String, String>,
    /// The errors not yet closed, oldest first, each a toast over the view:
    /// refusals, failed solves and failed crawls alike. See
    /// [`Form::take_errors`].
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
    /// list travels apart, and is [`Form::take_assets`]'s; the errors are
    /// [`Form::take_errors`]'s.
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

    /// Keep the errors `scene` carries until they are closed: one toast per
    /// message, since one refused edit is one event however many lines say why.
    ///
    /// Taken from every message, not only the one the panel is drawn from — a
    /// refusal followed within the frame by any other message would otherwise
    /// never be seen. And never cleared by a message carrying none: an edit
    /// that lands says nothing of the one refused before it. The same text
    /// twice is one toast, because a spec that will not build fails the same
    /// way on every solve.
    pub fn take_errors(&mut self, scene: &Payload) {
        let Some(errors) = scene.errors.as_ref().filter(|errors| !errors.is_empty()) else {
            return;
        };
        let error = errors.join("\n");
        if !self.errors.contains(&error) {
            self.errors.push(error);
        }
        // The oldest go first, so the stack stays shorter than the view.
        let over = self.errors.len().saturating_sub(MAX_TOASTS);
        self.errors.drain(..over);
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
#[cfg(not(test))]
fn send(request: Request) {
    if let Err(error) = crate::wire::write_request(&mut std::io::stdout().lock(), &request) {
        error!("request: {error:#}");
    }
}

/// What a test's panel sent, in order, instead of writing it down the test's
/// own stdout.
#[cfg(test)]
fn send(request: Request) {
    tests::SENT.with_borrow_mut(|sent| sent.push(request));
}

/// The layout camera's picture, drawn as though the window were only `view`:
/// what the panels leave of it. Both are rects in one unit, and only their
/// ratios count, so egui points serve as well as pixels.
///
/// The picture's full size is the view's, so its centre is the view's centre
/// and its vertical field of view spans the view's height: the framing a window
/// that size would give, zooming out as the dock grows over it. The window
/// shows that picture carried on past the view's edges, under the panels.
fn in_the_view(window: egui::Rect, view: egui::Rect) -> SubCameraView {
    // Never nought: a picture with no height has no aspect ratio.
    let size = |size: egui::Vec2| UVec2::new(size.x.round().max(1.0) as u32, size.y.round().max(1.0) as u32);
    let offset = window.min - view.min;
    SubCameraView { full_size: size(view.size()), offset: Vec2::new(offset.x, offset.y), size: size(window.size()) }
}

/// Cull against the picture the camera draws.
///
/// Bevy builds a camera's [`Frustum`] from its projection alone and never from
/// `sub_camera_view`, so it culls against a picture the window's own shape.
/// [`in_the_view`] draws a wider one wherever what the panels leave is wider
/// for its height than the window, and anything past the old frustum's edge,
/// though inside the view, would not be drawn.
fn cull_to_the_picture(mut cameras: Query<(&Camera, &GlobalTransform, &Projection, &mut Frustum)>) {
    for (camera, transform, projection, mut frustum) in &mut cameras {
        if camera.sub_camera_view.is_some() {
            *frustum = picture_frustum(camera.clip_from_view(), transform, projection.far());
        }
    }
}

/// The frustum of the picture `clip_from_view` draws, as Bevy builds one from a
/// projection's own matrix.
fn picture_frustum(clip_from_view: Mat4, transform: &GlobalTransform, far: f32) -> Frustum {
    let clip_from_world = clip_from_view * transform.affine().inverse();
    Frustum(ViewFrustum::from_clip_from_world_custom_far(
        &clip_from_world,
        &transform.translation(),
        &transform.back(),
        far,
    ))
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
/// context nothing ever draws into. See `docs/editor.md`.
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
    use std::cell::RefCell;

    use bevy::camera::CameraProjection;
    use bevy::camera::primitives::Aabb;
    use bevy::prelude::{GlobalTransform, PerspectiveProjection, Transform, Vec3};
    use bevy_egui::egui;

    use super::{Form, Payload, Request, camera_takes_input, in_the_view, picture_frustum};

    thread_local! {
        /// Every request a test's panel has sent; see `super::send`.
        pub static SENT: RefCell<Vec<Request>> = const { RefCell::new(Vec::new()) };
    }

    /// A window 1280 by 720, with the spec panel down its right and the asset
    /// browser along the foot of what that leaves.
    fn docked() -> (egui::Rect, egui::Rect) {
        let window = egui::Rect::from_min_size(egui::Pos2::ZERO, egui::vec2(1280.0, 720.0));
        let view = egui::Rect::from_min_max(window.min, egui::pos2(920.0, 440.0));
        (window, view)
    }

    /// Where a point in front of the camera is drawn in the window.
    fn drawn_at(clip: bevy::math::Mat4, window: egui::Rect, point: Vec3) -> egui::Pos2 {
        let ndc = clip.project_point3(point);
        egui::pos2((ndc.x + 1.0) / 2.0 * window.width(), (1.0 - ndc.y) / 2.0 * window.height())
    }

    /// What the camera looks at, straight ahead of it, is drawn in the middle
    /// of what the panels leave, and the field of view spans that height: the
    /// picture a window the size of the view would draw.
    #[test]
    fn the_view_is_framed_as_a_window_of_its_own() {
        let (window, view) = docked();
        let projection = PerspectiveProjection::default();
        let clip = projection.get_clip_from_view_for_sub(&in_the_view(window, view));

        let centre = drawn_at(clip, window, Vec3::NEG_Z);
        assert!((centre - view.center()).length() < 0.5, "{centre:?}");
        let top = drawn_at(clip, window, Vec3::new(0.0, (projection.fov / 2.0).tan(), -1.0));
        assert!((top.y - view.top()).abs() < 0.5, "{top:?}");
    }

    /// A panel down the side alone keeps the view's height, and so the zoom a
    /// window without panels would have.
    #[test]
    fn a_side_panel_alone_moves_the_picture_without_zooming_it() {
        let window = egui::Rect::from_min_size(egui::Pos2::ZERO, egui::vec2(1280.0, 720.0));
        let view = egui::Rect::from_min_max(window.min, egui::pos2(920.0, 720.0));
        let mut projection = PerspectiveProjection::default();
        projection.update(window.width(), window.height());
        let clip = projection.get_clip_from_view_for_sub(&in_the_view(window, view));
        let bare = projection.get_clip_from_view();

        let point = Vec3::new(0.3, 0.2, -1.0);
        let (docked, alone) = (drawn_at(clip, window, point), drawn_at(bare, window, point));
        assert!((docked.y - alone.y).abs() < 0.5, "{docked:?} against {alone:?}");
        assert!((alone.x - docked.x - 180.0).abs() < 0.5, "moved by half the panel: {docked:?}");
    }

    /// Something at the view's left edge is drawn, though Bevy's own frustum —
    /// the window's shape, not the view's — leaves it out.
    #[test]
    fn what_the_view_shows_is_not_culled() {
        let (window, view) = docked();
        let mut projection = PerspectiveProjection::default();
        projection.update(window.width(), window.height());
        let clip = projection.get_clip_from_view_for_sub(&in_the_view(window, view));
        let camera = GlobalTransform::from(Transform::default());

        // Ten points in from the view's left edge, 2 m ahead of the camera.
        let at = egui::pos2(view.left() + 10.0, view.center().y);
        let ndc = bevy::math::Vec3::new(at.x / window.width() * 2.0 - 1.0, 1.0 - at.y / window.height() * 2.0, 0.5);
        let ahead = clip.inverse().project_point3(ndc);
        let point = ahead * (2.0 / -ahead.z);
        let speck = Aabb::from_min_max(point - 0.01, point + 0.01);
        let whole = bevy::math::Affine3A::IDENTITY;

        let bevys = projection.compute_frustum(&camera);
        assert!(!bevys.intersects_obb(&speck, &whole, true, true), "the case this guards is gone");
        let ours = picture_frustum(clip, &camera, projection.far());
        assert!(ours.intersects_obb(&speck, &whole, true, true));
    }

    /// Only a new spec or a refusal ends a half-made edit. Anything else —
    /// here, pictures landing — leaves it, along with what the panel was
    /// saying about the solve behind it.
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
    }

    /// An error stays up until it is closed: an edit landing after it, or a
    /// message about something else, leaves it. The same one again is one
    /// toast, and a message's lines are one toast between them.
    #[test]
    fn an_error_stays_until_it_is_closed() {
        let with_errors = |errors: &[&str]| Payload {
            errors: Some(errors.iter().map(|error| error.to_string()).collect()),
            ..Default::default()
        };
        let mut form = Form::default();

        form.take_errors(&with_errors(&["objects.0: named by a task", "objects.0: no asset"]));
        form.take_errors(&with_errors(&[]));
        form.take_errors(&Payload::default());
        assert_eq!(form.errors, ["objects.0: named by a task\nobjects.0: no asset"]);

        form.take_errors(&with_errors(&["SpecError: no bowl"]));
        form.take_errors(&with_errors(&["SpecError: no bowl"]));
        assert_eq!(form.errors.len(), 2, "a repeated failure piled up: {:?}", form.errors);

        for index in 0..super::MAX_TOASTS {
            form.take_errors(&with_errors(&[&format!("error {index}")]));
        }
        assert_eq!(form.errors.len(), super::MAX_TOASTS);
        assert_eq!(form.errors.last().unwrap(), &format!("error {}", super::MAX_TOASTS - 1), "the newest went");
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


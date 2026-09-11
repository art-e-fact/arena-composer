//! The editor panel: the authored spec as a form, docked on the right.
//!
//! Drawn in immediate mode, so the panel *is* the last spec Python sent — there
//! is no widget tree to keep in step with it, and nothing to rebuild when a new
//! one arrives. What little state the panel keeps of its own is UI state and
//! nothing else: a search string, and the number currently under the pointer.
//!
//! Nothing here edits the spec it draws. A control sends an [`Edit`] up the
//! pipe, Python writes the YAML and re-resolves, and the next payload is what
//! the panel draws from. That is why there is nothing to reconcile, and why a
//! refused edit needs no undo: the spec never changed.
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

mod browser;
mod form;

use std::collections::HashMap;

use bevy::prelude::*;
use bevy_egui::input::EguiWantsInput;
use bevy_egui::{EguiPlugin, EguiPrimaryContextPass};
use bevy_panorbit_camera::PanOrbitCamera;

use crate::scene::Scene as Payload;
use crate::spec::{Edit, Spec, Vocabulary};
use crate::wire;

pub fn plugin(app: &mut App) {
    app.add_plugins(EguiPlugin::default())
        .init_resource::<Form>()
        // The panel belongs in egui's own pass, not in `Update`: that is where
        // a context exists to draw into.
        .add_systems(EguiPrimaryContextPass, form::draw)
        .add_systems(Update, keep_the_camera_off_the_panel);
}

/// The spec the panel draws.
#[derive(Resource, Default)]
pub struct Form {
    pub spec: Spec,
    pub vocabulary: Vocabulary,
    pub owners: HashMap<String, String>,
    pub errors: Vec<String>,
    /// Bumped every time a new spec lands. A half-made edit is kept against the
    /// spec it was made on and dropped when that spec is replaced, so a number
    /// does not snap back to its old value while Python is still re-solving.
    pub revision: u64,
}

impl Form {
    /// Take the parts of `scene` the panel draws.
    ///
    /// A scene with no assets is a status message — Python answering a refused
    /// edit — and carries no spec to draw from, only the reason.
    pub fn take(&mut self, scene: &Payload) {
        self.errors = scene.errors.clone();
        if scene.assets.is_empty() {
            return;
        }
        self.spec = scene.spec.clone();
        self.vocabulary = scene.vocabulary.clone();
        self.owners = scene.owners.clone();
        self.revision += 1;
    }

    /// Which file a top-level key lives in, for the badge beside its group.
    fn owner(&self, key: &str) -> &str {
        self.owners.get(key).map_or("", String::as_str)
    }
}

/// Hand one edit to Python.
///
/// Straight down stdout rather than through a channel and a thread: an edit is
/// tens of bytes against a 64 KiB pipe with Python in a read loop, so the write
/// cannot block long enough to be worth getting off this thread.
// ponytail: blocking write on the render thread; channel + thread if an intent
// ever outruns Python.
fn send(edit: Edit) {
    if let Err(error) = wire::write_edit(&mut std::io::stdout().lock(), &edit) {
        error!("edit intent: {error:#}");
    }
}

/// Keep the orbit camera off the panel.
///
/// `PanOrbitCamera` claims a drag on the frame the button goes down, checking
/// only that the cursor is inside the camera's viewport — it knows nothing
/// about the panel drawn over it. Latching the decision between drags is what
/// keeps a drag that starts in the panel from orbiting, and one that leaves the
/// panel from stopping.
fn keep_the_camera_off_the_panel(
    mouse: Res<ButtonInput<MouseButton>>,
    egui: Res<EguiWantsInput>,
    camera: Option<Single<&mut PanOrbitCamera>>,
) {
    let Some(mut camera) = camera else { return };
    if mouse.get_pressed().next().is_some() && mouse.get_just_pressed().next().is_none() {
        return; // mid-drag; whoever owns it keeps it
    }
    let taken = egui.wants_any_pointer_input();
    if camera.enabled == taken {
        camera.enabled = !taken;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A refused edit answers with the reason alone — re-reading every asset's
    /// USD to say "no" would cost seconds. The panel keeps the spec it has, and
    /// with it whatever the user was half way through typing.
    #[test]
    fn a_status_message_keeps_the_spec_it_is_drawing() {
        let mut form = Form::default();
        let mut scene = Payload::default();
        scene.assets.push(crate::scene::Asset::default());
        scene.spec.env_name = "demo".into();
        form.take(&scene);
        let revision = form.revision;

        form.take(&Payload {
            errors: vec!["objects.0: still needed by a task".into()],
            ..default()
        });
        assert_eq!(form.spec.env_name, "demo", "a status message replaced the spec");
        assert_eq!(form.errors.len(), 1);
        assert_eq!(form.revision, revision, "there is nothing new to draw");
    }
}

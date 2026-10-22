//! A click in the 3D view picks what it lands on, as a click on its node in
//! the graph does: the panel opens its card, and the graph outlines its node.
//! Hovering outlines what a click would pick, and a double click brings the
//! camera round to it.
//!
//! What a hit stands for is the drop's rule, [`target`]: the smallest placed
//! box that holds the hit point, so a click on a counter picks the reference
//! to its top and not the kitchen around it, whose mesh the ray hit — a
//! reference draws nothing of its own. Unlike a drop, a click can land on the
//! robot.
//!
//! The ray is cast when the pointer moves, or the layout under it changes,
//! and what it found is kept: a pointer at rest costs nothing, and a click
//! reads that answer rather than casting again. Nothing is cast while a
//! button is held, when the pointer is turning the view rather than pointing
//! at it.

use bevy::picking::mesh_picking::ray_cast::{MeshRayCast, MeshRayCastSettings};
use bevy::prelude::*;
use bevy::window::PrimaryWindow;
use bevy_egui::input::EguiWantsInput;
use bevy_egui::{EguiContexts, egui};
use bevy_panorbit_camera::PanOrbitCamera;

use super::drop::target;
use super::graph::Item;
use super::relations::{self, RelationGizmos};
use super::{PointerOverPanel, pointer_in_the_view};
use crate::layout::{Placed, fitting_distance, world_aabb};
use crate::scene::arena_to_bevy;

/// What a click in the view picked, for the panel to take up as it takes a
/// click in the graph: an entry, or nothing — the backdrop — which drops the
/// pick.
#[derive(Message)]
pub struct Clicked(pub Option<Item>);

/// Runs in egui's pass, after the panel and before the drop: [`PointerOverPanel`]
/// is this frame's once the panel has been drawn, and an asset still being
/// carried belongs to the drop.
#[allow(clippy::too_many_arguments)]
pub fn pick_in_the_view(
    mut contexts: EguiContexts,
    placed: Res<Placed>,
    over_panel: Res<PointerOverPanel>,
    egui_input: Res<EguiWantsInput>,
    window: Single<&Window, With<PrimaryWindow>>,
    camera: Single<(&Camera, &GlobalTransform, &mut PanOrbitCamera)>,
    mut ray_cast: MeshRayCast,
    mut gizmos: Gizmos<RelationGizmos>,
    mut clicked: MessageWriter<Clicked>,
    // What the pointer is over, by id, found when it last moved.
    mut under: Local<Option<String>>,
) -> Result {
    let ctx = contexts.ctx_mut()?;
    if !pointer_in_the_view(&over_panel, &egui_input) || egui::DragAndDrop::has_any_payload(ctx) {
        *under = None;
        return Ok(());
    }
    let (settled, click, double) = ctx.input(|input| {
        let pointer = &input.pointer;
        // Moved with nothing held, let go of a button, or turned the wheel:
        // whatever is under the pointer may have changed.
        let settled = (pointer.delta() != egui::Vec2::ZERO && !pointer.any_down())
            || pointer.any_released()
            || input.smooth_scroll_delta != egui::Vec2::ZERO;
        (settled, pointer.primary_clicked(), pointer.button_double_clicked(egui::PointerButton::Primary))
    });
    let (camera, transform, mut orbit) = camera.into_inner();
    if settled || placed.is_changed() {
        *under = window
            .cursor_position()
            .and_then(|cursor| camera.viewport_to_world(transform, cursor).ok())
            .and_then(|ray| ray_cast.cast_ray(ray, &MeshRayCastSettings::default()).first().map(|(_, hit)| hit.point))
            .and_then(|point| target(&placed, arena_to_bevy().inverse() * point, |_| true))
            .map(|asset| asset.id.clone());
    }
    let asset = under.as_deref().and_then(|id| relations::find(&placed, id));
    if click {
        clicked.write(Clicked(asset.map(|asset| Item::Node(asset.id.clone()))));
    }
    let Some(asset) = asset else {
        return Ok(());
    };
    relations::outline(asset, &mut gizmos);
    if double {
        let (min, max) = world_aabb(asset);
        orbit.target_focus = arena_to_bevy() * ((min + max) / 2.0);
        orbit.target_radius = fitting_distance(max - min);
    }
    Ok(())
}

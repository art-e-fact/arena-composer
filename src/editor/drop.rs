//! Dragging an asset out of the browser and onto the layout.
//!
//! The drag is egui's from end to end. A browser row's button hands egui the
//! asset when a drag starts on it, and that payload can be read by any system
//! in egui's pass — so the 3D view is a drop target without egui knowing there
//! is one. The orbit camera stays off throughout, because the drag began on the
//! panel.
//!
//! While an asset is held over the view, the surface an `on` would stand it on
//! is drawn, and letting go adds the object with that `on`. Over nothing that
//! can hold it — the backdrop, the robot — the target is the anchor, as for a
//! click. Let go over the panel, or press Escape, and nothing is added.

use bevy::picking::mesh_picking::ray_cast::{MeshRayCast, MeshRayCastSettings};
use bevy::prelude::*;
use bevy::window::PrimaryWindow;
use bevy_egui::{EguiContexts, egui};

use super::browser::{add, anchor, on_params};
use super::relations::{self, RelationGizmos};
use super::{Form, PointerOverPanel};
use crate::layout::{Placed, world_aabb};
use crate::scene::{Asset, arena_to_bevy};
use crate::spec::CatalogAsset;

/// How far outside its box a hit may land and still count as on it: the box
/// Arena reports and the mesh drawn here are read from one USD, but not by the
/// same code.
const SLACK: f32 = 0.01;

/// Runs in egui's pass, after the panel: the payload is only readable there,
/// and [`PointerOverPanel`] is this frame's once the panel has been drawn.
#[allow(clippy::too_many_arguments)]
pub fn drop_onto_the_layout(
    mut contexts: EguiContexts,
    form: Res<Form>,
    placed: Res<Placed>,
    over_panel: Res<PointerOverPanel>,
    window: Single<&Window, With<PrimaryWindow>>,
    camera: Single<(&Camera, &GlobalTransform)>,
    mut ray_cast: MeshRayCast,
    mut gizmos: Gizmos<RelationGizmos>,
) -> Result {
    let ctx = contexts.ctx_mut()?;
    let Some(asset) = egui::DragAndDrop::payload::<CatalogAsset>(ctx) else {
        return Ok(());
    };
    // What is being carried, beside the pointer rather than under it, where it
    // would hide what it is held over.
    if let Some(pointer) = ctx.pointer_latest_pos() {
        egui::Area::new(egui::Id::new("carried asset"))
            .order(egui::Order::Tooltip)
            .fixed_pos(pointer + egui::vec2(16.0, 16.0))
            .interactable(false)
            .show(ctx, |ui| egui::Frame::popup(ui.style()).show(ui, |ui| ui.label(&asset.name)));
    }
    // Back over the panel is a change of mind. egui drops the payload itself
    // at the end of the pass the button comes up in.
    if over_panel.0 {
        return Ok(());
    }

    let (camera, transform) = *camera;
    let hit = window
        .cursor_position()
        .and_then(|cursor| camera.viewport_to_world(transform, cursor).ok())
        .and_then(|ray| ray_cast.cast_ray(ray, &MeshRayCastSettings::default()).first().map(|(_, hit)| hit.point));
    let target = hit
        .and_then(|point| target(&placed, arena_to_bevy().inverse() * point))
        .or_else(|| relations::find(&placed, anchor(&form.spec)?));

    if let Some(parent) = target {
        let params = on_params(&form.spec, &parent.id);
        relations::draw_surface(parent, &params, form.vocabulary.relation("on"), &mut gizmos);
    }
    if ctx.input(|input| input.pointer.any_released()) {
        egui::DragAndDrop::clear_payload(ctx);
        add(&form.spec, &asset, target.map(|parent| parent.id.as_str()));
    }
    Ok(())
}

/// What a point on the layout, in Arena's frame, stands for: the smallest box
/// that holds it.
///
/// Smallest, so a point on a counter lands on the `table_top` reference and
/// not on the kitchen around it — a reference draws nothing of its own, so a
/// ray can only ever hit its parent. Where the two boxes are the same, as when
/// the reference names the whole of its parent, the reference still wins: it
/// is the node the spec stands things on. The embodiment is never stood on.
fn target(placed: &Placed, point: Vec3) -> Option<&Asset> {
    placed
        .0
        .iter()
        .filter(|asset| asset.role != "embodiment")
        .map(|asset| (asset, world_aabb(asset)))
        .filter(|(_, (min, max))| point.cmpge(*min - SLACK).all() && point.cmple(*max + SLACK).all())
        .min_by_key(|(asset, bounds)| (cubic_cm(*bounds), asset.role != "object_reference"))
        .map(|(asset, _)| asset)
}

/// A box's volume in whole cubic centimetres. Whole, so two boxes read from
/// the same prim compare equal whatever their last bits say.
fn cubic_cm((min, max): (Vec3, Vec3)) -> i64 {
    ((max - min).element_product() * 1e6).round() as i64
}

#[cfg(test)]
mod tests {
    use super::*;

    fn placed(id: &str, role: &str, min: [f32; 3], max: [f32; 3]) -> Asset {
        Asset {
            id: id.into(),
            role: role.into(),
            bbox_min: min,
            bbox_max: max,
            ..default()
        }
    }

    /// A table with a bowl on it and a reference to its top, and a robot whose
    /// box reaches over both.
    fn layout() -> Placed {
        Placed(vec![
            placed("droid", "embodiment", [-1.0, -1.0, 0.0], [2.0, 2.0, 2.0]),
            placed("table", "background", [0.0, 0.0, 0.0], [1.0, 1.0, 0.8]),
            placed("table_top", "object_reference", [0.0, 0.0, 0.8], [1.0, 1.0, 0.8]),
            placed("bowl", "object", [0.4, 0.4, 0.8], [0.6, 0.6, 0.9]),
        ])
    }

    fn id(asset: Option<&Asset>) -> Option<&str> {
        asset.map(|asset| asset.id.as_str())
    }

    /// The bowl's rim is the bowl, however much larger boxes also hold it.
    #[test]
    fn a_hit_lands_on_the_smallest_box_that_holds_it() {
        assert_eq!(id(target(&layout(), Vec3::new(0.5, 0.45, 0.9))), Some("bowl"));
    }

    /// The tabletop beside the bowl is the reference, which draws nothing and
    /// so is never what the ray hits — and a hit a hair above it still counts.
    #[test]
    fn a_hit_on_the_top_lands_on_the_reference_to_it() {
        assert_eq!(id(target(&layout(), Vec3::new(0.1, 0.1, 0.805))), Some("table_top"));
        assert_eq!(id(target(&layout(), Vec3::new(0.1, 0.1, 0.4))), Some("table"), "a table leg");
    }

    /// A reference to the whole of its parent reports the parent's box, to the
    /// last few bits. The reference is still the one stood on, whichever of
    /// the two comes out a hair smaller.
    #[test]
    fn a_reference_to_the_whole_parent_wins_the_tie() {
        for top in [0.7 - 1e-7, 0.7 + 1e-7] {
            let layout = Placed(vec![
                placed("table", "background", [0.0, 0.0, 0.0], [0.7, 1.0, 0.7]),
                placed("table_top", "object_reference", [0.0, 0.0, 0.0], [0.7, 1.0, top]),
            ]);
            assert_eq!(id(target(&layout, Vec3::new(0.3, 0.5, 0.7))), Some("table_top"), "top at {top}");
        }
    }

    /// The robot's arm, out past the table, is nothing to stand on.
    #[test]
    fn the_embodiment_is_never_a_target() {
        assert_eq!(id(target(&layout(), Vec3::new(1.5, 1.5, 1.0))), None);
    }
}

//! A click in the 3D view picks what it lands on, as a click on its node in
//! the graph does: the panel opens its card, and the graph outlines its node.
//! Hovering outlines what a click would pick, and a double click brings the
//! camera round to it.
//!
//! What a hit stands for is [`picked`]: the asset whose mesh the ray hit, or
//! one of that asset's object references, where the reference's box holds the
//! hit point — so a click on a counter picks the reference to its top and not
//! the kitchen around it, a reference drawing nothing of its own. Unlike a
//! drop, a click can land on the robot.
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
use super::{Form, PointerOverPanel, pointer_in_the_view};
use crate::layout::{AssetId, Placed, fitting_distance, world_aabb};
use crate::scene::{Asset, arena_to_bevy};
use crate::spec::Spec;

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
    form: Res<Form>,
    placed: Res<Placed>,
    over_panel: Res<PointerOverPanel>,
    egui_input: Res<EguiWantsInput>,
    window: Single<&Window, With<PrimaryWindow>>,
    camera: Single<(&Camera, &GlobalTransform, &mut PanOrbitCamera)>,
    mut ray_cast: MeshRayCast,
    // From a piece of mesh to the placement it hangs under, and its id.
    parents: Query<&ChildOf>,
    ids: Query<&AssetId>,
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
            .and_then(|ray| ray_cast.cast_ray(ray, &MeshRayCastSettings::default()).first().cloned())
            .and_then(|(piece, hit)| {
                let owner = ids.get(parents.get(piece).ok()?.parent()).ok()?;
                picked(&placed, &form.spec, &owner.0, arena_to_bevy().inverse() * hit.point)
            })
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

/// What a hit at `point`, in Arena's frame, on a mesh of the asset `hit`
/// picks: the smallest box that holds the point of that asset and its own
/// object references, by the drop's rule ([`target`]); else the asset itself.
///
/// The mesh the ray hit says which asset it is, and the box alone could not:
/// a robot placed by its stand reports the stand's box, so a hit on its arm
/// lies in no box but the room's. But a reference draws nothing, and only its
/// box can say a hit on the kitchen is on its counter.
fn picked<'a>(placed: &'a Placed, spec: &Spec, hit: &str, point: Vec3) -> Option<&'a Asset> {
    let ours = |asset: &Asset| {
        asset.id == hit || spec.object_references.iter().any(|part| part.id == asset.id && part.parent_id == hit)
    };
    target(placed, point, ours).or_else(|| relations::find(placed, hit))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::spec::ObjectReference;

    fn placed(id: &str, role: &str, min: [f32; 3], max: [f32; 3]) -> Asset {
        Asset { id: id.into(), role: role.into(), bbox_min: min, bbox_max: max, ..default() }
    }

    /// A kitchen with a counter top, a bowl on it, and a robot placed by its
    /// stand, whose arm reaches out over the counter.
    fn scene() -> (Placed, Spec) {
        let placed = Placed(vec![
            placed("kitchen", "background", [0.0, 0.0, 0.0], [4.0, 4.0, 3.0]),
            placed("counter", "object_reference", [1.0, 1.0, 0.9], [2.0, 2.0, 0.9]),
            placed("bowl", "object", [1.4, 1.4, 0.9], [1.6, 1.6, 1.0]),
            placed("droid", "embodiment", [2.5, 1.0, 0.0], [3.0, 1.5, 0.8]),
        ]);
        let mut spec = Spec::default();
        spec.object_references.push(ObjectReference {
            id: "counter".into(),
            parent_id: "kitchen".into(),
            ..default()
        });
        (placed, spec)
    }

    fn id(asset: Option<&Asset>) -> Option<&str> {
        asset.map(|asset| asset.id.as_str())
    }

    /// The robot's arm, out past the stand its box covers, is the robot.
    #[test]
    fn a_hit_on_the_robot_outside_its_box_picks_the_robot() {
        let (placed, spec) = scene();
        assert_eq!(id(picked(&placed, &spec, "droid", Vec3::new(2.2, 1.2, 1.3))), Some("droid"));
    }

    /// A hit on the kitchen's counter picks the reference to it; elsewhere
    /// on the kitchen, the kitchen.
    #[test]
    fn a_hit_on_the_counter_picks_the_reference_to_it() {
        let (placed, spec) = scene();
        assert_eq!(id(picked(&placed, &spec, "kitchen", Vec3::new(1.2, 1.2, 0.9))), Some("counter"));
        assert_eq!(id(picked(&placed, &spec, "kitchen", Vec3::new(0.1, 3.0, 2.0))), Some("kitchen"));
    }

    /// A hit on the bowl is the bowl, not the counter whose box its foot
    /// stands in; and a hit on the kitchen inside the bowl's box is not the bowl.
    #[test]
    fn only_the_hit_asset_and_its_references_are_candidates() {
        let (placed, spec) = scene();
        assert_eq!(id(picked(&placed, &spec, "bowl", Vec3::new(1.5, 1.5, 0.9))), Some("bowl"));
        assert_eq!(id(picked(&placed, &spec, "kitchen", Vec3::new(1.5, 1.5, 0.95))), Some("kitchen"));
    }
}

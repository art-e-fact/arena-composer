//! The backdrop the layout is drawn against: a vertical gradient with its
//! light band where the subject is, so a white robot reads against the dark
//! above it and a table's legs against the dark below.
//!
//! It is `bevy_ui`'s own gradient, drawn on a camera of its own *below* the
//! layout's, which then clears nothing and draws over it. It cannot go on the
//! layout's camera: the UI pass runs after the 3D one, so a full-screen node
//! there would cover the layout rather than sit behind it.
//!
//! Nor can it be geometry. Anything drawn inside the 3D pass is tonemapped,
//! and a backdrop authored there comes out as the inverse of a curve nobody
//! here controls — `#39414f` on an unlit dome landed at `#8b94a3`, with the
//! whole ramp flattened with it. A UI gradient reaches the frame as written.

use bevy::camera::ClearColorConfig;
use bevy::prelude::*;
use bevy::ui::{ColorStop, LinearGradient};

/// Put the backdrop in the world, and answer how the layout's camera should
/// clear over it.
pub fn spawn(world: &mut World) -> ClearColorConfig {
    let camera = world
        .spawn((
            Camera2d,
            Camera {
                order: -1,
                ..default()
            },
        ))
        .id();
    world.spawn((
        Node {
            width: percent(100),
            height: percent(100),
            ..default()
        },
        BackgroundGradient::from(LinearGradient::to_bottom(vec![
            stop("#2b3140", 0.0),
            stop("#7e8ca1", 44.0),
            stop("#1e212a", 100.0),
        ])),
        // Explicit, because a root node without one goes to the *highest*
        // order camera — the layout's, which draws over this one.
        UiTargetCamera(camera),
    ));
    ClearColorConfig::None
}

/// A colour stop, written the way a stylesheet would write it.
fn stop(hex: &str, percent: f32) -> ColorStop {
    ColorStop::percent(Srgba::hex(hex).expect("a backdrop colour"), percent)
}

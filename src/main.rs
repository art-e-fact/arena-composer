//! `arena-viewer <scene.msgpack>` — the viewer without the Python round trip.
//!
//! Takes what `arena-resolve-layout --payload scene.msgpack` wrote, so the Bevy
//! side can be iterated on without re-importing Arena, re-solving the layout
//! and re-reading every USD.

use std::path::PathBuf;

use arena_composer::scene::Scene;

fn main() -> anyhow::Result<()> {
    let Some(path) = std::env::args_os().nth(1).map(PathBuf::from) else {
        anyhow::bail!("usage: arena-viewer <scene.msgpack>");
    };
    let scene = Scene::from_msgpack(&std::fs::read(&path)?)?;
    // Nothing sends this one a second scene. The sender is held anyway, since
    // dropping it would read as "no more scenes" and close the window.
    let (_sender, updates) = std::sync::mpsc::channel();
    arena_composer::viewer::run(scene, updates);
    Ok(())
}

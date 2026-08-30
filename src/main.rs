//! `arena-viewer <scene.json>` — the viewer without the Python round trip.
//!
//! Takes what `arena-resolve-layout --output scene.json` wrote, so the Bevy
//! side can be iterated on without re-importing Arena and re-solving.

use std::path::PathBuf;

use arena_composer::scene::Scene;

fn main() -> anyhow::Result<()> {
    let Some(path) = std::env::args_os().nth(1).map(PathBuf::from) else {
        anyhow::bail!("usage: arena-viewer <scene.json>");
    };
    let scene = Scene::from_json(&std::fs::read_to_string(&path)?)?;
    arena_composer::viewer::run(scene);
    Ok(())
}

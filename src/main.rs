//! `arena-viewer <scene.msgpack> [<view>=<out.png> ... [--size N]]` — the
//! viewer, or with shots the headless renderer, without the Python round trip.
//!
//! Takes what `arena-resolve-layout --payload scene.msgpack` wrote, so the Bevy
//! side can be iterated on without re-importing Arena, re-solving the layout
//! and re-reading every USD. With no shots it opens the window; with them it
//! draws what `arena-render` would and exits.
//!
//! ```text
//! arena-viewer scene.msgpack
//! arena-viewer scene.msgpack top=top.png front=front.png --size 768
//! ```

use std::path::PathBuf;

use arena_composer::layout::View;
use arena_composer::scene::Scene;

fn main() -> anyhow::Result<()> {
    // `args_os`, and the payload taken first and as it is: a path may hold an
    // `=`, or bytes that are not UTF-8, and still name the file.
    let mut args = std::env::args_os().skip(1);
    let Some(payload) = args.next().map(PathBuf::from) else {
        anyhow::bail!("usage: arena-viewer <scene.msgpack> [<view>=<out.png> ... [--size N]]");
    };
    let mut shots: Vec<(View, PathBuf)> = Vec::new();
    let mut size = 768;

    while let Some(arg) = args.next() {
        let arg = arg
            .into_string()
            .map_err(|arg| anyhow::anyhow!("unexpected argument {arg:?}"))?;
        if arg == "--size" {
            size = args
                .next()
                .and_then(|value| value.into_string().ok()?.parse().ok())
                .ok_or_else(|| anyhow::anyhow!("--size wants a pixel count"))?;
        } else if let Some((view, path)) = arg.split_once('=') {
            let view: View = view.parse().map_err(anyhow::Error::msg)?;
            shots.push((view, PathBuf::from(path)));
        } else {
            anyhow::bail!("unexpected argument {arg:?}");
        }
    }

    let scene = Scene::from_msgpack(&std::fs::read(&payload)?)?;
    if shots.is_empty() {
        // Nothing sends this one a second scene. The sender is held anyway,
        // since dropping it would read as "no more scenes" and close the window.
        let (_sender, updates) = std::sync::mpsc::channel();
        arena_composer::viewer::run(scene, updates);
    } else {
        arena_composer::render::run(scene, shots, size, size);
    }
    Ok(())
}

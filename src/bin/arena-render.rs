//! `arena-render <scene.msgpack> <view>=<out.png> ...` — offscreen renders
//! without the Python round trip.
//!
//! Takes what `arena-resolve-layout --payload scene.msgpack` wrote, so the
//! camera framing can be iterated on without re-importing Arena, re-solving the
//! layout and re-reading every USD.
//!
//! ```text
//! arena-render scene.msgpack top=top.png front=front.png --size 768
//! ```

use std::path::PathBuf;

use arena_composer::layout::View;
use arena_composer::scene::Scene;

fn main() -> anyhow::Result<()> {
    let mut args = std::env::args().skip(1);
    let mut payload: Option<PathBuf> = None;
    let mut shots: Vec<(View, PathBuf)> = Vec::new();
    let mut size = 768;

    while let Some(arg) = args.next() {
        if arg == "--size" {
            size = args
                .next()
                .and_then(|value| value.parse().ok())
                .ok_or_else(|| anyhow::anyhow!("--size wants a pixel count"))?;
        } else if let Some((view, path)) = arg.split_once('=') {
            let view: View = view.parse().map_err(anyhow::Error::msg)?;
            shots.push((view, PathBuf::from(path)));
        } else if payload.is_none() {
            payload = Some(PathBuf::from(arg));
        } else {
            anyhow::bail!("unexpected argument {arg:?}");
        }
    }

    let (Some(payload), false) = (payload, shots.is_empty()) else {
        anyhow::bail!("usage: arena-render <scene.msgpack> <view>=<out.png> ... [--size N]");
    };
    let scene = Scene::from_msgpack(&std::fs::read(&payload)?)?;
    arena_composer::render::run(scene, shots, size, size);
    Ok(())
}

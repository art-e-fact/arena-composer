//! The browser front end: the viewer as a web page, over one packed payload.
//!
//! The page fetches the payload and hands it over whole ([`view`]); `web/index.html` is that page,
//! and `web/build.sh` builds this crate for it. Textures are fetched the same way, from `assets/`
//! beside the page, where `web/export.py` copied them and rewrote the payload to name them.
//!
//! Nothing answers the panel here: there is no Python behind the page, so an edit stays in its
//! card, as it does in `arena-viewer`.

use wasm_bindgen::prelude::*;

/// Draw one packed scene in a canvas that fills the page. Returns once the app is running, since
/// in a browser the page drives the frames; a payload that does not decode throws.
#[wasm_bindgen]
pub fn view(payload: &[u8]) -> Result<(), String> {
    let scene = crate::scene::Scene::from_msgpack(payload).map_err(|e| format!("{e:#}"))?;
    // Nothing sends a second scene, and a dropped sender reads as the end of the stream, which
    // closes the window. `run` has returned long before the page is closed, so the sender is
    // leaked rather than held.
    let (sender, updates) = std::sync::mpsc::channel();
    std::mem::forget(sender);
    crate::viewer::run(scene, updates);
    Ok(())
}

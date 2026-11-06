//! The browser front end: the viewer as a web page, over packed payloads the page hands it.
//!
//! The page hands over the opening payload whole ([`view`]), and any after it the same way
//! ([`push`]), as `arena-view`'s pipe would. The panel's requests go out through a function the
//! page defines, `globalThis.arena_request` ([`arena_request`]). What carries the two is the
//! page's business: `web/index.html` fetches one payload and only logs the requests, so an edit
//! stays in its card, as it does in `arena-viewer`. A page with a server behind it carries both
//! over a WebSocket. `web/build.sh` builds this crate for either.
//!
//! Textures are fetched by the path the payload names, against the page's origin: for
//! `web/index.html`, from `assets/` beside it, where `web/export.py` copied them and rewrote the
//! payload to name them.

use std::cell::RefCell;
use std::sync::mpsc::Sender;

use wasm_bindgen::prelude::*;

use crate::scene::Scene;
use crate::wire::Decoder;

thread_local! {
    /// What [`push`] needs of the running viewer: the geometry its last layout drew, and the
    /// channel it reads scenes from. Held here for the life of the page, since `run` returns long
    /// before it is closed, and a dropped sender reads as the end of the stream, which closes the
    /// window.
    static LINK: RefCell<Option<(Decoder, Sender<Scene>)>> = const { RefCell::new(None) };
}

#[wasm_bindgen]
extern "C" {
    /// Hand one request to the page, packed as `arena-view` reads one off the pipe, minus the
    /// length in front. The page defines it before the panel can send: `bytes => ws.send(bytes)`.
    #[wasm_bindgen(catch)]
    pub(crate) fn arena_request(request: &[u8]) -> Result<(), JsValue>;
}

/// Draw one packed scene in a canvas that fills the page. Returns once the app is running, since
/// in a browser the page drives the frames; a payload that does not decode throws. Call it once:
/// a second app would start a second event loop on the page.
#[wasm_bindgen]
pub fn view(payload: &[u8]) -> Result<(), String> {
    let (sender, updates) = std::sync::mpsc::channel();
    // Decoded inside the borrow and run outside it: `run` brings the app up before it returns.
    let scene = LINK
        .with_borrow_mut(|link| link.insert((Decoder::default(), sender)).0.decode(payload))
        .map_err(|e| format!("{e:#}"))?;
    crate::viewer::run(scene, updates);
    Ok(())
}

/// Hand the running viewer one more packed scene, as `arena-view` sends one down the pipe. A
/// payload that does not decode throws, and so does one pushed before [`view`].
#[wasm_bindgen]
pub fn push(payload: &[u8]) -> Result<(), String> {
    LINK.with_borrow_mut(|link| {
        let (decoder, sender) = link.as_mut().ok_or("nothing is being viewed yet")?;
        let scene = decoder.decode(payload).map_err(|e| format!("{e:#}"))?;
        sender.send(scene).map_err(|_| "the viewer has closed".to_owned())
    })
}

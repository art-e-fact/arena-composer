//! `arena_composer` — the Bevy half of this project's Arena tooling.
//!
//! Python resolves an `ArenaEnvGraphSpec` into a layout and extracts the USD
//! geometry to draw it with (see `arena_composer.payload`); this crate draws
//! what it packs. Two ways to look at the result, over one [`layout`]: the
//! windowed [`viewer`], which redraws whatever it is sent, and the one-shot
//! [`render`], which writes a PNG per view and exits.
//!
//! Each has a Python front end (the [`python`] module, behind the `python`
//! feature) and a `[[bin]]` that reads the same payload off disk.

pub mod background;
pub mod editor;
pub mod layout;
pub mod render;
pub mod scene;
pub mod spawn;
pub mod spec;
pub mod viewer;
pub mod wire;

#[cfg(feature = "python")]
mod python;

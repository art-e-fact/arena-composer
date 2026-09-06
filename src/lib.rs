//! `arena_composer` — the Bevy half of this project's Arena tooling.
//!
//! Python resolves an `ArenaEnvGraphSpec` into a layout and extracts the USD
//! geometry to draw it with (see `arena_composer.payload`); this crate draws
//! what it packs. Two front ends, one [`viewer::run`]: the [`python`] module
//! (behind the `python` feature) for `arena-view`, and a `[[bin]]` that reads
//! the same payload off disk.

pub mod scene;
pub mod spawn;
pub mod viewer;
pub mod wire;

#[cfg(feature = "python")]
mod python;

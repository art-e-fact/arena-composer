//! `arena_composer` — the Bevy half of this project's Arena tooling.
//!
//! Python resolves an `ArenaEnvGraphSpec` into a plain scene description (see
//! `arena_composer.resolve_layout`); this crate draws it. Two front ends, one
//! [`viewer::run`]: the [`python`] module (behind the `python` feature) for
//! `arena-view`, and a `[[bin]]` that reads the same JSON off disk.

pub mod material;
pub mod scene;
pub mod spawn;
pub mod viewer;

#[cfg(feature = "python")]
mod python;

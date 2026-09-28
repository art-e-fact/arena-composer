//! Asset pictures, for whichever card shows one: a row of the asset browser,
//! or an object's card naming the asset.
//!
//! A picture is a file Python mirrored, drawn from disk through Bevy's asset
//! server — this side opens no socket and asks for nothing. Every picture there
//! is to have is fetched before a list is sent, so an asset with no
//! `thumbnail` is one the bucket publishes no picture for. What drawing costs
//! is the reading: only the cards on screen ask for theirs, and [`RESIDENT`]
//! bounds how many stay uploaded.

use std::collections::{HashMap, HashSet};

use bevy::ecs::system::SystemParam;
use bevy::prelude::*;
use bevy_egui::{EguiContexts, EguiTextureHandle, egui};

/// How many pictures stay uploaded. Several screenfuls, so scrolling back is
/// free, and bounded: a sidecar is 256 px square whatever size it is drawn at,
/// so each one holds a quarter of a megabyte of texture and the browser is long
/// enough to scroll past a thousand.
const RESIDENT: usize = 64;

/// The pictures on hand, by the path Python sent.
#[derive(Default)]
pub struct Thumbnails {
    loaded: HashMap<String, Thumbnail>,
    /// What this frame put on screen, so [`Thumbnails::evict`] knows what the
    /// rest is.
    drawn: HashSet<String>,
}

struct Thumbnail {
    handle: Handle<Image>,
    /// None until the file has finished loading. egui draws whatever texture
    /// id it is handed and has no way to wait for one.
    texture: Option<egui::TextureId>,
}

/// Where a picture is read from: the server that reads the file, and the
/// collection it lands in once it has been read.
#[derive(SystemParam)]
pub struct Files<'w> {
    server: Res<'w, AssetServer>,
    loaded: Res<'w, Assets<Image>>,
}

/// What puts a picture on screen: the files above, and egui's texture table.
///
/// That table is reached through [`EguiContexts`] rather than as a resource of
/// its own, because `EguiContexts` already holds it — asking for both would be
/// two mutable borrows of one resource in a single system.
pub struct Images<'a, 'w, 's> {
    server: &'a AssetServer,
    loaded: &'a Assets<Image>,
    egui: &'a mut EguiContexts<'w, 's>,
}

impl<'a, 'w, 's> Images<'a, 'w, 's> {
    /// The resources behind `files` are taken as plain references, so how long
    /// the system holds them is not tied to how long egui's table is borrowed.
    pub fn new(files: &'a Files<'_>, egui: &'a mut EguiContexts<'w, 's>) -> Self {
        Self { server: &files.server, loaded: &files.loaded, egui }
    }
}

impl Thumbnails {
    /// The picture at `path` as egui draws it: None for an asset with no
    /// picture, and for one still being read. Starting that read is this
    /// call's own doing, so a path that has just arrived is on its way after a
    /// frame.
    pub fn get(&mut self, path: &str, images: &mut Images) -> Option<egui::TextureId> {
        if path.is_empty() {
            return None;
        }
        self.drawn.insert(path.to_owned());
        let thumbnail = self.loaded.entry(path.to_owned()).or_insert_with(|| Thumbnail {
            handle: images.server.load(path.to_owned()),
            texture: None,
        });
        if thumbnail.texture.is_none() && images.loaded.contains(&thumbnail.handle) {
            let handle = EguiTextureHandle::Strong(thumbnail.handle.clone());
            thumbnail.texture = Some(images.egui.add_image(handle));
        }
        thumbnail.texture
    }

    /// Drop the pictures this frame did not draw, once there are more than
    /// [`RESIDENT`] of them. Called once a frame, after everything that draws
    /// one.
    ///
    /// Kept until then rather than freed every frame, so scrolling a little way
    /// back does not read the same files again. A dropped one is loaded afresh
    /// when it is scrolled to, which is the trade for not holding a texture per
    /// prop in the index.
    pub fn evict(&mut self, images: &mut Images) {
        let drawn = std::mem::take(&mut self.drawn);
        if self.loaded.len() <= RESIDENT {
            return;
        }
        self.loaded.retain(|path, thumbnail| {
            if drawn.contains(path) {
                return true;
            }
            // Dropping the handle alone would leave egui an id pointing at an
            // image that is no longer loaded.
            images.egui.remove_image(&thumbnail.handle);
            false
        });
    }
}

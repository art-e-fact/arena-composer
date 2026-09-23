//! The asset browser: everything a spec can name, searchable, one click to add
//! an object.
//!
//! Two kinds of asset are listed together. Arena's registered ones ride in the
//! vocabulary and are always there. NVIDIA's SimReady props ride in
//! [`Form::remote`] and are there once the index has been built, which is what
//! the reindex button asks for. A pick writes the same kind of entry either
//! way: the asset carries the `params` its own entry needs, so nothing here
//! has to know which kind it picked.
//!
//! An asset's picture is a file Python mirrored, drawn from disk through
//! Bevy's asset server — this side opens no socket and asks for nothing. Every
//! picture there is to have is fetched before a list is sent, so a row with no
//! `thumbnail` is a row the bucket publishes no picture for. What scrolling
//! costs is the reading: only the visible rows are uploaded, and [`RESIDENT`]
//! bounds how many stay.

use std::collections::{HashMap, HashSet};

use bevy::ecs::system::SystemParam;
use bevy::prelude::*;
use bevy_egui::{EguiContexts, EguiTextureHandle, egui};

use super::{Form, send};
use crate::spec::{CatalogAsset, Request, Step, Value};

/// How big a picture is drawn, and so how tall every row is — the space is
/// taken whether the row has one or not, so the names line up down the list.
const THUMBNAIL: f32 = 32.0;

/// How many pictures stay uploaded. Several screenfuls, so scrolling back is
/// free, and bounded: a sidecar is 256 px square whatever size it is drawn at,
/// so each one holds a quarter of a megabyte of texture and the list is long
/// enough to scroll past a thousand.
const RESIDENT: usize = 64;

/// What the browser keeps that is not in the spec.
#[derive(Default)]
pub struct Browser {
    /// What the list is filtered to.
    search: String,
    /// The pictures on hand, by the path Python sent.
    thumbnails: HashMap<String, Thumbnail>,
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

pub fn show(ui: &mut egui::Ui, form: &Form, browser: &mut Browser, images: &mut Images) {
    ui.horizontal(|ui| {
        ui.label("assets");
        // Python says whether a crawl is running, the way it says whether a
        // solve is. A prop list would be the wrong thing to read it from: one
        // arrives whenever a picture lands or another process rebuilds the
        // index, neither of which ends the crawl this button started.
        if form.indexing {
            ui.spinner().on_hover_text("listing NVIDIA's SimReady props…");
        } else if ui
            .button("⟳")
            .on_hover_text("list NVIDIA's SimReady props and fetch their pictures — a couple of minutes and ~170 MB")
            .clicked()
        {
            send(Request::Reindex);
        }
        ui.add(
            egui::TextEdit::singleline(&mut browser.search)
                .hint_text("search name and tags")
                .desired_width(f32::INFINITY),
        );
    });

    let terms: Vec<String> = browser.search.split_whitespace().map(str::to_lowercase).collect();
    let found: Vec<&CatalogAsset> = form
        .assets()
        .filter(|asset| asset.role == "object" && matches(asset, &terms))
        .collect();

    // What this frame put on screen, so the rest can be dropped.
    let mut drawn = HashSet::new();
    egui::ScrollArea::vertical()
        .auto_shrink(false)
        // Only the visible rows are drawn, which is what makes listing the
        // whole catalogue — a few hundred assets and a thousand props — cost
        // nothing, and what keeps the uploaded textures down to a screenful.
        // The height given is a row's own; the spacing between rows is added
        // to it here, and adding it twice leaves a list longer than its rows.
        .show_rows(ui, THUMBNAIL, found.len(), |ui, visible| {
            for asset in &found[visible] {
                ui.horizontal(|ui| {
                    thumbnail(ui, asset, browser, images, &mut drawn);
                    if ui.button(&asset.name).on_hover_text(hover(asset)).clicked() {
                        send(Request::Insert {
                            path: vec![Step::from("objects")],
                            value: entry(asset, &form.spec.node_ids()),
                        });
                    }
                });
            }
        });

    browser.evict(&drawn, images);
}

/// Draw the asset's picture, or leave the space it would take.
fn thumbnail(
    ui: &mut egui::Ui,
    asset: &CatalogAsset,
    browser: &mut Browser,
    images: &mut Images,
    drawn: &mut HashSet<String>,
) {
    let size = egui::vec2(THUMBNAIL, THUMBNAIL);
    let blank = |ui: &mut egui::Ui| {
        let _ = ui.allocate_exact_size(size, egui::Sense::hover());
    };

    // Nothing to draw and nothing to wait for: the bucket publishes no picture
    // beside this layer, or the asset composes its USD and has no layer.
    if asset.thumbnail.is_empty() {
        return blank(ui);
    }

    drawn.insert(asset.thumbnail.clone());
    match browser.texture(&asset.thumbnail, images) {
        Some(texture) => {
            ui.add(egui::Image::new(egui::load::SizedTexture::new(texture, size)));
        }
        // Still reading off the disk. One frame of blank, not a missing row.
        None => blank(ui),
    }
}

impl Browser {
    /// Opened with the list already filtered. `ARENA_VIEWER_SEARCH` is what
    /// gives a screenshot a search, since there is nobody to type one.
    pub fn filtered(search: String) -> Self {
        Self { search, ..Self::default() }
    }

    /// The picture at `path` as egui draws it, or None while it is still being
    /// read. Starting that read is this call's own doing, so a path that has
    /// just arrived is on its way after a frame.
    fn texture(&mut self, path: &str, images: &mut Images) -> Option<egui::TextureId> {
        let thumbnail = self.thumbnails.entry(path.to_owned()).or_insert_with(|| Thumbnail {
            handle: images.server.load(path.to_owned()),
            texture: None,
        });
        if thumbnail.texture.is_none() && images.loaded.contains(&thumbnail.handle) {
            let handle = EguiTextureHandle::Strong(thumbnail.handle.clone());
            thumbnail.texture = Some(images.egui.add_image(handle));
        }
        thumbnail.texture
    }

    /// Drop the pictures that are not on screen, once there are more than
    /// [`RESIDENT`] of them.
    ///
    /// Kept until then rather than freed every frame, so scrolling a little way
    /// back does not read the same files again. A dropped one is loaded afresh
    /// when it is scrolled to, which is the trade for not holding a texture per
    /// prop in the index.
    fn evict(&mut self, drawn: &HashSet<String>, images: &mut Images) {
        if self.thumbnails.len() <= RESIDENT {
            return;
        }
        self.thumbnails.retain(|path, thumbnail| {
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

/// Whether every whitespace-separated term appears in the name, a tag or the
/// USD's file name — the same test `arena_composer.catalog.Asset.matches`
/// makes, so the panel and an agent searching find the same things.
fn matches(asset: &CatalogAsset, terms: &[String]) -> bool {
    let file = asset.usd_path.rsplit('/').next().unwrap_or_default();
    let stem = file.rsplit_once('.').map_or(file, |(stem, _)| stem);
    let haystack = format!("{} {} {stem}", asset.name, asset.tags.join(" ")).to_lowercase();
    terms.iter().all(|term| haystack.contains(term))
}

/// What a row says when hovered: the registry name it writes, and for a prop
/// the URL that makes it — every prop writes the same registry name, so the
/// name alone would not say which one this is.
fn hover(asset: &CatalogAsset) -> String {
    match asset.usd_path.as_str() {
        "" => asset.registry_name.clone(),
        url => format!("{}\n{url}", asset.registry_name),
    }
}

/// The object entry a pick writes.
///
/// The id is worked out on the click rather than when the row is drawn, so
/// adding two of the same asset gives two ids and not one. It comes from the
/// asset's name and not its registry name, which for a prop is the one name
/// every prop shares.
///
/// `params` goes in as the asset gave it: empty for a registered asset, and
/// the URL for a prop, which is the whole of what makes that entry build.
fn entry(asset: &CatalogAsset, taken: &[&str]) -> Value {
    Value::map([
        ("id", Value::text(unused(&asset.name.to_lowercase(), taken))),
        ("registry_name", Value::text(&asset.registry_name)),
        ("params", Value::Map(asset.params.clone())),
    ])
}

/// `stem`, or the lowest numbered variant of it that is not already an id.
fn unused(stem: &str, taken: &[&str]) -> String {
    if !taken.contains(&stem) {
        return stem.to_owned();
    }
    (2..)
        .map(|n| format!("{stem}_{n}"))
        .find(|id| !taken.contains(&id.as_str()))
        .expect("an unused id")
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A SimReady prop as the payload carries one: one registry name for all
    /// of them, and the URL that says which.
    fn prop(name: &str, url: &str) -> CatalogAsset {
        CatalogAsset {
            registry_name: "simready_usd_object".into(),
            name: name.into(),
            usd_path: url.into(),
            params: [("usd_path".to_owned(), Value::text(url))].into_iter().collect(),
            ..Default::default()
        }
    }

    /// Adding the same asset twice has to give two objects, not one id twice —
    /// Arena refuses a duplicate, and a refused add looks like a dead button.
    #[test]
    fn an_added_object_does_not_take_an_id_that_is_used() {
        assert_eq!(unused("apple", &["banana"]), "apple");
        assert_eq!(unused("apple", &["apple"]), "apple_2");
        assert_eq!(unused("apple", &["apple", "apple_2"]), "apple_3");
    }

    /// Every term has to land, and a tag counts the same as the name.
    #[test]
    fn a_search_narrows_by_every_term() {
        let asset = CatalogAsset {
            registry_name: "banana_ycb_robolab".into(),
            name: "banana_ycb_robolab".into(),
            tags: vec!["fruit".into(), "small".into()],
            ..Default::default()
        };
        assert!(matches(&asset, &["banana".into(), "fruit".into()]));
        assert!(matches(&asset, &[]));
        assert!(!matches(&asset, &["banana".into(), "large".into()]));
    }

    /// A prop is found by its folder name, by a tag, and by the layer's own
    /// file name — which is the only place a word like `sm_misc` appears.
    #[test]
    fn a_prop_is_found_by_its_layer_name_too() {
        let mut basket = prop(
            "Plastic_Basket_A01",
            "https://bucket/Residential/Kitchen/Baskets/Plastic_Basket_A01/sm_misc_basket_plastic_a01.usd",
        );
        basket.tags = vec!["sim-ready".into(), "kitchen".into()];
        assert!(matches(&basket, &["plastic".into(), "basket".into()]));
        assert!(matches(&basket, &["kitchen".into()]));
        assert!(matches(&basket, &["sm_misc".into()]));
        assert!(!matches(&basket, &["usd".into()]), "the suffix is not a word to search by");
    }

    /// A prop's entry has to carry the URL: `simready_usd_object` is one name
    /// for fifteen hundred assets, and the URL is what picks this one. Its id
    /// comes from the prop's own name for the same reason.
    #[test]
    fn a_picked_prop_writes_the_url_that_makes_it() {
        let basket = prop("Plastic_Basket_A01", "https://bucket/Plastic_Basket_A01/basket.usd");
        let Value::Map(written) = entry(&basket, &["table"]) else {
            panic!("an entry is a mapping");
        };
        assert_eq!(written["id"], Value::text("plastic_basket_a01"));
        assert_eq!(written["registry_name"], Value::text("simready_usd_object"));
        assert_eq!(
            written["params"],
            Value::map([("usd_path", Value::text("https://bucket/Plastic_Basket_A01/basket.usd"))]),
        );
    }

    /// A registered asset writes what it always did: its own name as the id,
    /// and no parameters.
    #[test]
    fn a_picked_registered_asset_writes_no_params() {
        let asset = CatalogAsset {
            registry_name: "banana_ycb_robolab".into(),
            name: "banana_ycb_robolab".into(),
            ..Default::default()
        };
        let Value::Map(written) = entry(&asset, &["banana_ycb_robolab"]) else {
            panic!("an entry is a mapping");
        };
        assert_eq!(written["id"], Value::text("banana_ycb_robolab_2"));
        assert_eq!(written["params"], Value::map([]));
    }
}

//! The asset browser: everything a spec can name, searchable, each asset a
//! card. A click on one adds an object on the anchor; a drag drops it onto
//! something in the layout (see [`super::drop`]).
//!
//! Two kinds of asset are listed together. Arena's registered ones ride in the
//! vocabulary and are always there. NVIDIA's SimReady props ride in
//! [`Form::remote`] and are there once the index has been built, which is what
//! the reindex button asks for. A pick writes the same kind of entry either
//! way: the asset carries the `params` its own entry needs, so nothing here
//! has to know which kind it picked. The card says which it is all the same,
//! since a prop is fetched from the network on its first solve.

use bevy_egui::egui;

use super::thumbnails::{Images, Thumbnails};
use super::{Form, card, send};
use crate::spec::{CatalogAsset, Params, Request, Spec, Step, Value};

/// How tall a card is. Fixed, because the list draws only the cards on screen
/// and has to know where each one falls without drawing the ones before it.
const CARD: f32 = 52.0;

/// The space between a card's border and what is on it.
const PAD: f32 = 5.0;

pub fn show(ui: &mut egui::Ui, form: &Form, search: &mut String, thumbnails: &mut Thumbnails, images: &mut Images) {
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
            egui::TextEdit::singleline(search)
                .hint_text("search name and tags")
                .desired_width(f32::INFINITY),
        );
    });

    let terms: Vec<String> = search.split_whitespace().map(str::to_lowercase).collect();
    let found: Vec<(&CatalogAsset, &str)> = form
        .vocabulary
        .assets
        .iter()
        .map(|asset| (asset, "Arena"))
        .chain(form.remote.iter().map(|asset| (asset, "SimReady")))
        .filter(|(asset, _)| asset.role == "object" && matches(asset, &terms))
        .collect();

    egui::ScrollArea::vertical()
        .auto_shrink(false)
        // Only the visible cards are drawn, which is what makes listing the
        // whole catalogue — a few hundred assets and a thousand props — cost
        // nothing, and what keeps the uploaded pictures down to a screenful.
        // The height given is a card's own; the spacing between cards is added
        // to it here, and adding it twice leaves a list longer than its cards.
        .show_rows(ui, CARD, found.len(), |ui, visible| {
            for &(asset, source) in &found[visible] {
                let in_scene = form.spec.objects.iter().filter(|node| asset.is_named_by(node)).count();
                let picture = thumbnails.get(&asset.thumbnail, images);
                let response = asset_card(ui, asset, &details(asset, source), in_scene, picture);
                // A click adds the asset onto the anchor; a drag carries it
                // out onto the layout, where `super::drop` takes it.
                if response.drag_started() {
                    egui::DragAndDrop::set_payload(ui.ctx(), asset.clone());
                }
                if response.on_hover_text(hover(asset)).clicked() {
                    add(&form.spec, asset, None);
                }
            }
        });
}

/// One asset as a card: its picture, its name, and what else is worth knowing
/// before picking it — where it comes from, how it simulates, what it is
/// tagged, and how many of it the scene holds already.
///
/// The whole card is one widget sensing both clicks and drags, so it can be
/// picked up anywhere on it. Everything on it is painted rather than laid out
/// as widgets of its own, which would take the pointer from the card under
/// them.
fn asset_card(
    ui: &mut egui::Ui,
    asset: &CatalogAsset,
    details: &str,
    in_scene: usize,
    picture: Option<egui::TextureId>,
) -> egui::Response {
    let size = egui::vec2(ui.available_width(), CARD);
    let (rect, response) = ui.allocate_exact_size(size, egui::Sense::click_and_drag());
    if !ui.is_rect_visible(rect) {
        return response;
    }
    let visuals = ui.visuals();
    let (fill, stroke) = match response.hovered() || response.dragged() {
        true => (visuals.widgets.hovered.weak_bg_fill, visuals.widgets.hovered.bg_stroke),
        false => (card::fill(visuals), visuals.widgets.noninteractive.bg_stroke),
    };
    let radius = visuals.widgets.noninteractive.corner_radius;
    ui.painter().rect(rect, radius, fill, stroke, egui::StrokeKind::Inside);

    // The picture's square keeps its place whether or not there is a picture,
    // so the names line up down the list.
    let inner = rect.shrink(PAD);
    let square = egui::Rect::from_min_size(inner.min, egui::Vec2::splat(inner.height()));
    match picture {
        Some(texture) => egui::Image::new(egui::load::SizedTexture::new(texture, square.size()))
            .corner_radius(radius)
            .paint_at(ui, square),
        None => {
            ui.painter().rect_filled(square, radius, visuals.extreme_bg_color);
        }
    }

    let text = egui::Rect::from_min_max(egui::pos2(square.right() + PAD * 1.5, inner.top()), inner.max);
    let line = |text: egui::RichText, width: f32| {
        egui::WidgetText::from(text).into_galley(ui, Some(egui::TextWrapMode::Truncate), width, egui::TextStyle::Body)
    };
    let mut name_width = text.width();
    if in_scene > 0 {
        let badge = egui::RichText::new(format!("{in_scene} in scene")).small();
        let badge = line(badge.color(visuals.selection.stroke.color), text.width());
        name_width -= badge.size().x + PAD;
        ui.painter().galley(egui::pos2(text.right() - badge.size().x, text.top()), badge, visuals.text_color());
    }
    let tags = asset.tags.join(" · ");
    let lines = [
        line(egui::RichText::new(&asset.name).strong(), name_width),
        line(egui::RichText::new(details).small().weak(), text.width()),
        line(egui::RichText::new(tags).small().weak(), text.width()),
    ];
    let mut top = text.top();
    for galley in lines {
        let height = galley.size().y;
        ui.painter().galley(egui::pos2(text.left(), top), galley, visuals.text_color());
        top += height;
    }
    response
}

/// Where an asset comes from, and the physics it simulates with where it says.
fn details(asset: &CatalogAsset, source: &str) -> String {
    match &asset.object_type {
        Some(object_type) => format!("{source} · {object_type}"),
        None => source.to_owned(),
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
pub(super) fn hover(asset: &CatalogAsset) -> String {
    match asset.usd_path.as_str() {
        "" => asset.registry_name.clone(),
        url => format!("{}\n{url}", asset.registry_name),
    }
}

/// Add `asset` as a new object, standing on `onto` or else on the anchor.
pub(super) fn add(spec: &Spec, asset: &CatalogAsset, onto: Option<&str>) {
    for request in additions(spec, asset, onto) {
        send(request);
    }
}

/// What adding `asset` writes: the object, and the `on` that places it.
///
/// Arena places only the objects that have a relation. One without stays at
/// its default pose, and the solver steers everything else around it — so an
/// add carries an `on` wherever there is something to stand on: `onto`, else
/// the anchor. With neither, the object goes in alone.
///
/// The id is worked out on the click rather than when the row is drawn, so
/// adding two of the same asset gives two ids and not one. It comes from the
/// asset's name and not its registry name, which for a prop is the one name
/// every prop shares. Python applies the two writes in order, so the `on` names
/// an id the first has already written.
// ponytail: two writes, so a refused object gets its `on` refused as well, and
// that second error replaces the first. One batched request if that bites.
fn additions(spec: &Spec, asset: &CatalogAsset, onto: Option<&str>) -> Vec<Request> {
    let id = unused(&asset.name.to_lowercase(), &spec.node_ids());
    let mut requests = vec![Request::Insert {
        path: vec![Step::from("objects")],
        value: entry(asset, &id),
    }];
    if let Some(reference) = onto.or_else(|| anchor(spec)) {
        requests.push(Request::Insert {
            path: vec![Step::from("relations")],
            value: Value::map([
                ("kind", Value::text("on")),
                ("subject", Value::text(&id)),
                ("reference", Value::text(reference)),
                ("params", Value::Map(on_params(spec, reference))),
            ]),
        });
    }
    requests
}

/// The node the solver holds still and places everything else against.
pub(super) fn anchor(spec: &Spec) -> Option<&str> {
    let relation = spec.relations.iter().find(|relation| relation.kind == "is_anchor")?;
    Some(&relation.subject)
}

/// The parameters an added `on` takes from what it stands on.
///
/// A background or an object reference is a surface — a table, a counter —
/// and keeps the kind's defaults: contained, and off the rim. An object is
/// about the size of what gets put on it, so containment inside its inset top
/// rarely fits, and it asks for the footprints to overlap instead.
pub(super) fn on_params(spec: &Spec, reference: &str) -> Params {
    let object = spec.objects.iter().any(|node| node.id == reference)
        || spec.object_sets.iter().any(|set| set.id == reference);
    match object {
        true => Params::from([("overlap".to_owned(), Value::Bool(true))]),
        false => Params::new(),
    }
}

/// The object entry an add writes, under `id`.
///
/// `params` goes in as the asset gave it: empty for a registered asset, and
/// the URL for a prop, which is the whole of what makes that entry build.
fn entry(asset: &CatalogAsset, id: &str) -> Value {
    Value::map([
        ("id", Value::text(id)),
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
    use crate::spec::Relation;
    use std::collections::HashMap;

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

    fn banana() -> CatalogAsset {
        CatalogAsset {
            registry_name: "banana_ycb_robolab".into(),
            name: "banana_ycb_robolab".into(),
            ..Default::default()
        }
    }

    /// A table as the background, a bowl on it, and the table anchored unless
    /// asked otherwise.
    fn spec(anchored: bool) -> Spec {
        let node = |id: &str| crate::spec::Node { id: id.into(), ..Default::default() };
        let mut spec = Spec {
            background: node("table"),
            objects: vec![node("bowl")],
            ..Default::default()
        };
        if anchored {
            spec.relations.push(Relation { kind: "is_anchor".into(), subject: "table".into(), ..Default::default() });
        }
        spec
    }

    /// Each entry the requests insert, and where.
    fn inserted(requests: &[Request]) -> Vec<(String, HashMap<String, Value>)> {
        requests
            .iter()
            .map(|request| match request {
                Request::Insert { path, value: Value::Map(entry) } => match path.as_slice() {
                    [Step::Key(key)] => (key.clone(), entry.clone()),
                    other => panic!("inserted at {other:?}"),
                },
                other => panic!("not an insert of an entry: {other:?}"),
            })
            .collect()
    }

    /// A prop's entry has to carry the URL: `simready_usd_object` is one name
    /// for fifteen hundred assets, and the URL is what picks this one. Its id
    /// comes from the prop's own name for the same reason.
    #[test]
    fn a_picked_prop_writes_the_url_that_makes_it() {
        let basket = prop("Plastic_Basket_A01", "https://bucket/Plastic_Basket_A01/basket.usd");
        let written = inserted(&additions(&spec(true), &basket, None));
        let (_, object) = &written[0];
        assert_eq!(object["id"], Value::text("plastic_basket_a01"));
        assert_eq!(object["registry_name"], Value::text("simready_usd_object"));
        assert_eq!(
            object["params"],
            Value::map([("usd_path", Value::text("https://bucket/Plastic_Basket_A01/basket.usd"))]),
        );
    }

    /// A registered asset writes what it always did: its own name as the id,
    /// and no parameters.
    #[test]
    fn a_picked_registered_asset_writes_no_params() {
        let mut spec = spec(true);
        spec.objects.push(crate::spec::Node { id: "banana_ycb_robolab".into(), ..Default::default() });
        let written = inserted(&additions(&spec, &banana(), None));
        let (_, object) = &written[0];
        assert_eq!(object["id"], Value::text("banana_ycb_robolab_2"));
        assert_eq!(object["params"], Value::map([]));
    }

    /// A click stands the new object on the anchor, contained like anything
    /// else on a table — an object with no relation is never placed at all.
    #[test]
    fn a_click_puts_the_object_on_the_anchor() {
        let written = inserted(&additions(&spec(true), &banana(), None));
        let keys: Vec<&str> = written.iter().map(|(key, _)| key.as_str()).collect();
        assert_eq!(keys, ["objects", "relations"], "the object has to exist before the `on` names it");
        let (_, on) = &written[1];
        assert_eq!(on["kind"], Value::text("on"));
        assert_eq!(on["subject"], Value::text("banana_ycb_robolab"));
        assert_eq!(on["reference"], Value::text("table"));
        assert_eq!(on["params"], Value::map([]), "a surface keeps the kind's defaults");
    }

    /// Dropped onto an object, the `on` asks for overlap: containment inside a
    /// bowl's inset top rarely fits a thing the size of a bowl's contents.
    #[test]
    fn a_drop_onto_an_object_asks_for_overlap() {
        let written = inserted(&additions(&spec(true), &banana(), Some("bowl")));
        let (_, on) = &written[1];
        assert_eq!(on["reference"], Value::text("bowl"));
        assert_eq!(on["params"], Value::map([("overlap", Value::Bool(true))]));
    }

    /// With no anchor and nothing named, there is nothing to stand on, and the
    /// object goes in alone as it always did.
    #[test]
    fn without_an_anchor_a_click_adds_the_object_alone() {
        let written = inserted(&additions(&spec(false), &banana(), None));
        assert_eq!(written.len(), 1, "{written:?}");
    }
}

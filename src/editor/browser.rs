//! The asset browser, a tab of the dock under the 3D view (see [`super::dock`])
//! after Isaac Sim's content browser: a folder tree, a grid of tiles, and the
//! details of the tile picked.
//!
//! A click picks a tile, and the details beside the grid say what it is and
//! offer what can be done with it: add an object onto the anchor or onto any
//! node, or make a background or a robot the scene's own. A double click on an
//! object does what the details' add button does, and a drag drops it onto
//! something in the layout (see [`super::drop`]). A background or a robot is
//! only ever set from the details: either replaces a whole part of the scene,
//! which one stray click should not do.
//!
//! Two kinds of asset are listed together. Arena's registered ones ride in the
//! vocabulary and are always there. NVIDIA's SimReady props ride in
//! [`Form::remote`] and are there once the index has been built, which is what
//! the reindex button asks for. A pick writes the same kind of entry either
//! way: the asset carries the `params` its own entry needs, so nothing here
//! has to know which kind it picked. The details say which it is all the same,
//! since a prop is fetched from the network on its first solve, and give the
//! credit a SimReady Central prop's license asks for.

use std::collections::BTreeMap;

use bevy_egui::egui;

use super::controls::row;
use super::thumbnails::Pictures;
use super::{Form, card, send};
use crate::spec::{CatalogAsset, Params, Request, Spec, Step, Value};

/// How wide the tree and the details open.
const TREE: f32 = 190.0;
const DETAILS: f32 = 270.0;

/// The space between a tile's border and what is on it.
const PAD: f32 = 5.0;

/// How large the details draw the picked asset's picture.
const PICTURE: f32 = 96.0;

/// What the browser remembers between frames. None of it is in the spec.
pub struct Browser {
    /// What the grid is filtered to.
    search: String,
    /// The folder the grid shows, with everything under it; empty for all.
    folder: String,
    /// The asset picked, by its registry name and its USD — every prop shares
    /// the one registry name, and the URL is what tells two apart.
    picked: Option<(String, String)>,
    /// How wide a tile is drawn.
    tile: f32,
    /// What the add button stands an object on, by id; None for the anchor.
    onto: Option<String>,
    /// Pick the first match as soon as there is one.
    pick_first: bool,
}

/// `ARENA_VIEWER_SEARCH=basket` opens the browser filtered, with the first
/// match picked, since a screenshot has nobody to type or click for it.
impl Default for Browser {
    fn default() -> Self {
        let search = std::env::var("ARENA_VIEWER_SEARCH").unwrap_or_default();
        Self {
            pick_first: !search.is_empty(),
            search,
            folder: String::new(),
            picked: None,
            tile: 96.0,
            onto: None,
        }
    }
}

/// One asset as the browser lists it.
pub struct Listed<'a> {
    asset: &'a CatalogAsset,
    /// Where it comes from: `Arena` or `SimReady`.
    source: &'static str,
    /// Where the tree files it, e.g. `SimReady/Residential/Kitchen/Baskets`.
    folder: String,
}

/// The rest of the dock's header while the browser is its tab: how many
/// assets there are, the reindex button, the search and the tile size.
pub fn header(ui: &mut egui::Ui, form: &Form, state: &mut Browser, total: usize) {
    ui.weak(total.to_string());
    // Python says whether a crawl is running, the way it says whether a
    // solve is. A prop list would be the wrong thing to read it from: one
    // arrives whenever a picture lands or another process rebuilds the
    // index, neither of which ends the crawl this button started.
    if form.indexing {
        ui.spinner().on_hover_text("listing NVIDIA's SimReady props…");
    } else if ui
        .button("⟳")
        .on_hover_text(
            "list NVIDIA's SimReady props, from Isaac Sim's bucket and SimReady Central, and fetch their \
             pictures — a couple of minutes and ~830 MB",
        )
        .clicked()
    {
        send(Request::Reindex);
    }
    // Right to left, so the slider keeps the far edge and the search takes
    // what is left between.
    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
        ui.add(egui::Slider::new(&mut state.tile, 64.0..=192.0).show_value(false)).on_hover_text("tile size");
        ui.add(
            egui::TextEdit::singleline(&mut state.search)
                .hint_text("search name and tags")
                .desired_width(f32::INFINITY),
        );
    });
}

/// The tree on the left, the details on the right, and the grid between.
pub fn body(ui: &mut egui::Ui, form: &Form, state: &mut Browser, listed: &[Listed], pictures: &mut Pictures) {
    let terms: Vec<String> = state.search.split_whitespace().map(str::to_lowercase).collect();
    let matching: Vec<bool> = listed.iter().map(|item| matches(item.asset, &terms)).collect();
    let tally = tally(listed, &matching);

    egui::Panel::left("asset tree")
        .resizable(true)
        .default_size(TREE)
        .min_size(120.0)
        .show(ui, |ui| tree(ui, form, &tally, &mut state.folder));
    egui::Panel::right("asset details")
        .resizable(true)
        .default_size(DETAILS)
        .min_size(200.0)
        .show(ui, |ui| details(ui, form, state, listed, pictures));

    let shown: Vec<&Listed> = listed
        .iter()
        .zip(&matching)
        .filter(|(item, matched)| **matched && within(&item.folder, &state.folder))
        .map(|(item, _)| item)
        .collect();
    if state.pick_first
        && let Some(first) = shown.first()
    {
        state.picked = Some(key(first.asset));
        state.pick_first = false;
    }
    grid(ui, form, state, &shown, pictures);
}

/// Every asset the browser offers, filed where the tree shows it. Objects
/// first, since adding one is what the browser is mostly for: Arena's, then
/// SimReady's, then the backgrounds and the robots.
pub fn listed(form: &Form) -> Vec<Listed<'_>> {
    let arena = |role: &'static str, folder: &'static str| {
        form.vocabulary.assets.iter().filter(move |asset| asset.role == role).map(move |asset| Listed {
            asset,
            source: "Arena",
            folder: format!("Arena/{folder}"),
        })
    };
    let simready = form.remote.iter().map(|asset| Listed {
        asset,
        source: "SimReady",
        folder: match asset.category.as_str() {
            "" => "SimReady".to_owned(),
            category => format!("SimReady/{category}"),
        },
    });
    arena("object", "Objects")
        .chain(simready)
        .chain(arena("background", "Backgrounds"))
        .chain(arena("embodiment", "Robots"))
        .collect()
}

/// How many assets under each folder match the search, for every folder that
/// holds an asset at all. A folder whose assets are all filtered out stays at
/// nought rather than leaving, so the tree keeps its shape under a search.
fn tally<'a>(listed: &'a [Listed], matching: &[bool]) -> BTreeMap<&'a str, usize> {
    let mut tally = BTreeMap::new();
    for (item, &matched) in listed.iter().zip(matching) {
        let folder = item.folder.as_str();
        let parents = folder.match_indices('/').map(|(end, _)| &folder[..end]);
        for path in parents.chain([folder]) {
            *tally.entry(path).or_default() += usize::from(matched);
        }
    }
    tally
}

/// The folders directly under `parent`, or the roots under "".
fn children<'a>(tally: &BTreeMap<&'a str, usize>, parent: &str) -> Vec<&'a str> {
    tally
        .keys()
        .copied()
        .filter(|path| match parent {
            "" => !path.contains('/'),
            parent => path
                .strip_prefix(parent)
                .and_then(|rest| rest.strip_prefix('/'))
                .is_some_and(|rest| !rest.contains('/')),
        })
        .collect()
}

/// Whether `folder` is `picked` or somewhere under it. Everything is under "".
fn within(folder: &str, picked: &str) -> bool {
    picked.is_empty() || folder.strip_prefix(picked).is_some_and(|rest| rest.is_empty() || rest.starts_with('/'))
}

/// The folder tree, each folder with how many of its assets match.
fn tree(ui: &mut egui::Ui, form: &Form, tally: &BTreeMap<&str, usize>, folder: &mut String) {
    egui::ScrollArea::vertical().auto_shrink(false).show(ui, |ui| {
        let all: usize = children(tally, "").iter().map(|root| tally[root]).sum();
        ui.horizontal(|ui| {
            ui.add_space(ui.spacing().icon_width + ui.spacing().icon_spacing);
            leaf(ui, "all", all, folder.is_empty(), || folder.clear());
        });
        for root in children(tally, "") {
            branch(ui, tally, root, folder);
        }
        if form.remote.is_empty() {
            ui.weak(match form.indexing {
                true => "listing SimReady props…",
                false => "SimReady props: ⟳ lists them",
            });
        }
    });
}

/// One folder, and the folders under it where it has any. The roots open
/// folded out, and everything under them folded in.
fn branch(ui: &mut egui::Ui, tally: &BTreeMap<&str, usize>, path: &str, folder: &mut String) {
    let name = path.rsplit('/').next().unwrap_or(path);
    let under = children(tally, path);
    if under.is_empty() {
        // Indented past where a fold arrow would be, so a folder with nothing
        // under it lines up with its neighbours that have one.
        ui.horizontal(|ui| {
            ui.add_space(ui.spacing().icon_width + ui.spacing().icon_spacing);
            leaf(ui, name, tally[path], folder == path, || *folder = path.to_owned());
        });
        return;
    }
    let id = ui.make_persistent_id(("asset folder", path));
    egui::collapsing_header::CollapsingState::load_with_default_open(ui.ctx(), id, !path.contains('/'))
        .show_header(ui, |ui| leaf(ui, name, tally[path], folder == path, || *folder = path.to_owned()))
        .body(|ui| {
            for child in under {
                branch(ui, tally, child, folder);
            }
        });
}

/// A folder's name and count, picked by a click.
fn leaf(ui: &mut egui::Ui, name: &str, count: usize, picked: bool, pick: impl FnOnce()) {
    let glyph = if picked { "🗁" } else { "🗀" };
    let text = egui::RichText::new(format!("{glyph} {name}  {count}"));
    let text = if count == 0 { text.weak() } else { text };
    if ui.selectable_label(picked, text).clicked() {
        pick();
    }
}

/// The assets in the picked folder that match the search, a tile each.
fn grid(ui: &mut egui::Ui, form: &Form, state: &mut Browser, shown: &[&Listed], pictures: &mut Pictures) {
    if shown.is_empty() {
        ui.add_space(PAD);
        match state.folder.is_empty() {
            true => ui.weak("nothing matches the search"),
            false => ui.weak(format!("nothing in {} matches the search", state.folder)),
        };
        if !state.folder.is_empty() && ui.button("search every folder").clicked() {
            state.folder.clear();
        }
        return;
    }
    let size = egui::vec2(state.tile, state.tile + 2.0 * ui.text_style_height(&egui::TextStyle::Body) + PAD);
    let gap = ui.spacing().item_spacing.x;
    let columns = (((ui.available_width() + gap) / (size.x + gap)).floor() as usize).max(1);
    egui::ScrollArea::vertical()
        .auto_shrink(false)
        // Only the rows on screen are drawn, which is what makes listing the
        // whole catalogue — a few hundred assets and fifteen hundred props —
        // cost nothing, and what keeps the uploaded pictures to a screenful.
        // The height given is a row's own; the spacing between rows is added
        // to it here, and adding it twice leaves a list longer than its rows.
        .show_rows(ui, size.y, shown.len().div_ceil(columns), |ui, rows| {
            for row in rows {
                ui.horizontal(|ui| {
                    for item in shown.iter().skip(row * columns).take(columns) {
                        let asset = item.asset;
                        let picture = pictures(&asset.thumbnail);
                        let picked = state.picked == Some(key(asset));
                        let response = tile(ui, asset, in_scene(&form.spec, asset).len(), picture, picked, size);
                        // A drag carries an object out onto the layout, where
                        // `super::drop` takes it.
                        if response.drag_started() {
                            egui::DragAndDrop::set_payload(ui.ctx(), asset.clone());
                        }
                        let response = response.on_hover_text(&asset.name);
                        if response.clicked() {
                            state.picked = Some(key(asset));
                        }
                        if response.double_clicked() && asset.role == "object" {
                            add(&form.spec, asset, state.onto.as_deref());
                        }
                    }
                });
            }
        });
}

/// One asset as a tile: its picture, its name under it, and how many of it the
/// scene holds already.
///
/// The whole tile is one widget, so it can be picked up anywhere on it, and
/// everything on it is painted: a widget laid over it would take the pointer.
/// Only an object senses a drag, since only an object can be dropped.
fn tile(
    ui: &mut egui::Ui,
    asset: &CatalogAsset,
    in_scene: usize,
    picture: Option<egui::TextureId>,
    picked: bool,
    size: egui::Vec2,
) -> egui::Response {
    let sense = match asset.role.as_str() {
        "object" => egui::Sense::click_and_drag(),
        _ => egui::Sense::click(),
    };
    let (rect, response) = ui.allocate_exact_size(size, sense);
    if !ui.is_rect_visible(rect) {
        return response;
    }
    let visuals = ui.visuals();
    let (fill, stroke) = if picked {
        (visuals.selection.bg_fill.gamma_multiply(0.35), visuals.selection.stroke)
    } else if response.hovered() || response.dragged() {
        (visuals.widgets.hovered.weak_bg_fill, visuals.widgets.hovered.bg_stroke)
    } else {
        (card::fill(visuals), visuals.widgets.noninteractive.bg_stroke)
    };
    let radius = visuals.widgets.noninteractive.corner_radius;
    ui.painter().rect(rect, radius, fill, stroke, egui::StrokeKind::Inside);

    let inner = rect.shrink(PAD);
    let square = egui::Rect::from_min_size(inner.min, egui::Vec2::splat(inner.width()));
    picture_or_glyph(ui, asset, picture, square);

    if in_scene > 0 {
        let badge = egui::WidgetText::from(egui::RichText::new(format!("{in_scene} in scene")).small()).into_galley(
            ui,
            Some(egui::TextWrapMode::Truncate),
            square.width(),
            egui::TextStyle::Small,
        );
        let at = egui::Rect::from_min_size(
            egui::pos2(square.right() - badge.size().x - 2.0 * PAD, square.top() + PAD),
            badge.size() + egui::vec2(PAD, 2.0),
        );
        ui.painter().rect_filled(at, radius, visuals.selection.bg_fill);
        ui.painter().galley(at.min + egui::vec2(PAD / 2.0, 1.0), badge, visuals.selection.stroke.color);
    }

    // Two lines, centred, and broken anywhere: a name is one word joined by
    // underscores, with nowhere else to break it.
    let font = egui::TextStyle::Body.resolve(ui.style());
    let mut name = egui::text::LayoutJob::simple(asset.name.clone(), font, visuals.text_color(), inner.width());
    name.wrap.max_rows = 2;
    name.wrap.break_anywhere = true;
    name.halign = egui::Align::Center;
    let name = ui.painter().layout_job(name);
    ui.painter().galley(egui::pos2(inner.center().x, square.bottom() + PAD / 2.0), name, visuals.text_color());
    response
}

/// An asset's picture in `square`, or where it has none, the glyph its kind of
/// card wears in the panel.
fn picture_or_glyph(ui: &egui::Ui, asset: &CatalogAsset, picture: Option<egui::TextureId>, square: egui::Rect) {
    let radius = ui.visuals().widgets.noninteractive.corner_radius;
    match picture {
        Some(texture) => egui::Image::new(egui::load::SizedTexture::new(texture, square.size()))
            .corner_radius(radius)
            .paint_at(ui, square),
        None => {
            ui.painter().rect_filled(square, radius, ui.visuals().extreme_bg_color);
            let glyph = match asset.role.as_str() {
                "embodiment" => card::EMBODIMENT,
                "background" => card::BACKGROUND,
                _ => card::OBJECT,
            };
            let font = egui::FontId::proportional(square.height() / 3.0);
            let color = ui.visuals().weak_text_color();
            ui.painter().text(square.center(), egui::Align2::CENTER_CENTER, glyph, font, color);
        }
    }
}

/// What the picked asset is, and what can be done with it.
fn details(ui: &mut egui::Ui, form: &Form, state: &mut Browser, listed: &[Listed], pictures: &mut Pictures) {
    let Some(item) = listed.iter().find(|item| state.picked == Some(key(item.asset))) else {
        ui.add_space(PAD);
        ui.weak("Click an asset to see what it is. Double-click an object, or drag it onto the layout, to add it.");
        return;
    };
    let asset = item.asset;
    egui::ScrollArea::vertical().auto_shrink(false).show(ui, |ui| {
        // The picture beside the name rather than over it, so what the asset
        // is and what can be done with it fit a dock of the height it opens at.
        ui.horizontal(|ui| {
            let (square, _) = ui.allocate_exact_size(egui::Vec2::splat(PICTURE), egui::Sense::hover());
            picture_or_glyph(ui, asset, pictures(&asset.thumbnail), square);
            ui.vertical(|ui| {
                ui.add(egui::Label::new(egui::RichText::new(&asset.name).strong()).wrap());
                ui.weak(match item.source {
                    "SimReady" => "NVIDIA SimReady prop, downloaded on its first solve",
                    _ => "registered with Arena",
                });
                action(ui, form, asset, &mut state.onto);
            });
        });
        ui.add_space(PAD);
        row(ui, "registry name", None, |ui| {
            ui.add(egui::Label::new(egui::RichText::new(&asset.registry_name).monospace()).truncate());
        });
        if let Some(object_type) = &asset.object_type {
            row(ui, "physics", None, |ui| {
                ui.label(object_type);
            });
        }
        // Wrapped: a panel widens to fit what it cannot wrap, and a SimReady
        // prop's folder is five or six steps, which took the grid's room.
        row(ui, "folder", None, |ui| {
            ui.horizontal_wrapped(|ui| {
                // Each step is a way back up the tree.
                let folder = item.folder.as_str();
                let steps = folder.match_indices('/').map(|(end, _)| end).chain([folder.len()]);
                let mut start = 0;
                for end in steps {
                    if start > 0 {
                        ui.weak("›");
                    }
                    let step = egui::Button::new(&folder[start..end]).frame(false).small();
                    if ui.add(step).on_hover_text("show this folder").clicked() {
                        state.folder = folder[..end].to_owned();
                    }
                    start = end + 1;
                }
            });
        });
        if !asset.usd_path.is_empty() {
            row(ui, "usd", None, |ui| {
                if ui.small_button("🗐").on_hover_text("copy the path").clicked() {
                    ui.ctx().copy_text(asset.usd_path.clone());
                }
                let file = asset.usd_path.rsplit('/').next().unwrap_or_default();
                ui.add(egui::Label::new(file).truncate()).on_hover_text(&asset.usd_path);
            });
        }
        // Wrapped, as the folder is, for the dataset's URL in it.
        if !asset.credit.is_empty() {
            row(ui, "credit", None, |ui| {
                ui.horizontal_wrapped(|ui| ui.add(egui::Label::new(&asset.credit).wrap()));
            });
        }
        let nodes = in_scene(&form.spec, asset);
        row(ui, "in scene", None, |ui| match nodes.is_empty() {
            true => {
                ui.weak("none");
            }
            false => {
                ui.add(egui::Label::new(nodes.join(", ")).truncate());
            }
        });
        if !asset.tags.is_empty() {
            ui.add_space(PAD);
            ui.horizontal_wrapped(|ui| {
                for tag in &asset.tags {
                    if ui.small_button(tag).on_hover_text("search by this tag").clicked() {
                        state.search = tag.clone();
                    }
                }
            });
        }
    });
}

/// What the picked asset can be made into: a new object, standing on `onto`,
/// or the scene's background or robot.
fn action(ui: &mut egui::Ui, form: &Form, asset: &CatalogAsset, onto: &mut Option<String>) {
    let spec = &form.spec;
    let replaces = match asset.role.as_str() {
        "background" => Some(("background", &spec.background)),
        "embodiment" => Some(("embodiment", &spec.embodiment)),
        _ => None,
    };
    if let Some((key, node)) = replaces {
        // A pick sets the registry name alone, as the card's own menu does.
        let label = if key == "background" { "use as the background" } else { "use as the robot" };
        let current = asset.is_named_by(node);
        let button = ui.add_enabled(!current, egui::Button::new(label));
        if current {
            button.on_disabled_hover_text("the scene's already");
        } else if button.clicked() {
            send(Request::Set {
                path: vec![Step::from(key), Step::from("registry_name")],
                value: Value::text(&asset.registry_name),
            });
        }
        return;
    }
    ui.horizontal(|ui| {
        if ui.button("✚ add").on_hover_text("add it to the scene, standing on this").clicked() {
            add(spec, asset, onto.as_deref());
        }
        ui.label("on");
        onto_menu(ui, spec, onto);
    });
}

/// Where the add button stands a new object: the anchor, or any node but the
/// robot. A pick naming a node since removed falls back to the anchor.
fn onto_menu(ui: &mut egui::Ui, spec: &Spec, onto: &mut Option<String>) {
    let anchor = anchor(spec);
    let targets: Vec<&str> =
        spec.node_ids().into_iter().filter(|id| *id != spec.embodiment.id && Some(*id) != anchor).collect();
    if onto.as_deref().is_some_and(|id| !targets.contains(&id)) {
        *onto = None;
    }
    let anchored = anchor.map_or("nothing".to_owned(), |anchor| format!("{anchor} (anchor)"));
    egui::ComboBox::from_id_salt("add onto")
        .selected_text(onto.clone().unwrap_or_else(|| anchored.clone()))
        .width(ui.available_width())
        .show_ui(ui, |ui| {
            ui.selectable_value(onto, None, anchored);
            for id in targets {
                ui.selectable_value(onto, Some(id.to_owned()), id);
            }
        });
}

/// What tells an asset apart from every other: its registry name, and its
/// USD, which is all that tells one prop from the next.
fn key(asset: &CatalogAsset) -> (String, String) {
    (asset.registry_name.clone(), asset.usd_path.clone())
}

/// The ids of the scene's nodes naming `asset`.
fn in_scene<'a>(spec: &'a Spec, asset: &CatalogAsset) -> Vec<&'a str> {
    [&spec.embodiment, &spec.background]
        .into_iter()
        .chain(&spec.objects)
        .filter(|node| asset.is_named_by(node))
        .map(|node| node.id.as_str())
        .collect()
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
    use crate::editor::tests::SENT;
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

    /// A registered object and a registered background, the SimReady prop
    /// from the kitchen, and a scene with the background anchored.
    fn catalogue() -> Form {
        let mut form = Form::default();
        let registered = |name: &str, role: &str| CatalogAsset {
            registry_name: name.into(),
            name: name.into(),
            role: role.into(),
            ..Default::default()
        };
        form.vocabulary.assets =
            vec![registered("maple_table_robolab", "background"), registered("banana_ycb_robolab", "object")];
        let mut basket = prop("Plastic_Basket_A01", "https://bucket/Plastic_Basket_A01/basket.usd");
        basket.role = "object".into();
        basket.category = "Residential/Kitchen/Baskets".into();
        form.remote = vec![basket];
        form.spec = spec(true);
        form
    }

    /// Objects come first, Arena's then SimReady's, and every asset is filed
    /// where the tree shows it: Arena's by role, a prop by its bucket folders.
    #[test]
    fn the_tree_files_arena_by_role_and_simready_by_folder() {
        let form = catalogue();
        let listed = listed(&form);
        let folders: Vec<&str> = listed.iter().map(|item| item.folder.as_str()).collect();
        assert_eq!(folders, ["Arena/Objects", "SimReady/Residential/Kitchen/Baskets", "Arena/Backgrounds"]);

        // A search for the basket leaves every folder in the tree, at nought.
        let matching = [false, true, false];
        let tally = tally(&listed, &matching);
        assert_eq!(children(&tally, ""), ["Arena", "SimReady"]);
        assert_eq!(children(&tally, "Arena"), ["Arena/Backgrounds", "Arena/Objects"]);
        assert_eq!(children(&tally, "SimReady/Residential"), ["SimReady/Residential/Kitchen"]);
        assert_eq!((tally["Arena"], tally["SimReady"], tally["SimReady/Residential/Kitchen/Baskets"]), (0, 1, 1));

        assert!(within("SimReady/Residential/Kitchen", "SimReady/Residential"));
        assert!(within("SimReady/Residential", "SimReady/Residential"));
        assert!(!within("SimReady/ResidentialExtra", "SimReady/Residential"), "a name sharing a prefix");
        assert!(within("Arena/Objects", ""), "everything is under all");
    }

    /// A prop's folder and credit wrap in the details rather than widening
    /// them: a panel grows to fit what it cannot wrap, and what it takes is the
    /// grid's.
    #[test]
    fn a_long_folder_does_not_widen_the_details() {
        let width = |category: &str, credit: &str| {
            let mut form = catalogue();
            form.remote[0].category = category.into();
            form.remote[0].credit = credit.into();
            let mut state = Browser { picked: Some(key(&form.remote[0])), ..Browser::default() };
            let ctx = egui::Context::default();
            for time in 0..3 {
                let input = egui::RawInput {
                    time: Some(time.into()),
                    screen_rect: Some(egui::Rect::from_min_size(egui::Pos2::ZERO, egui::vec2(900.0, 400.0))),
                    ..Default::default()
                };
                ctx.run_ui(input, |ui| body(ui, &form, &mut state, &listed(&form), &mut |_| None))
                    .drop_without_applying_deltas();
            }
            egui::PanelState::load(&ctx, egui::Id::new("asset details")).expect("the details are drawn").size().x
        };
        assert_eq!(width("Residential/Furnishing/Kitchen/Kitchenware/Cleaning_Supplies", ""), width("Residential", ""));
        let credit = "NVIDIA SimReady Central (https://huggingface.co/datasets/nvidia/simready-assets), CC-BY-4.0";
        assert_eq!(width("Central/paint", credit), width("Central/paint", ""), "the credit widened them");
    }

    /// The grid alone, a frame at a time, with the input a person would give
    /// it, as `controls`' tests drive their controls.
    struct Grid {
        ctx: egui::Context,
        state: Browser,
        form: Form,
        time: f64,
    }

    impl Grid {
        fn new() -> Self {
            SENT.with_borrow_mut(Vec::clear);
            Self { ctx: egui::Context::default(), state: Browser::default(), form: catalogue(), time: 0.0 }
        }

        fn frame(&mut self, events: Vec<egui::Event>) {
            self.time += 1.0;
            let input = egui::RawInput {
                events,
                time: Some(self.time),
                screen_rect: Some(egui::Rect::from_min_size(egui::Pos2::ZERO, egui::vec2(600.0, 400.0))),
                ..Default::default()
            };
            let (form, state) = (&self.form, &mut self.state);
            self.ctx
                .run_ui(input, |ui| {
                    let listed = listed(form);
                    let shown: Vec<&Listed> = listed.iter().collect();
                    grid(ui, form, state, &shown, &mut |_| None);
                })
                .drop_without_applying_deltas();
        }

        /// The middle of the `n`th tile's picture, counted along the first row.
        fn tile(&self, n: usize) -> egui::Pos2 {
            let step = self.state.tile + egui::Style::default().spacing.item_spacing.x;
            egui::pos2(self.state.tile / 2.0 + n as f32 * step, self.state.tile / 2.0)
        }

        fn carried(&self) -> Option<String> {
            egui::DragAndDrop::payload::<CatalogAsset>(&self.ctx).map(|asset| asset.name.clone())
        }
    }

    /// `clicks` presses and lets go of the primary button at `at`, all in one
    /// frame, which is as quick as two clicks can be.
    fn clicks(at: egui::Pos2, clicks: usize) -> Vec<egui::Event> {
        let button = |pressed| egui::Event::PointerButton {
            pos: at,
            button: egui::PointerButton::Primary,
            pressed,
            modifiers: egui::Modifiers::NONE,
        };
        let mut events = vec![egui::Event::PointerMoved(at)];
        for _ in 0..clicks {
            events.extend([button(true), button(false)]);
        }
        events
    }

    /// A click only picks. A double click on an object adds it, onto the
    /// anchor; on a background it picks and nothing more, since replacing the
    /// scene's background is the details' button's to do.
    #[test]
    fn a_click_picks_and_a_double_click_adds_an_object() {
        let mut grid = Grid::new();
        grid.frame(vec![]);
        let (object, background) = (grid.tile(0), grid.tile(2));

        grid.frame(clicks(object, 1));
        assert_eq!(grid.state.picked, Some(key(&banana())));
        assert_eq!(SENT.with_borrow(Vec::len), 0, "a click sent something");

        grid.frame(clicks(object, 2));
        let sent = SENT.with_borrow(Clone::clone);
        let written = inserted(&sent);
        assert_eq!(written.len(), 2, "{sent:?}");
        assert_eq!(written[1].1["reference"], Value::text("table"), "onto the anchor");

        grid.frame(clicks(background, 2));
        assert_eq!(grid.state.picked.as_ref().map(|(name, _)| name.as_str()), Some("maple_table_robolab"));
        assert_eq!(SENT.with_borrow(Vec::len), 2, "a double click replaced the background");
    }

    /// An object is picked up by dragging its tile; a background, which
    /// nothing in the layout can stand on, is not.
    #[test]
    fn only_an_object_can_be_dragged_out() {
        let press = |at: egui::Pos2| {
            let button = egui::Event::PointerButton {
                pos: at,
                button: egui::PointerButton::Primary,
                pressed: true,
                modifiers: egui::Modifiers::NONE,
            };
            vec![egui::Event::PointerMoved(at), button]
        };
        let away = |at: egui::Pos2| vec![egui::Event::PointerMoved(at + egui::vec2(0.0, 150.0))];

        let mut grid = Grid::new();
        grid.frame(vec![]);
        let background = grid.tile(2);
        grid.frame(press(background));
        grid.frame(away(background));
        assert_eq!(grid.carried(), None);

        let mut grid = Grid::new();
        grid.frame(vec![]);
        let object = grid.tile(0);
        grid.frame(press(object));
        grid.frame(away(object));
        assert_eq!(grid.carried().as_deref(), Some("banana_ycb_robolab"));
    }
}

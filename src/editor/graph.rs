//! The spatial graph: every node of the spec and the relations between them,
//! as Arena's review GUI draws them with Mermaid, in a tab of the dock under
//! the view.
//!
//! A relation naming a reference is an edge from its subject to that
//! reference, labelled with its kind. One naming no other node — `is_anchor`,
//! the position limits — is a chip on its subject. An object reference hangs
//! from the node it is a part of on a dashed edge. The graph reads down the
//! way the scene stands: what a node is placed against is drawn below it, so
//! the anchor, the background and whatever nothing names end up along the
//! foot.
//!
//! A click on a node, a chip or an edge's label picks that entry, and the
//! panel opens its card and scrolls to it (`PanelState` in [`super::form`]).
//! The card stays outlined while the pick stands; a click on the background
//! drops it, and so does any change to the spec. Hovering one shows it in the view the way hovering its card does:
//! a relation's shape, or a node's box. What the view is drawing is orange here
//! too, the colour it draws a relation's subject in.
//!
//! Nothing here edits the spec. The graph is laid out afresh every frame from
//! the spec, so there is nothing to keep in step with it; see [`layout`].

use std::sync::Arc;

use bevy::color::ColorToPacked;
use bevy_egui::egui;
use egui::emath::TSTransform;
use egui::epaint::CubicBezierShape;
use egui::{Color32, FontId, Galley, Pos2, Rect, Sense, Stroke, Vec2, pos2, vec2};

use super::thumbnails::Pictures;
use super::{Form, card, relations};
use crate::spec::{Spec, Step};

/// Space between two things side by side in a row.
const GAP: f32 = 18.0;
/// Space between one row and the next, where the edges bend.
const ROW_GAP: f32 = 18.0;
/// How wide an edge passing through a row is taken to be, so two of them
/// passing through the same row keep apart.
const LANE: f32 = 4.0;
/// The space around the graph when it is fitted to the pane.
const MARGIN: f32 = 12.0;
/// The space between a node's border and what is on it.
const PAD: Vec2 = vec2(7.0, 5.0);
/// The same for a chip, and an edge's label.
const CHIP_PAD: Vec2 = vec2(5.0, 2.0);
/// How far apart the edges leaving or reaching one side of a node are spread.
const PORT: f32 = 14.0;
/// How long an arrowhead is.
const ARROW: f32 = 7.0;
/// How far the pane zooms: out as far as a large graph needs, and in to twice
/// its size. Fitting it never enlarges it past its own size.
const ZOOM: egui::Rangef = egui::Rangef { min: 0.1, max: 2.0 };
/// How many points the wheel scrolls for the pane to zoom by a factor of e.
const WHEEL: f32 = 400.0;

/// Something in the graph a click picks: a node by its id, or a relation by
/// its position in the list, as pins keep one (`PanelState::pinned`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Item {
    Node(String),
    Relation(usize),
}

impl Item {
    /// Where the entry is in `spec`: the path its card is keyed by. None once
    /// the spec no longer has it.
    pub fn path(&self, spec: &Spec) -> Option<Vec<Step>> {
        let at = |key: &str, index: usize| vec![Step::from(key), Step::from(index)];
        match self {
            Item::Relation(index) => (*index < spec.relations.len()).then(|| at("relations", *index)),
            Item::Node(id) if spec.embodiment.id == *id => Some(vec![Step::from("embodiment")]),
            Item::Node(id) if spec.background.id == *id => Some(vec![Step::from("background")]),
            Item::Node(id) => {
                let within = |key: &str, index: Option<usize>| index.map(|index| at(key, index));
                within("objects", position(spec.objects.iter().map(|node| &node.id), id))
                    .or_else(|| within("object_sets", position(spec.object_sets.iter().map(|set| &set.id), id)))
                    .or_else(|| {
                        let references = spec.object_references.iter().map(|reference| &reference.id);
                        within("object_references", position(references, id))
                    })
            }
        }
    }
}

/// Where `id` is among `ids`.
fn position<'a>(mut ids: impl Iterator<Item = &'a String>, id: &str) -> Option<usize> {
    ids.position(|entry| entry == id)
}

/// A pick, and when it was made: the panel goes on scrolling to it for a
/// moment after, while the card it opened unfolds.
pub struct Pick {
    pub item: Item,
    pub at: f64,
}

/// What the graph remembers between frames. None of it is in the spec.
#[derive(Default)]
pub struct Graph {
    /// Where the graph's points land in the pane, once it has been panned or
    /// zoomed by hand. Until then, None: the whole graph fitted to the pane,
    /// following the graph and the pane as either changes.
    view: Option<TSTransform>,
    /// The entry last picked, until the background is clicked or the spec
    /// changes, which drops it in `form::panel`.
    pub picked: Option<Pick>,
}

/// The dock's header for the graph: the fit button, and how to use it.
pub fn header(ui: &mut egui::Ui, state: &mut Graph) {
    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
        if ui.button("fit").on_hover_text("show the whole graph — or double-click its background").clicked() {
            state.view = None;
        }
        let hint = "click: open its card · hover: show it in the view · drag: pan · scroll: zoom";
        ui.add(egui::Label::new(egui::RichText::new(hint).weak()).truncate());
    });
}

/// Draw the graph of `form`'s spec over the whole of `ui`, and say what the
/// pointer is over. `lit` is the relations the view is drawing already — the
/// ones hovered or pinned in the panel — which are lit here too.
///
/// Dragging the background pans, and the wheel zooms about the pointer, as the
/// 3D view above zooms; so does a pinch, or ctrl with the wheel. The zoom is
/// the graph's own rather than a scaled `egui::Scene`'s, so its text is laid
/// out at the size it is drawn and stays sharp: a scene scales text drawn at
/// its own size, and an id's underscores are the first thing lost.
pub fn show(
    ui: &mut egui::Ui,
    form: &Form,
    state: &mut Graph,
    lit: &std::collections::BTreeSet<usize>,
    pictures: &mut Pictures,
) -> Option<Item> {
    let (pane, background) = ui.allocate_exact_size(ui.available_size_before_wrap(), Sense::click_and_drag());
    let drawn = Drawn::of(ui, form, pictures);
    if drawn.nodes.is_empty() {
        let font = egui::TextStyle::Body.resolve(ui.style());
        let color = ui.visuals().weak_text_color();
        ui.painter().text(pane.center(), egui::Align2::CENTER_CENTER, "nothing in the spec yet", font, color);
        return None;
    }
    let bounds = Rect::from_min_size(Pos2::ZERO, drawn.layout.size + Vec2::splat(2.0 * MARGIN));
    let mut view = state.view.unwrap_or_else(|| fit(pane, bounds));
    if background.dragged() {
        view.translation += background.drag_delta();
        state.view = Some(view);
    }
    let (scroll, pinch, pointer) =
        ui.input(|input| (input.smooth_scroll_delta.y, input.zoom_delta(), input.pointer.hover_pos()));
    if let Some(pointer) = pointer.filter(|_| ui.rect_contains_pointer(pane))
        && (scroll != 0.0 || pinch != 1.0)
    {
        view = zoom_about(view, pointer, ZOOM.clamp(view.scaling * (scroll / WHEEL).exp() * pinch));
        state.view = Some(view);
    }

    let picked = state.picked.as_ref().map(|pick| &pick.item);
    let (hovered, clicked) = drawn.paint(ui, pane, view, form, picked, lit);
    if let Some(item) = clicked {
        state.picked = Some(Pick { item, at: ui.input(|input| input.time) });
    } else if background.double_clicked() {
        state.view = None;
    } else if background.clicked() {
        state.picked = None;
    }
    hovered
}

/// Where the graph's points land in `pane` for all of `bounds` to show,
/// centred, and no larger than its own size.
fn fit(pane: Rect, bounds: Rect) -> TSTransform {
    let scale = (pane.size() / bounds.size()).min_elem().clamp(ZOOM.min, 1.0);
    TSTransform::new(pane.center().to_vec2() - scale * bounds.center().to_vec2(), scale)
}

/// `view` zoomed to `scale` about `pointer`: what is under the pointer stays
/// under it.
fn zoom_about(view: TSTransform, pointer: Pos2, scale: f32) -> TSTransform {
    let pointer = pointer.to_vec2();
    TSTransform::new(pointer - (pointer - view.translation) * (scale / view.scaling), scale)
}

/// What kind of node a node is, which its colours and glyph say.
#[derive(Debug, Clone, Copy, PartialEq)]
enum Role {
    Embodiment,
    Background,
    Object,
    ObjectSet,
    ObjectReference,
}

impl Role {
    /// Fill and border: Arena's own palette for its Mermaid graph.
    fn colors(self) -> (Color32, Color32) {
        let rgb = Color32::from_rgb;
        match self {
            Role::Embodiment => (rgb(0x7a, 0x3a, 0x3a), rgb(0xd8, 0x7a, 0x7a)),
            Role::Background => (rgb(0x3a, 0x4f, 0x7a), rgb(0x7a, 0xa0, 0xd8)),
            Role::Object => (rgb(0x7a, 0x6b, 0x3a), rgb(0xd8, 0xc4, 0x7a)),
            Role::ObjectSet => (rgb(0x7a, 0x5a, 0x3a), rgb(0xd8, 0xa8, 0x7a)),
            Role::ObjectReference => (rgb(0x6b, 0x3a, 0x7a), rgb(0xc4, 0x7a, 0xd8)),
        }
    }

    /// The glyph its card wears in the panel.
    fn glyph(self) -> &'static str {
        match self {
            Role::Embodiment => card::EMBODIMENT,
            Role::Background => card::BACKGROUND,
            Role::Object => card::OBJECT,
            Role::ObjectSet => card::OBJECT_SET,
            Role::ObjectReference => card::OBJECT_REFERENCE,
        }
    }
}

/// A node as the graph draws it.
struct Node<'a> {
    id: &'a str,
    role: Role,
    /// What hovering it says: the asset it stands for.
    hover: &'a str,
    /// Its asset's picture, for an object whose asset has one.
    picture: Option<egui::TextureId>,
    /// The relations drawn as chips on it, by position in the list: those
    /// naming no other node.
    chips: Vec<usize>,
}

/// An edge, between two nodes by their position in the node list: a relation
/// from its subject to its reference, or an object reference to its parent.
struct Link {
    from: usize,
    to: usize,
    /// None for an object reference's link to its parent.
    relation: Option<usize>,
}

/// Every node of `form`'s spec in the order the node menus list them, and
/// every edge between them.
///
/// A relation naming no other node is a chip on its subject instead of an
/// edge: a unary one, and one naming its own subject, which has nowhere to
/// run. One whose subject is no node is left out; Arena refuses it anyway.
fn model<'a>(form: &'a Form, pictures: &mut Pictures) -> (Vec<Node<'a>>, Vec<Link>) {
    let spec = &form.spec;
    let node = |id: &'a String, role: Role, hover: &'a str| Node {
        id: id.as_str(),
        role,
        hover,
        picture: None,
        chips: Vec::new(),
    };
    let mut nodes = vec![
        node(&spec.embodiment.id, Role::Embodiment, &spec.embodiment.registry_name),
        node(&spec.background.id, Role::Background, &spec.background.registry_name),
    ];
    for entry in &spec.objects {
        let asset = form.asset(entry);
        nodes.push(Node {
            picture: asset.and_then(|asset| pictures(&asset.thumbnail)),
            ..node(&entry.id, Role::Object, asset.map_or(entry.registry_name.as_str(), |asset| &asset.name))
        });
    }
    nodes.extend(spec.object_sets.iter().map(|set| node(&set.id, Role::ObjectSet, "an object set")));
    nodes.extend(spec.object_references.iter().map(|reference| {
        node(&reference.id, Role::ObjectReference, reference.prim_path.as_deref().unwrap_or("an object reference"))
    }));
    // An empty id is a node half typed; a repeated one, a node Arena refuses.
    let mut seen = std::collections::HashSet::new();
    nodes.retain(|node| !node.id.is_empty() && seen.insert(node.id));

    let index = |id: &str| nodes.iter().position(|node| node.id == id);
    let mut links = Vec::new();
    let mut chips = vec![Vec::new(); nodes.len()];
    for (at, relation) in spec.relations.iter().enumerate() {
        let Some(subject) = index(&relation.subject) else {
            continue;
        };
        match relation.reference.as_deref().and_then(index) {
            Some(reference) if reference != subject => {
                links.push(Link { from: subject, to: reference, relation: Some(at) });
            }
            _ => chips[subject].push(at),
        }
    }
    for reference in &spec.object_references {
        if let (Some(from), Some(to)) = (index(&reference.id), index(&reference.parent_id))
            && from != to
        {
            links.push(Link { from, to, relation: None });
        }
    }
    for (node, chips) in nodes.iter_mut().zip(chips) {
        node.chips = chips;
    }
    (nodes, links)
}

/// The graph measured and laid out, ready to paint.
struct Drawn<'a> {
    nodes: Vec<Node<'a>>,
    links: Vec<Link>,
    /// Each node's id, and its chips' kinds, written at the graph's own size:
    /// what the nodes are measured by.
    titles: Vec<Arc<Galley>>,
    chips: Vec<Vec<Arc<Galley>>>,
    /// What the ids are written in, and the chips and edges' labels. Written
    /// again at the size they are drawn, rather than scaled.
    fonts: (FontId, FontId),
    layout: Layout,
}

impl<'a> Drawn<'a> {
    fn of(ui: &egui::Ui, form: &'a Form, pictures: &mut Pictures) -> Self {
        let (nodes, links) = model(form, pictures);
        let body = egui::TextStyle::Body.resolve(ui.style());
        // A little under the ids, to read as notes on them. `Small` is too
        // small once a graph of any size is fitted to the pane.
        let small = FontId::new(body.size * 0.9, body.family.clone());
        let text =
            |text: &str, font: &FontId| ui.painter().layout_no_wrap(text.to_owned(), font.clone(), Color32::WHITE);
        let kind = |at: usize| form.spec.relations[at].kind.as_str();

        let titles: Vec<_> = nodes.iter().map(|node| text(node.id, &body)).collect();
        let chips: Vec<Vec<_>> =
            nodes.iter().map(|node| node.chips.iter().map(|&at| text(kind(at), &small)).collect()).collect();
        let labels: Vec<_> = links.iter().map(|link| link.relation.map(|at| text(kind(at), &small))).collect();

        let sizes: Vec<Vec2> = titles.iter().zip(&chips).map(|(title, chips)| node_size(title, chips)).collect();
        let edges: Vec<_> = links
            .iter()
            .zip(&labels)
            .map(|(link, label)| (link.from, link.to, label.as_ref().map(|label| label.size() + 2.0 * CHIP_PAD)))
            .collect();
        let mut layout = layout(&sizes, &edges);
        layout.shift(Vec2::splat(MARGIN));
        Self { nodes, links, titles, chips, fonts: (body, small), layout }
    }

    /// The chips on the node at `at`, each with where it stands in the
    /// graph's points: in a row under the node's id.
    fn chips_at(&self, at: usize) -> impl Iterator<Item = (usize, Rect)> + '_ {
        let rect = self.layout.nodes[at];
        let top = rect.top() + PAD.y + self.titles[at].size().y + PAD.y;
        let mut left = rect.left() + PAD.x;
        self.nodes[at].chips.iter().zip(&self.chips[at]).map(move |(&relation, chip)| {
            let chip = Rect::from_min_size(pos2(left, top), chip.size() + 2.0 * CHIP_PAD);
            left = chip.right() + PAD.x / 2.0;
            (relation, chip)
        })
    }

    /// Paint the graph into `pane` of `ui`, its points carried there by
    /// `view`, and say what the pointer is over and what was clicked.
    fn paint(
        &self,
        ui: &mut egui::Ui,
        pane: Rect,
        view: TSTransform,
        form: &Form,
        picked: Option<&Item>,
        lit: &std::collections::BTreeSet<usize>,
    ) -> (Option<Item>, Option<Item>) {
        let painter = ui.painter_at(pane);
        let visuals = ui.visuals().clone();
        let shown = shown_color();
        let selected = visuals.selection.stroke.color;
        let radius = visuals.widgets.noninteractive.corner_radius;
        let scale = view.scaling;
        // Written out at the size drawn, not scaled from the graph's own.
        let write = |text: &str, font: &FontId, color: Color32| {
            painter.layout_no_wrap(text.to_owned(), FontId::new(font.size * scale, font.family.clone()), color)
        };
        let (body, small) = &self.fonts;
        let kind = |at: usize| form.spec.relations[at].kind.as_str();
        let summary = |at: usize| {
            let kind = form.vocabulary.relation(&form.spec.relations[at].kind);
            kind.map_or(String::new(), |kind| kind.summary.clone())
        };
        // Only the part of a rect inside the pane takes the pointer: panned
        // past its edge, a node is under the dock's header or the view.
        let sense = |rect: Rect, id: (&str, usize)| {
            let inside = rect.intersect(pane);
            inside.is_positive().then(|| ui.interact(inside, ui.id().with(id), Sense::click()))
        };
        let mut hovered = None;
        let mut clicked = None;
        let mut report = |response: Option<egui::Response>, item: Item, hover: String| {
            let Some(response) = response else { return };
            let response = match hover.is_empty() {
                true => response,
                false => response.on_hover_text(hover),
            };
            if response.hovered() {
                hovered = Some(item.clone());
            }
            if response.clicked() {
                clicked = Some(item);
            }
        };

        // The edges first, so the nodes and labels cover their ends.
        for (at, (link, route)) in self.links.iter().zip(&self.layout.edges).enumerate() {
            let label = route.label.map(|rect| view * rect);
            let response = label.and_then(|rect| sense(rect, ("graph edge", at)));
            let item = link.relation.map(Item::Relation);
            let is_picked = item.is_some() && item.as_ref() == picked;
            let is_lit = response.as_ref().is_some_and(egui::Response::hovered)
                || link.relation.is_some_and(|at| lit.contains(&at));
            let color = match (is_picked, is_lit) {
                (true, _) => selected,
                (false, true) => shown,
                (false, false) => visuals.widgets.noninteractive.fg_stroke.color,
            };
            let stroke = Stroke::new(if is_picked || is_lit { 2.0 } else { 1.25 }, color);
            let points: Vec<Pos2> = route.points.iter().map(|&point| view * point).collect();
            paint_route(&painter, &points, stroke, ARROW * scale, link.relation.is_none());

            if let (Some(rect), Some(relation)) = (label, link.relation) {
                let fill = match is_lit {
                    true => visuals.widgets.hovered.weak_bg_fill,
                    false => card::fill(&visuals),
                };
                painter.rect(rect, radius, fill, stroke, egui::StrokeKind::Inside);
                let text = write(kind(relation), small, visuals.text_color());
                painter.galley(centered(rect, &text), text, color);
                report(response, Item::Relation(relation), summary(relation));
            }
        }

        for (at, node) in self.nodes.iter().enumerate() {
            let rect = self.layout.nodes[at];
            let response = sense(view * rect, ("graph node", at));
            let item = Item::Node(node.id.to_owned());
            let (fill, border) = node.role.colors();
            let hovering = response.as_ref().is_some_and(egui::Response::hovered);
            let stroke = match (picked == Some(&item), hovering) {
                (true, _) => Stroke::new(2.0, selected),
                (false, true) => Stroke::new(2.0, shown),
                (false, false) => Stroke::new(1.0, border),
            };
            painter.rect(view * rect, radius, fill, stroke, egui::StrokeKind::Inside);

            // Laid out in the graph's points, as the node was measured, and
            // carried into the pane.
            let side = self.titles[at].size().y;
            let icon = Rect::from_min_size(rect.min + PAD, Vec2::splat(side));
            match node.picture {
                Some(texture) => egui::Image::new(egui::load::SizedTexture::new(texture, icon.size() * scale))
                    .corner_radius(radius)
                    .paint_at(ui, view * icon),
                None => {
                    let font = FontId::new(body.size * scale, body.family.clone());
                    painter.text(
                        view * icon.center(),
                        egui::Align2::CENTER_CENTER,
                        node.role.glyph(),
                        font,
                        Color32::WHITE,
                    );
                }
            }
            let title = pos2(icon.right() + PAD.x / 2.0, icon.top());
            painter.galley(view * title, write(node.id, body, Color32::WHITE), Color32::WHITE);
            report(response, item, node.hover.to_owned());

            // After the node, so a chip is over it and takes the pointer from it.
            for (relation, chip) in self.chips_at(at) {
                let response = sense(view * chip, ("graph chip", relation));
                let item = Item::Relation(relation);
                let hovering = response.as_ref().is_some_and(egui::Response::hovered);
                let stroke = match (picked == Some(&item), hovering || lit.contains(&relation)) {
                    (true, _) => Stroke::new(1.5, selected),
                    (false, true) => Stroke::new(1.5, shown),
                    (false, false) => Stroke::NONE,
                };
                painter.rect(view * chip, radius, Color32::from_black_alpha(90), stroke, egui::StrokeKind::Inside);
                let text = write(kind(relation), small, Color32::WHITE);
                painter.galley(centered(view * chip, &text), text, Color32::WHITE);
                report(response, item, summary(relation));
            }
        }
        (hovered, clicked)
    }
}

/// How large a node is drawn: its glyph or picture and its id on one line,
/// and its chips in a row under them.
fn node_size(title: &Galley, chips: &[Arc<Galley>]) -> Vec2 {
    let side = title.size().y;
    let header = vec2(side + PAD.x / 2.0 + title.size().x, side);
    if chips.is_empty() {
        return header + 2.0 * PAD;
    }
    let widths = chips.iter().map(|chip| chip.size().x + 2.0 * CHIP_PAD.x);
    let row = widths.sum::<f32>() + (chips.len() - 1) as f32 * PAD.x / 2.0;
    let height = chips.iter().map(|chip| chip.size().y).fold(0.0, f32::max) + 2.0 * CHIP_PAD.y;
    vec2(header.x.max(row), header.y + PAD.y + height) + 2.0 * PAD
}

/// Where to put `text` for its letters to sit in the middle of `rect`. Text
/// is laid out afresh at each zoom, where its metrics round differently from
/// the size it was measured at, so it is centred on what it draws rather than
/// set in by a padding.
fn centered(rect: Rect, text: &Galley) -> Pos2 {
    pos2(rect.center().x - text.size().x / 2.0, rect.center().y - text.mesh_bounds.center().y)
}

/// The colour the view draws a relation's subject in, which is what the graph
/// lights whatever the view is drawing with.
fn shown_color() -> Color32 {
    let [r, g, b, _] = relations::SUBJECT.to_u8_array();
    Color32::from_rgb(r, g, b)
}

/// Paint an edge along its route: a curve down each gap between rows, and a
/// straight run through each row it passes. The arrowhead goes where it ends,
/// and the line stops at its base, so it meets the head square on.
fn paint_route(painter: &egui::Painter, points: &[Pos2], stroke: Stroke, arrow: f32, dashed: bool) {
    let [.., before, tip] = *points else { return };
    let down = if tip.y >= before.y { 1.0 } else { -1.0 };
    let base = tip - vec2(0.0, down * arrow);
    let mut points = points.to_vec();
    *points.last_mut().unwrap() = base;
    for (at, pair) in points.windows(2).enumerate() {
        let (from, to) = (pair[0], pair[1]);
        let line = match at % 2 {
            // Leaving and arriving straight up or down, as a node's edges do.
            0 => {
                let bend = vec2(0.0, (to.y - from.y) / 2.0);
                let curve = [from, from + bend, to - bend, to];
                CubicBezierShape::from_points_stroke(curve, false, Color32::TRANSPARENT, stroke).flatten(Some(0.2))
            }
            _ => vec![from, to],
        };
        match dashed {
            true => painter.extend(egui::Shape::dashed_line(&line, stroke, 4.0, 3.0)),
            false => {
                painter.line(line, stroke);
            }
        }
    }
    let head = vec![tip, base + vec2(arrow / 2.0, 0.0), base - vec2(arrow / 2.0, 0.0)];
    painter.add(egui::Shape::convex_polygon(head, stroke.color, Stroke::NONE));
}

/// Where everything is drawn, in points, the graph's top-left at the origin.
#[derive(Debug)]
struct Layout {
    /// Each node's box, by its position in the input.
    nodes: Vec<Rect>,
    /// Each edge's route, by its position in the input.
    edges: Vec<Route>,
    size: Vec2,
}

/// Where one edge runs.
#[derive(Debug, Default)]
struct Route {
    /// From the node it leaves to the one it reaches, alternately the ends of
    /// a gap between rows and the ends of a run through a row. Empty for an
    /// edge from a node to itself.
    points: Vec<Pos2>,
    /// The box its label is drawn in, where it has one.
    label: Option<Rect>,
}

impl Layout {
    fn shift(&mut self, by: Vec2) {
        for rect in self.nodes.iter_mut().chain(self.edges.iter_mut().filter_map(|edge| edge.label.as_mut())) {
            *rect = rect.translate(by);
        }
        for point in self.edges.iter_mut().flat_map(|edge| &mut edge.points) {
            *point += by;
        }
    }
}

/// How many times the rows are swept to untangle them, and to settle where
/// things stand along them.
const SWEEPS: usize = 12;

/// Lay out a graph of boxes, `sizes` the nodes' and `edges` running from one
/// node to another with a label of the given size where they have one — as a
/// layered drawing does it (Sugiyama's; dagre, which Mermaid draws with, is
/// one):
///
/// 1. Edges that would close a loop are laid out the other way up, so the
///    rest all point down (`back_edges`).
/// 2. Each node takes a row by the longest way down from it, so everything an
///    edge reaches is below where it leaves, and everything reaching nothing
///    is along the foot (`ranks`).
/// 3. Between two rows of nodes is a row for labels, and each edge takes a
///    place in every row it passes through — its label's box in the row under
///    where it leaves, a narrow lane in the rest — so the rows below keep room
///    for it.
/// 4. Each row is reordered so fewer edges cross (`untangle`), and each thing
///    in it moved as near the middle of what it joins as the others in the
///    row allow (`place`).
fn layout(sizes: &[Vec2], edges: &[(usize, usize, Option<Vec2>)]) -> Layout {
    let back = back_edges(sizes.len(), edges);
    let down: Vec<(usize, usize)> =
        edges.iter().zip(&back).map(|(&(from, to, _), &back)| if back { (to, from) } else { (from, to) }).collect();
    let rank = ranks(sizes.len(), &down);

    // Everything with a place in a row: the nodes, and after them the lanes
    // and labels each edge takes on its way down.
    let mut row: Vec<usize> = rank.iter().map(|rank| 2 * rank).collect();
    let mut size = sizes.to_vec();
    let mut chains = Vec::with_capacity(edges.len());
    let mut labels = Vec::with_capacity(edges.len());
    for (at, (&(upper, lower), &(.., label))) in down.iter().zip(edges).enumerate() {
        let mut chain = vec![upper];
        let mut label_at = None;
        if upper != lower {
            // Beside the subject: under it, or over it on an edge laid out
            // the other way up.
            let label_row = if back[at] { row[lower] - 1 } else { row[upper] + 1 };
            for through in row[upper] + 1..row[lower] {
                if through == label_row && label.is_some() {
                    label_at = Some(size.len());
                }
                chain.push(size.len());
                row.push(through);
                size.push(label.filter(|_| through == label_row).unwrap_or(vec2(LANE, 0.0)));
            }
            chain.push(lower);
        }
        chains.push(chain);
        labels.push(label_at);
    }

    let rows = row.iter().max().map_or(0, |last| last + 1);
    let mut above = vec![Vec::new(); size.len()];
    let mut below = vec![Vec::new(); size.len()];
    for chain in &chains {
        for pair in chain.windows(2) {
            below[pair[0]].push(pair[1]);
            above[pair[1]].push(pair[0]);
        }
    }
    let mut order = vec![Vec::new(); rows];
    for (at, &row) in row.iter().enumerate() {
        order[row].push(at);
    }
    let order = untangle(order, &above, &below);

    // Across: packed from the left, then settled towards what each joins.
    let mut x = vec![0.0; size.len()];
    for row in &order {
        let mut left = 0.0;
        for &at in row {
            x[at] = left + size[at].x / 2.0;
            left += size[at].x + GAP;
        }
    }
    for sweep in 0..SWEEPS {
        let rows: Box<dyn Iterator<Item = &Vec<usize>>> = match sweep % 2 {
            0 => Box::new(order.iter()),
            _ => Box::new(order.iter().rev()),
        };
        for row in rows {
            let wanted: Vec<f32> = row
                .iter()
                .map(|&at| {
                    let joined: Vec<f32> = above[at].iter().chain(&below[at]).map(|&other| x[other]).collect();
                    match joined.is_empty() {
                        true => x[at],
                        false => joined.iter().sum::<f32>() / joined.len() as f32,
                    }
                })
                .collect();
            let apart: Vec<f32> = row.windows(2).map(|pair| (size[pair[0]].x + size[pair[1]].x) / 2.0 + GAP).collect();
            for (&at, placed) in row.iter().zip(place(&wanted, &apart)) {
                x[at] = placed;
            }
        }
    }
    let left = (0..size.len()).map(|at| x[at] - size[at].x / 2.0).fold(f32::INFINITY, f32::min);
    for x in &mut x {
        *x -= left;
    }

    // Down: each row as tall as the tallest thing in it.
    let height: Vec<f32> = order.iter().map(|row| row.iter().map(|&at| size[at].y).fold(0.0, f32::max)).collect();
    let mut top = Vec::with_capacity(rows);
    let mut y = 0.0;
    for height in &height {
        top.push(y);
        y += height + ROW_GAP;
    }
    let bounds = |at: usize| {
        let row = row[at];
        Rect::from_center_size(pos2(x[at], top[row] + height[row] / 2.0), size[at])
    };

    // Where each edge meets its nodes: spread along the node's side in the
    // order of where they head, so that two leave or arrive side by side.
    let mut ends = vec![(0.0, 0.0); edges.len()];
    for node in 0..sizes.len() {
        let joined = chains.iter().enumerate().filter(|(_, chain)| chain.len() > 1);
        let leaving = joined.clone().filter(|(_, chain)| chain[0] == node).map(|(edge, chain)| (edge, x[chain[1]]));
        for (edge, at) in spread(x[node], size[node].x, leaving.collect()) {
            ends[edge].0 = at;
        }
        let reaching = joined.filter(|(_, chain)| chain[chain.len() - 1] == node);
        for (edge, at) in
            spread(x[node], size[node].x, reaching.map(|(edge, chain)| (edge, x[chain[chain.len() - 2]])).collect())
        {
            ends[edge].1 = at;
        }
    }

    let routes = chains
        .iter()
        .enumerate()
        .map(|(edge, chain)| {
            let [first, .., last] = chain[..] else {
                return Route::default();
            };
            let mut points = vec![pos2(ends[edge].0, bounds(first).bottom())];
            for &through in &chain[1..chain.len() - 1] {
                // A label is entered and left at its own edges; a lane runs
                // the full height of its row.
                let (from, to) = match labels[edge] == Some(through) {
                    true => (bounds(through).top(), bounds(through).bottom()),
                    false => (top[row[through]], top[row[through]] + height[row[through]]),
                };
                points.extend([pos2(x[through], from), pos2(x[through], to)]);
            }
            points.push(pos2(ends[edge].1, bounds(last).top()));
            if back[edge] {
                points.reverse();
            }
            Route { points, label: labels[edge].map(bounds) }
        })
        .collect();

    let width = (0..size.len()).map(|at| x[at] + size[at].x / 2.0).fold(0.0, f32::max);
    Layout { nodes: (0..sizes.len()).map(bounds).collect(), edges: routes, size: vec2(width, (y - ROW_GAP).max(0.0)) }
}

/// Where each of `edges` meets one side of a node `width` wide about `centre`,
/// given where each heads: side by side about the middle, in that order.
fn spread(centre: f32, width: f32, mut edges: Vec<(usize, f32)>) -> Vec<(usize, f32)> {
    edges.sort_by(|a, b| a.1.total_cmp(&b.1));
    let step = (width * 0.7 / edges.len() as f32).min(PORT);
    let middle = (edges.len() as f32 - 1.0) / 2.0;
    edges.into_iter().enumerate().map(|(k, (edge, _))| (edge, centre + (k as f32 - middle) * step)).collect()
}

/// Which edges to lay out the other way up, so that following the rest never
/// leads back where it started: those a walk depth first finds pointing back
/// into its own path. An edge from a node to itself is one.
fn back_edges(nodes: usize, edges: &[(usize, usize, Option<Vec2>)]) -> Vec<bool> {
    #[derive(Clone, Copy, PartialEq)]
    enum Seen {
        Not,
        OnPath,
        Done,
    }
    fn walk(
        node: usize,
        edges: &[(usize, usize, Option<Vec2>)],
        leaving: &[Vec<usize>],
        seen: &mut [Seen],
        back: &mut [bool],
    ) {
        seen[node] = Seen::OnPath;
        for &edge in &leaving[node] {
            let next = edges[edge].1;
            match seen[next] {
                Seen::Not => walk(next, edges, leaving, seen, back),
                Seen::OnPath => back[edge] = true,
                Seen::Done => {}
            }
        }
        seen[node] = Seen::Done;
    }
    let mut leaving = vec![Vec::new(); nodes];
    for (at, edge) in edges.iter().enumerate() {
        leaving[edge.0].push(at);
    }
    let mut seen = vec![Seen::Not; nodes];
    let mut back = vec![false; edges.len()];
    for node in 0..nodes {
        if seen[node] == Seen::Not {
            walk(node, edges, &leaving, &mut seen, &mut back);
        }
    }
    back
}

/// Each node's row, counted down from the top: the longest way down from a
/// node, through edges that all point down, is how far above the foot it
/// stands.
fn ranks(nodes: usize, down: &[(usize, usize)]) -> Vec<usize> {
    fn measure(node: usize, below: &[Vec<usize>], height: &mut [Option<usize>]) -> usize {
        if let Some(height) = height[node] {
            return height;
        }
        let measured = below[node].iter().map(|&next| measure(next, below, height) + 1).max().unwrap_or(0);
        height[node] = Some(measured);
        measured
    }
    let mut below = vec![Vec::new(); nodes];
    for &(upper, lower) in down.iter().filter(|(upper, lower)| upper != lower) {
        below[upper].push(lower);
    }
    let mut height = vec![None; nodes];
    let heights: Vec<usize> = (0..nodes).map(|node| measure(node, &below, &mut height)).collect();
    let highest = heights.iter().copied().max().unwrap_or(0);
    heights.into_iter().map(|height| highest - height).collect()
}

/// Reorder each row so fewer edges cross: everything moves to the mean
/// position of what it joins in the row just swept, down the rows and then
/// back up, and the order with the fewest crossings seen is the one kept.
fn untangle(mut order: Vec<Vec<usize>>, above: &[Vec<usize>], below: &[Vec<usize>]) -> Vec<Vec<usize>> {
    let mut position = vec![0.0; above.len()];
    let number = |order: &[Vec<usize>], position: &mut [f32]| {
        for row in order {
            for (at, &item) in row.iter().enumerate() {
                position[item] = at as f32;
            }
        }
    };
    number(&order, &mut position);
    let mut best = (crossings(&order, below, &position), order.clone());
    for sweep in 0..SWEEPS {
        let (rows, joined): (Vec<usize>, _) = match sweep % 2 {
            0 => ((1..order.len()).collect(), above),
            _ => ((0..order.len().saturating_sub(1)).rev().collect(), below),
        };
        for row in rows {
            let mut keyed: Vec<(f32, usize)> = order[row]
                .iter()
                .map(|&item| match joined[item].len() {
                    0 => (position[item], item),
                    count => (joined[item].iter().map(|&other| position[other]).sum::<f32>() / count as f32, item),
                })
                .collect();
            // Stable, so a tie keeps the order it had.
            keyed.sort_by(|a, b| a.0.total_cmp(&b.0));
            order[row] = keyed.into_iter().map(|(_, item)| item).collect();
            number(&order[row..=row], &mut position);
        }
        let crossed = crossings(&order, below, &position);
        if crossed < best.0 {
            best = (crossed, order.clone());
        }
    }
    best.1
}

/// How many pairs of edges cross between each row and the next, down the
/// whole graph.
fn crossings(order: &[Vec<usize>], below: &[Vec<usize>], position: &[f32]) -> usize {
    order
        .iter()
        .map(|row| {
            let segments: Vec<(f32, f32)> = row
                .iter()
                .flat_map(|&item| below[item].iter().map(move |&next| (position[item], position[next])))
                .collect();
            let mut crossed = 0;
            for (at, a) in segments.iter().enumerate() {
                crossed += segments[at + 1..].iter().filter(|b| (a.0 - b.0) * (a.1 - b.1) < 0.0).count();
            }
            crossed
        })
        .sum()
}

/// Centres for a row of things in order, as near `wanted` as they can stand
/// with each at least `apart` from the next: the least-squares fit, found by
/// pooling any run of neighbours that would otherwise overlap and standing it
/// at its mean (pool adjacent violators).
fn place(wanted: &[f32], apart: &[f32]) -> Vec<f32> {
    // Measured from where each would stand packed against the first, the
    // constraint is only that they never go down.
    let packed: Vec<f32> = std::iter::once(0.0)
        .chain(apart.iter().scan(0.0, |offset, apart| {
            *offset += apart;
            Some(*offset)
        }))
        .collect();
    // Pools of (sum, count), each standing at its mean.
    let mut pools: Vec<(f32, usize)> = Vec::new();
    for (wanted, offset) in wanted.iter().zip(&packed) {
        pools.push((wanted - offset, 1));
        while let [.., (sum_a, count_a), (sum_b, count_b)] = pools[..]
            && sum_a / count_a as f32 > sum_b / count_b as f32
        {
            pools.truncate(pools.len() - 2);
            pools.push((sum_a + sum_b, count_a + count_b));
        }
    }
    pools
        .into_iter()
        .flat_map(|(sum, count)| std::iter::repeat_n(sum / count as f32, count))
        .zip(packed)
        .map(|(pooled, offset)| pooled + offset)
        .collect()
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeSet;

    use super::*;
    use crate::spec::{Node as Entry, ObjectReference, Relation};

    /// A box `width` by 20, and an edge's label.
    fn node(width: f32) -> Vec2 {
        vec2(width, 20.0)
    }
    const LABEL: Option<Vec2> = Some(vec2(30.0, 14.0));

    /// No two things in the layout overlap: the nodes, and the labels.
    fn assert_apart(layout: &Layout) {
        let labels = layout.edges.iter().filter_map(|edge| edge.label);
        let boxes: Vec<Rect> = layout.nodes.iter().copied().chain(labels).collect();
        for (at, a) in boxes.iter().enumerate() {
            for b in &boxes[at + 1..] {
                assert!(!a.shrink(0.5).intersects(*b), "{a:?} overlaps {b:?}");
            }
        }
    }

    /// What an edge reaches is drawn below where it leaves, and everything
    /// reaching nothing — here the robot — stands along the foot.
    #[test]
    fn what_a_node_is_placed_against_is_drawn_below_it() {
        let (banana, plate, table, robot) = (0, 1, 2, 3);
        let layout = layout(&[node(60.0); 4], &[(banana, plate, LABEL), (plate, table, LABEL)]);
        let [banana, plate, table, robot] = [banana, plate, table, robot].map(|at| layout.nodes[at]);
        assert!(banana.bottom() < plate.top() && plate.bottom() < table.top(), "{layout:?}");
        assert_eq!(robot.center().y, table.center().y, "the robot is off the foot");
        assert_apart(&layout);
    }

    /// Each edge's route leaves its first node and reaches its second, the
    /// arrow's end, whichever way up it had to be laid out to break a loop.
    /// An edge from a node to itself has no route, and breaks nothing.
    #[test]
    fn a_loop_is_laid_out_all_the_same() {
        let edges = [(0, 1, LABEL), (1, 2, LABEL), (2, 0, LABEL), (1, 0, None), (2, 2, LABEL)];
        let layout = layout(&[node(40.0), node(50.0), node(60.0)], &edges);
        for (&(from, to, _), route) in edges.iter().zip(&layout.edges) {
            if from == to {
                assert!(route.points.is_empty(), "a node's edge to itself was routed");
                continue;
            }
            let (from, to) = (layout.nodes[from], layout.nodes[to]);
            let (first, last) = (route.points[0], route.points[route.points.len() - 1]);
            assert!(from.x_range().contains(first.x) && [from.top(), from.bottom()].contains(&first.y), "{route:?}");
            assert!(to.x_range().contains(last.x) && [to.top(), to.bottom()].contains(&last.y), "{route:?}");
        }
        assert_apart(&layout);
    }

    /// A row of objects on one table, some beside others, with every label:
    /// nothing is drawn over anything else.
    #[test]
    fn nothing_in_a_row_overlaps() {
        let table = 0;
        let mut edges: Vec<_> = (1..7).map(|object| (object, table, LABEL)).collect();
        edges.extend([(1, 2, LABEL), (3, 5, LABEL), (6, 1, LABEL)]);
        let sizes: Vec<Vec2> = (0..7).map(|at| node(40.0 + 10.0 * at as f32)).collect();
        assert_apart(&layout(&sizes, &edges));
    }

    /// Two edges that could as well run side by side do not cross, whatever
    /// order the nodes came in.
    #[test]
    fn edges_that_need_not_cross_do_not() {
        let (a, b, c, d) = (0, 1, 2, 3);
        let layout = layout(&[node(40.0); 4], &[(a, d, None), (b, c, None)]);
        let x = |at: usize| layout.nodes[at].center().x;
        assert_eq!(x(a) < x(b), x(d) < x(c), "{layout:?}");
    }

    /// A node stands over the middle of what it joins: the table under the two
    /// objects on it.
    #[test]
    fn a_node_stands_over_the_middle_of_what_it_joins() {
        let (table, left, right) = (0, 1, 2);
        let layout = layout(&[node(120.0), node(40.0), node(80.0)], &[(left, table, LABEL), (right, table, LABEL)]);
        let x = |at: usize| layout.nodes[at].center().x;
        assert!((x(table) - (x(left) + x(right)) / 2.0).abs() < 1.0, "{layout:?}");
    }

    /// Each as near as it wants to be, as far apart as it has to be: a crowd
    /// spreads about where it wanted to stand, and a gap left alone stays.
    #[test]
    fn a_row_is_placed_as_near_as_it_can_be_to_where_it_wants() {
        assert_eq!(place(&[0.0, 0.0, 0.0], &[10.0, 10.0]), [-10.0, 0.0, 10.0]);
        assert_eq!(place(&[0.0, 100.0], &[10.0]), [0.0, 100.0]);
        assert_eq!(place(&[5.0, 0.0], &[10.0]), [-2.5, 7.5], "out of order, pooled at the mean");
    }

    /// A scene with an anchor, a reference, and relations naming nothing, their
    /// own subject, or a node that is not there.
    fn scene() -> Form {
        let entry = |id: &str| Entry { id: id.into(), ..Default::default() };
        let relation = |kind: &str, subject: &str, reference: Option<&str>| Relation {
            kind: kind.into(),
            subject: subject.into(),
            reference: reference.map(Into::into),
            ..Default::default()
        };
        let mut form = Form::default();
        form.spec.embodiment = entry("droid");
        form.spec.background = entry("table");
        form.spec.objects = vec![entry("banana"), entry("bowl")];
        form.spec.object_references =
            vec![ObjectReference { id: "top".into(), parent_id: "table".into(), ..Default::default() }];
        form.spec.relations = vec![
            relation("is_anchor", "top", None),
            relation("on", "banana", Some("top")),
            relation("next_to", "banana", Some("banana")),
            relation("on", "bowl", Some("ghost")),
            relation("on", "ghost", Some("top")),
        ];
        form
    }

    /// A relation naming no other node is a chip on its subject, whether it
    /// names none, its own subject, or one that is not there. One whose
    /// subject is not there is nowhere. A reference hangs from its parent.
    #[test]
    fn a_relation_naming_no_other_node_is_a_chip() {
        let form = scene();
        let (nodes, links) = model(&form, &mut |_| None);
        let ids: Vec<&str> = nodes.iter().map(|node| node.id).collect();
        assert_eq!(ids, ["droid", "table", "banana", "bowl", "top"]);
        let chips: Vec<&[usize]> = nodes.iter().map(|node| node.chips.as_slice()).collect();
        assert_eq!(chips, [&[][..], &[], &[2], &[3], &[0]]);
        let edges: Vec<_> = links.iter().map(|link| (ids[link.from], ids[link.to], link.relation)).collect();
        assert_eq!(edges, [("banana", "top", Some(1)), ("top", "table", None)]);
    }

    /// A pick names the card it opens by the path the panel keys it by.
    #[test]
    fn a_pick_is_found_by_its_path_in_the_spec() {
        let spec = scene().spec;
        let path = |item: Item| item.path(&spec).map(|path| format!("{path:?}"));
        let node = |id: &str| Item::Node(id.into());
        assert_eq!(path(node("droid")), Some(format!("{:?}", [Step::from("embodiment")])));
        assert_eq!(path(node("table")), Some(format!("{:?}", [Step::from("background")])));
        assert_eq!(path(node("bowl")), Some(format!("{:?}", [Step::from("objects"), Step::from(1usize)])));
        assert_eq!(path(node("top")), Some(format!("{:?}", [Step::from("object_references"), Step::from(0usize)])));
        assert_eq!(path(Item::Relation(4)), Some(format!("{:?}", [Step::from("relations"), Step::from(4usize)])));
        assert_eq!(path(Item::Relation(5)), None, "a relation since removed");
        assert_eq!(path(node("ghost")), None, "a node since removed");
    }

    /// The point under the pointer stays there however far the pane zooms.
    #[test]
    fn the_wheel_zooms_about_the_pointer() {
        let view = TSTransform::new(vec2(30.0, -12.0), 0.8);
        let pointer = pos2(200.0, 90.0);
        let under = view.inverse() * pointer;
        let zoomed = zoom_about(view, pointer, 1.7);
        assert_eq!(zoomed.scaling, 1.7);
        assert!((zoomed * under - pointer).length() < 1e-3, "{:?}", zoomed * under);
    }

    /// The graph alone in a headless egui, a frame at a time, with the input a
    /// person would give it, as the browser's tests drive its grid.
    struct Pane {
        ctx: egui::Context,
        form: Form,
        state: Graph,
        time: f64,
        /// What the pointer was over, as the last frame said.
        hovered: Option<Item>,
        /// Where each node, label and chip was drawn in the last frame.
        drawn: Vec<(Item, Pos2)>,
    }

    impl Pane {
        fn new() -> Self {
            let mut pane = Self {
                ctx: egui::Context::default(),
                form: scene(),
                state: Graph::default(),
                time: 0.0,
                hovered: None,
                drawn: Vec::new(),
            };
            pane.frame(vec![]);
            pane
        }

        fn frame(&mut self, events: Vec<egui::Event>) {
            // A second a frame, so two clicks are never one double click.
            self.time += 1.0;
            let input = egui::RawInput {
                events,
                time: Some(self.time),
                screen_rect: Some(Rect::from_min_size(Pos2::ZERO, vec2(800.0, 300.0))),
                ..Default::default()
            };
            let (form, state) = (&self.form, &mut self.state);
            let (mut hovered, mut drawn) = (None, Vec::new());
            self.ctx
                .run_ui(input, |ui| {
                    let pane = ui.max_rect();
                    hovered = show(ui, form, state, &BTreeSet::new(), &mut |_| None);
                    let graph = Drawn::of(ui, form, &mut |_| None);
                    let bounds = Rect::from_min_size(Pos2::ZERO, graph.layout.size + Vec2::splat(2.0 * MARGIN));
                    let view = state.view.unwrap_or_else(|| fit(pane, bounds));
                    for (at, node) in graph.nodes.iter().enumerate() {
                        drawn.push((Item::Node(node.id.to_owned()), view * graph.layout.nodes[at].center()));
                        drawn.extend(
                            graph.chips_at(at).map(|(relation, chip)| (Item::Relation(relation), view * chip.center())),
                        );
                    }
                    for (link, route) in graph.links.iter().zip(&graph.layout.edges) {
                        if let (Some(relation), Some(label)) = (link.relation, route.label) {
                            drawn.push((Item::Relation(relation), view * label.center()));
                        }
                    }
                })
                .drop_without_applying_deltas();
            (self.hovered, self.drawn) = (hovered, drawn);
        }

        /// Where `item` was drawn: a node's box, a chip, or an edge's label.
        fn at(&self, item: &Item) -> Pos2 {
            self.drawn.iter().find(|(drawn, _)| drawn == item).map(|(_, at)| *at).expect("not drawn")
        }

        fn picked(&self) -> Option<&Item> {
            self.state.picked.as_ref().map(|pick| &pick.item)
        }
    }

    /// Presses and lets go of the primary button at `at`.
    fn click(at: Pos2) -> Vec<egui::Event> {
        let button = |pressed| egui::Event::PointerButton {
            pos: at,
            button: egui::PointerButton::Primary,
            pressed,
            modifiers: egui::Modifiers::NONE,
        };
        vec![egui::Event::PointerMoved(at), button(true), button(false)]
    }

    /// A click on a node, a chip or an edge's label picks what it stands for,
    /// and a click on the background drops the pick.
    #[test]
    fn a_click_picks_what_it_lands_on_and_the_background_drops_it() {
        let mut pane = Pane::new();
        for item in [Item::Node("bowl".into()), Item::Relation(0), Item::Relation(1), Item::Relation(2)] {
            let at = pane.at(&item);
            pane.frame(click(at));
            assert_eq!(pane.picked(), Some(&item));
        }
        pane.frame(click(pos2(4.0, 4.0)));
        assert_eq!(pane.picked(), None, "the background kept the pick");
    }

    /// The pointer over a label is over its relation, which the view then
    /// draws; over a node, the node.
    #[test]
    fn hovering_says_what_the_pointer_is_over() {
        let mut pane = Pane::new();
        for item in [Item::Relation(1), Item::Node("banana".into())] {
            let at = pane.at(&item);
            pane.frame(vec![egui::Event::PointerMoved(at)]);
            assert_eq!(pane.hovered, Some(item));
        }
    }

    /// A drag across the background pans, and a drag starting on a node pans
    /// just the same rather than picking it.
    #[test]
    fn a_drag_pans_wherever_it_starts() {
        let mut pane = Pane::new();
        let before = pane.at(&Item::Node("banana".into()));
        let drag = |from: Pos2| {
            let button = |at, pressed| egui::Event::PointerButton {
                pos: at,
                button: egui::PointerButton::Primary,
                pressed,
                modifiers: egui::Modifiers::NONE,
            };
            let to = from + vec2(40.0, 10.0);
            [
                vec![egui::Event::PointerMoved(from), button(from, true)],
                vec![egui::Event::PointerMoved(from + vec2(20.0, 5.0))],
                vec![egui::Event::PointerMoved(to)],
                vec![button(to, false)],
            ]
        };
        for events in drag(before) {
            pane.frame(events);
        }
        let after = pane.at(&Item::Node("banana".into()));
        assert!((after - before - vec2(40.0, 10.0)).length() < 1.0, "{before:?} to {after:?}");
        assert_eq!(pane.picked(), None, "the drag picked what it started on");
    }
}

//! The card every entry of the spec is drawn as, after Blender's modifier
//! stack: a bordered unit whose header names the entry, and whose body — the
//! entry's settings — folds away under it.
//!
//! The header reads left to right: the fold arrow, an icon for what sort of
//! entry it is, the title — the entry's id, or a relation's kind — and at the
//! far edge the entry's toggle and the × that removes it. Every section draws
//! its entries through here, so one added later looks and behaves like the
//! rest: it says what goes in the title and in the body, and nothing else.
//!
//! A node's card wears its node's colours from the graph, so that the two are
//! matched by eye: a stripe down its left edge in the node's border colour,
//! its fill tinted toward the node's, and its glyph in white, as the graph
//! draws it. A relation, the task and a subtask are no node, and stay grey.

use bevy_egui::egui;
use egui::collapsing_header::{CollapsingState, paint_default_icon};
use egui::Color32;

/// The glyph each kind of node wears: on its card, on its tile in the asset
/// browser where it has no picture, and on its node in the graph.
pub const EMBODIMENT: &str = "🕹";
pub const BACKGROUND: &str = "🏠";
pub const OBJECT: &str = "📦";
pub const OBJECT_SET: &str = "🎲";
pub const OBJECT_REFERENCE: &str = "📍";

/// What kind of node an entry is, which its colours and glyph say: on its card
/// here, and on its node in the graph, which [`super::graph`] draws with them.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Role {
    Embodiment,
    Background,
    Object,
    ObjectSet,
    ObjectReference,
}

impl Role {
    /// Fill and border: Arena's own palette for its Mermaid graph.
    pub fn colors(self) -> (Color32, Color32) {
        let rgb = Color32::from_rgb;
        match self {
            Role::Embodiment => (rgb(0x7a, 0x3a, 0x3a), rgb(0xd8, 0x7a, 0x7a)),
            Role::Background => (rgb(0x3a, 0x4f, 0x7a), rgb(0x7a, 0xa0, 0xd8)),
            Role::Object => (rgb(0x7a, 0x6b, 0x3a), rgb(0xd8, 0xc4, 0x7a)),
            Role::ObjectSet => (rgb(0x7a, 0x5a, 0x3a), rgb(0xd8, 0xa8, 0x7a)),
            Role::ObjectReference => (rgb(0x6b, 0x3a, 0x7a), rgb(0xc4, 0x7a, 0xd8)),
        }
    }

    /// The glyph it wears, on its card and on its node in the graph.
    pub fn glyph(self) -> &'static str {
        match self {
            Role::Embodiment => EMBODIMENT,
            Role::Background => BACKGROUND,
            Role::Object => OBJECT,
            Role::ObjectSet => OBJECT_SET,
            Role::ObjectReference => OBJECT_REFERENCE,
        }
    }
}

/// What sort of entry a card holds, drawn left of its title.
pub enum Icon {
    /// A node of that kind: its glyph, in white on the tinted card.
    Node(Role),
    /// A node of that kind whose asset has a picture: the picture.
    Picture(Role, egui::TextureId),
    /// A glyph from egui's bundled fonts, and only those: a glyph they lack
    /// draws as a box. For an entry that is no node — a relation, the task —
    /// and so has no colour.
    Glyph(&'static str),
}

/// How far a node's card is tinted toward its node's fill: enough to tell the
/// kinds apart down the panel, little enough that the controls on it still
/// look like the controls on every other card.
const TINT: f32 = 0.15;
/// How wide the stripe down a node's card is.
const STRIPE: f32 = 3.0;

/// One entry of the spec, drawn by [`Card::show`].
pub struct Card<'a> {
    id: egui::Id,
    icon: Icon,
    summary: String,
    open: bool,
    pin: Option<&'a mut bool>,
    removable: bool,
    picked: bool,
    reveal: bool,
}

/// What a card reports once it is drawn.
pub struct Shown {
    /// The whole card, header and body. Hovering any of it, controls included,
    /// is looking at the entry.
    pub response: egui::Response,
    /// Whether its × was clicked this frame. Reported rather than acted on,
    /// because the removal has to wait for the frame's other edits — see
    /// `PanelState::removing`.
    pub removed: bool,
}

impl<'a> Card<'a> {
    /// A card with `id` unique in the panel: it keeps the card's fold, and
    /// scopes the ids of what is drawn inside it.
    pub fn new(id: impl egui::AsId, icon: Icon) -> Self {
        Self {
            id: egui::Id::new(id),
            icon,
            summary: String::new(),
            open: true,
            pin: None,
            removable: false,
            picked: false,
            reveal: false,
        }
    }

    /// What the header says after the title while the body is folded away, so
    /// that a folded card still tells its entry from the next one.
    pub fn summary(mut self, summary: impl Into<String>) -> Self {
        self.summary = summary.into();
        self
    }

    /// Start folded, for an entry whose header already says most of it.
    pub fn folded(mut self) -> Self {
        self.open = false;
        self
    }

    /// Put the toggle that keeps the entry drawn in the view in the header.
    pub fn pin(mut self, pinned: &'a mut bool) -> Self {
        self.pin = Some(pinned);
        self
    }

    /// End the header in a × that removes the entry.
    pub fn removable(mut self) -> Self {
        self.removable = true;
        self
    }

    /// Outline the card in the selection's colour: its entry is the one picked
    /// in the graph.
    pub fn picked(mut self, picked: bool) -> Self {
        self.picked = picked;
        self
    }

    /// Open the card and scroll it into view, as far as it needs to go and no
    /// further: what picking its entry in the graph does. Asked for on every
    /// frame until the card has finished unfolding, since until then its
    /// height, and so how far to scroll, is still changing.
    pub fn reveal(mut self, reveal: bool) -> Self {
        self.reveal = reveal;
        self
    }

    /// Draw the card: `title` in the header, taking whatever width is left to
    /// it, and `body` under it while the card is open.
    ///
    /// Both are handed `cx`, the state the controls in them edit. The two
    /// cannot each capture it, since both are alive at once.
    pub fn show<C>(
        self,
        ui: &mut egui::Ui,
        cx: &mut C,
        title: impl FnOnce(&mut egui::Ui, &mut C),
        body: impl FnOnce(&mut egui::Ui, &mut C),
    ) -> Shown {
        let Card { id, icon, summary, open, pin, removable, picked, reveal } = self;
        let colors = icon.role().map(Role::colors);
        ui.push_id(id, |ui| {
            let mut fold = CollapsingState::load_with_default_open(ui.ctx(), ui.make_persistent_id("fold"), open);
            if reveal {
                fold.set_open(true);
            }
            // Begun rather than shown, so the border can be settled after the
            // contents: it lights up while the pointer is over the card.
            let mut card = egui::Frame::group(ui.style()).fill(fill(ui.visuals())).begin(ui);
            let inner = &mut card.content_ui;
            // The panel's width rather than the contents', so one card's
            // controls line up with the next one's.
            inner.set_min_width(inner.available_width());

            let mut removed = false;
            inner.horizontal(|ui| {
                fold.show_toggle_button(ui, paint_default_icon);
                icon.show(ui);
                // Right to left: the buttons keep the far edge, and the title
                // spreads over whatever is left between them.
                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                    removed = removable && cross(ui, "remove").clicked();
                    if let Some(pinned) = pin {
                        ui.toggle_value(pinned, "👁").on_hover_text("keep drawn in the view");
                    }
                    if !fold.is_open() && !summary.is_empty() {
                        // Weak text is weak against the panel; on a tint it
                        // fades, so a node's summary is written in full.
                        show_summary(ui, &summary, colors.is_some());
                    }
                    title(ui, cx);
                });
            });
            fold.show_body_unindented(inner, |ui| body(ui, cx));

            let response = card.allocate_space(ui);
            if reveal {
                response.scroll_to_me(None);
            }
            if let Some((node_fill, _)) = colors {
                card.frame.fill = fill(ui.visuals()).lerp_to_gamma(node_fill, TINT);
            }
            if picked {
                card.frame.stroke = ui.visuals().selection.stroke;
            } else if response.contains_pointer() {
                card.frame.stroke = ui.visuals().widgets.hovered.bg_stroke;
            }
            card.paint(ui);
            // Over the frame and inside its border, so an outline — picked,
            // hovered — goes round the stripe rather than over it.
            if let Some((_, node_border)) = colors {
                let radius = ui.visuals().widgets.noninteractive.corner_radius;
                let rect = response.rect.shrink(1.0);
                let stripe = egui::Rect::from_min_max(rect.min, egui::pos2(rect.min.x + STRIPE, rect.max.y));
                let corners = egui::CornerRadius { nw: radius.nw, sw: radius.sw, ne: 0, se: 0 };
                ui.painter().rect_filled(stripe, corners, node_border);
            }
            Shown { response, removed }
        })
        .inner
    }
}

impl Icon {
    /// The kind of node the entry is, for one that is a node.
    fn role(&self) -> Option<Role> {
        match self {
            Icon::Node(role) | Icon::Picture(role, _) => Some(*role),
            Icon::Glyph(_) => None,
        }
    }

    fn show(self, ui: &mut egui::Ui) {
        let side = ui.spacing().interact_size.y;
        let glyph = |ui: &mut egui::Ui, text: egui::RichText| {
            ui.add_sized([side, side], egui::Label::new(text).selectable(false));
        };
        match self {
            Icon::Glyph(text) => glyph(ui, egui::RichText::new(text).weak()),
            Icon::Node(role) => glyph(ui, egui::RichText::new(role.glyph()).color(Color32::WHITE)),
            Icon::Picture(_, texture) => {
                ui.add(egui::Image::new(egui::load::SizedTexture::new(texture, [side, side])));
            }
        }
    }
}

/// The summary of a folded card, after its title in a right-to-left header:
/// weak, unless `full`.
///
/// Half of what is left, so a long one cannot squeeze the title out, and laid
/// out left to right inside that, so it reads on straight from the title
/// rather than from the far edge.
fn show_summary(ui: &mut egui::Ui, summary: &str, full: bool) {
    let width = (ui.available_width() / 2.0 - ui.spacing().item_spacing.x).max(0.0);
    let size = egui::vec2(width, ui.spacing().interact_size.y);
    ui.allocate_ui_with_layout(size, egui::Layout::left_to_right(egui::Align::Center), |ui| {
        let text = egui::RichText::new(summary);
        let text = if full { text } else { text.weak() };
        ui.add(egui::Label::new(text).truncate());
    });
}

/// A × that takes a frame only while it is hovered, like the eye beside it.
/// Sized up to the eye: the font's × is drawn at the height of a lowercase x.
pub fn cross(ui: &mut egui::Ui, hover: &str) -> egui::Response {
    let cross = egui::RichText::new("×").size(ui.text_style_height(&egui::TextStyle::Body) * 1.3);
    ui.add(egui::Button::new(cross).frame_when_inactive(false)).on_hover_text(hover)
}

/// A shade lifted off the panel, as Blender lifts a modifier off the editor
/// behind it, so a card reads as one unit without a heavy border.
pub fn fill(visuals: &egui::Visuals) -> egui::Color32 {
    visuals.panel_fill.lerp_to_gamma(visuals.widgets.inactive.weak_bg_fill, 0.3)
}

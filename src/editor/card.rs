//! The card every entry of the spec is drawn as, after Blender's modifier
//! stack: a bordered unit whose header names the entry, and whose body — the
//! entry's settings — folds away under it.
//!
//! The header reads left to right: the fold arrow, an icon for what sort of
//! entry it is, the title — the entry's id, or a relation's kind — and at the
//! far edge the entry's toggle and the × that removes it. Every section draws
//! its entries through here, so one added later looks and behaves like the
//! rest: it says what goes in the title and in the body, and nothing else.

use bevy_egui::egui;
use egui::collapsing_header::{CollapsingState, paint_default_icon};

/// What sort of entry a card holds, drawn left of its title.
pub enum Icon {
    /// A glyph from egui's bundled fonts, and only those: a glyph they lack
    /// draws as a box.
    Glyph(&'static str),
    /// The picture of the asset the entry names.
    Picture(egui::TextureId),
}

/// One entry of the spec, drawn by [`Card::show`].
pub struct Card<'a> {
    id: egui::Id,
    icon: Icon,
    summary: String,
    open: bool,
    pin: Option<&'a mut bool>,
    removable: bool,
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
        Self { id: egui::Id::new(id), icon, summary: String::new(), open: true, pin: None, removable: false }
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
        let Card { id, icon, summary, open, pin, removable } = self;
        ui.push_id(id, |ui| {
            let mut fold = CollapsingState::load_with_default_open(ui.ctx(), ui.make_persistent_id("fold"), open);
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
                        show_summary(ui, &summary);
                    }
                    title(ui, cx);
                });
            });
            fold.show_body_unindented(inner, |ui| body(ui, cx));

            let response = card.allocate_space(ui);
            if response.contains_pointer() {
                card.frame.stroke = ui.visuals().widgets.hovered.bg_stroke;
            }
            card.paint(ui);
            Shown { response, removed }
        })
        .inner
    }
}

impl Icon {
    fn show(self, ui: &mut egui::Ui) {
        let side = ui.spacing().interact_size.y;
        match self {
            Icon::Glyph(glyph) => {
                let glyph = egui::Label::new(egui::RichText::new(glyph).weak()).selectable(false);
                ui.add_sized([side, side], glyph);
            }
            Icon::Picture(texture) => {
                ui.add(egui::Image::new(egui::load::SizedTexture::new(texture, [side, side])));
            }
        }
    }
}

/// The summary of a folded card, after its title in a right-to-left header.
///
/// Half of what is left, so a long one cannot squeeze the title out, and laid
/// out left to right inside that, so it reads on straight from the title
/// rather than from the far edge.
fn show_summary(ui: &mut egui::Ui, summary: &str) {
    let width = (ui.available_width() / 2.0 - ui.spacing().item_spacing.x).max(0.0);
    let size = egui::vec2(width, ui.spacing().interact_size.y);
    ui.allocate_ui_with_layout(size, egui::Layout::left_to_right(egui::Align::Center), |ui| {
        ui.add(egui::Label::new(egui::RichText::new(summary).weak()).truncate());
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

//! The dock along the foot of the 3D view, beside the panel, the way Isaac Sim
//! docks its content browser: a tab for the asset browser ([`super::browser`])
//! and one for the spatial graph ([`super::graph`]).
//!
//! The header is the fold, the tabs, and whatever the open tab puts at the far
//! end. Folded, the dock is its header alone, and a tab clicked there opens it
//! on that tab.

use std::collections::BTreeSet;

use bevy_egui::egui;

use super::Form;
use super::browser::{self, Browser};
use super::graph::{self, Graph, Item};
use super::thumbnails::Pictures;

/// How tall the dock opens. Draggable from there, and folded away by its
/// header or by dragging its edge down to the window's foot.
const HEIGHT: f32 = 280.0;

/// What the dock remembers between frames. None of it is in the spec.
pub struct Dock {
    /// Whether the dock is open, or folded down to its header.
    open: bool,
    tab: Tab,
    pub browser: Browser,
    pub graph: Graph,
}

#[derive(Clone, Copy, PartialEq)]
enum Tab {
    Assets,
    Graph,
}

/// `ARENA_VIEWER_DOCK=graph` opens the dock on the graph, for a screenshot.
impl Default for Dock {
    fn default() -> Self {
        let tab = match std::env::var("ARENA_VIEWER_DOCK").as_deref() {
            Ok("graph") => Tab::Graph,
            _ => Tab::Assets,
        };
        Self { open: true, tab, browser: Browser::default(), graph: Graph::default() }
    }
}

/// What the dock reports once it is drawn.
pub struct Docked {
    /// Where it landed.
    pub rect: egui::Rect,
    /// What the pointer is over in the graph, for the view to show.
    pub hovered: Option<Item>,
}

/// Draw the dock along the foot of `ui`. `lit` is the relations the view is
/// drawing, which the graph lights as well.
pub fn dock(
    ui: &mut egui::Ui,
    form: &Form,
    state: &mut Dock,
    lit: &BTreeSet<usize>,
    pictures: &mut Pictures,
) -> Docked {
    let mut hovered = None;
    // The two are separate panels, so each keeps its own size. The open one
    // stops at its least height, and folds only once its edge is dragged on
    // down to the folded one's.
    let folded = egui::Panel::bottom("dock folded").resizable(false);
    let open = egui::Panel::bottom("dock").resizable(true).default_size(HEIGHT).min_size(140.0);
    let mut is_open = state.open;
    let shown = egui::Panel::show_switched(ui, &mut is_open, folded, open, |ui, open| {
        let mut listed = Vec::new();
        let toggled = ui
            .horizontal(|ui| {
                let toggled = tabs(ui, state, open);
                if open {
                    ui.separator();
                    match state.tab {
                        Tab::Assets => {
                            // Listed only while the browser shows: it is a
                            // couple of thousand assets. After the tabs, so a
                            // tab picked this frame shows its list at once.
                            listed = browser::listed(form);
                            browser::header(ui, form, &mut state.browser, listed.len());
                        }
                        Tab::Graph => graph::header(ui, &mut state.graph),
                    }
                }
                toggled
            })
            .inner;
        if open {
            ui.separator();
            match state.tab {
                Tab::Assets => browser::body(ui, form, &mut state.browser, &listed, pictures),
                Tab::Graph => hovered = graph::show(ui, form, &mut state.graph, lit, pictures),
            }
        }
        toggled
    });
    state.open = is_open != shown.inner;
    Docked { rect: shown.response.rect, hovered }
}

/// The start of the dock's header: the fold, and the tabs. Says whether the
/// dock is to fold or unfold.
fn tabs(ui: &mut egui::Ui, state: &mut Dock, open: bool) -> bool {
    let (glyph, hover) = match open {
        true => ("⏷", "fold the dock away"),
        false => ("⏵", "open the dock"),
    };
    let mut toggled = ui.add(egui::Button::new(glyph).frame(false)).on_hover_text(hover).clicked();
    for (tab, name) in [(Tab::Assets, "assets"), (Tab::Graph, "graph")] {
        if ui.selectable_label(open && state.tab == tab, egui::RichText::new(name).strong()).clicked() {
            // A tab picked while folded opens the dock on it.
            toggled |= !open;
            state.tab = tab;
        }
    }
    toggled
}

//! What the panel shows: a header for the layouts a solve drew, and under it a
//! section per part of the spec, each entry in it a [`Card`]. The dock under
//! the 3D view beside it holds the asset browser and the spatial graph; see
//! [`super::dock`].
//!
//! A section says what an entry's card holds — its icon, its title, its body —
//! and the card and the controls in it do the rest; see [`super::card`] and
//! [`super::controls`].

use std::collections::{BTreeSet, HashMap};

use bevy::prelude::*;
use bevy_egui::{EguiContexts, egui};
use bevy_panorbit_camera::PanOrbitCamera;

use super::browser;
use super::card::{Card, FAILED, Icon, Role, cross};
use super::controls::{id_options, insert, menu, params, row, set, text_field, toggle, with};
use super::dock::{self, Dock};
use super::graph::{Item, Pick};
use super::relations::{self, RelationGizmos};
use super::thumbnails::{Files, Images, Pictures, Thumbnails};
use super::{Form, PointerOverPanel, in_the_view, send};
use crate::layout::Placed;
use crate::scene::Layout;
use crate::spec::{Kind, Relation, Request, Step, Value};

/// How wide the panel opens. Draggable from there.
const WIDTH: f32 = 360.0;

/// How long after a pick in the graph the panel goes on scrolling its card
/// into view: long enough for the section and the card to finish unfolding,
/// and short enough that a scroll of one's own soon wins.
const REVEAL: f64 = 0.5;

/// Everything the panel remembers that is not in the spec.
pub struct PanelState {
    /// The dock under the view: which tab is open, the asset browser's search,
    /// folder and pick, and the graph's view and pick.
    pub(super) dock: Dock,
    /// The entry picked in the graph, by its path in this frame's spec, and
    /// whether it is still being scrolled into view. Taken from the graph's
    /// pick as each frame's panel begins; see [`PanelState::picked`].
    pick: Option<Vec<Step>>,
    revealing: bool,
    /// The number being dragged or typed into. One, because only one control
    /// can be under the pointer at a time.
    pub(super) editing: Option<Editing>,
    /// The text typed into each box not yet sent, by where in the spec it
    /// belongs. Only one box holds the caret, but two hold text for a frame
    /// when the caret moves from one to the other: the box it left sends its
    /// text only once it hears it has lost it.
    pub(super) typing: HashMap<Vec<Step>, String>,
    /// The entry whose × was clicked this frame, removed once everything else
    /// the frame sends has gone. A control that held the focus sends its edit
    /// as the click takes the focus away, and that edit names its entry by
    /// position: removed first, the entry after would take the position, and
    /// the edit with it.
    removing: Option<Vec<Step>>,
    /// The spec `editing` and `typing` were made against.
    revision: u64,
    /// Relations kept drawn in the view after the pointer has left them, by
    /// position in the list. Removing one above a pin shifts the pin.
    pinned: BTreeSet<usize>,
    /// Draw every relation, pointer or not.
    show_all: bool,
    /// An entry to pick in the graph as the first spec lands, as though it had
    /// been clicked there. Not before: that spec's arrival drops any pick.
    pick_on_open: Option<Item>,
}

impl PanelState {
    /// Whether the entry at `path` is the one picked in the graph.
    fn picked(&self, path: &[Step]) -> bool {
        self.pick.as_deref() == Some(path)
    }

    /// Whether the entry at `path` is the one picked in the graph, or holds
    /// it, while it is still being brought into view: a section or card to
    /// unfold, and the card to scroll to.
    fn revealing(&self, path: &[Step]) -> bool {
        self.revealing && self.pick.as_ref().is_some_and(|pick| pick.starts_with(path))
    }
}

/// A number being edited, before the spec carries it.
pub struct Editing {
    /// Where in the spec it belongs, so the view can draw the value before the
    /// spec comes back carrying it.
    pub path: Vec<Step>,
    pub value: f64,
    /// Whether it has gone to Python. A sent edit is kept until the spec comes
    /// back, so the number does not snap back to the old value meanwhile, and
    /// is not sent again.
    pub sent: bool,
}

/// `ARENA_VIEWER_SHOW_RELATIONS=all`, or `=2,5` for those positions in the
/// list, starts with those relations drawn: the way to see them in a
/// screenshot, where there is nothing to hover. `ARENA_VIEWER_PICK=bowl`, or
/// `=3` for the relation at that position, opens with that entry picked in the
/// graph and its card open, where there is nothing to click.
impl Default for PanelState {
    fn default() -> Self {
        let shown = std::env::var("ARENA_VIEWER_SHOW_RELATIONS").unwrap_or_default();
        let pick = std::env::var("ARENA_VIEWER_PICK").unwrap_or_default();
        let pick_on_open = match pick.trim() {
            "" => None,
            id => Some(id.parse().map_or_else(|_| Item::Node(id.to_owned()), Item::Relation)),
        };
        Self {
            dock: Dock::default(),
            pick: None,
            revealing: false,
            editing: None,
            typing: HashMap::new(),
            removing: None,
            revision: 0,
            pinned: shown.split(',').filter_map(|index| index.trim().parse().ok()).collect(),
            show_all: matches!(shown.trim(), "1" | "all"),
            pick_on_open,
        }
    }
}

#[allow(clippy::too_many_arguments)]
pub fn draw(
    mut contexts: EguiContexts,
    mut form: ResMut<Form>,
    placed: Res<Placed>,
    files: Files,
    mut gizmos: Gizmos<RelationGizmos>,
    mut state: Local<PanelState>,
    // The pictures on hand, for the browser's tiles, the objects' cards and
    // their nodes in the graph.
    mut thumbnails: Local<Thumbnails>,
    mut over_panel: ResMut<PointerOverPanel>,
    camera: Option<Single<(&mut Camera, &mut PanOrbitCamera)>>,
) -> Result {
    // Cloned, so `contexts` is free to be handed on to the pictures — which
    // need it for egui's texture table. An `egui::Context` is a handle.
    let ctx = contexts.ctx_mut()?.clone();
    // A panel needs a `Ui` to dock into, and the context's own root one is not
    // handed out — so this is the whole window, as a `Ui`.
    let mut window = egui::Ui::new(
        ctx.clone(),
        "viewport".into(),
        egui::UiBuilder::new()
            .layer_id(egui::LayerId::background())
            .max_rect(ctx.viewport_rect()),
    );
    let mut images = Images::new(&files, &mut contexts);
    let mut turned = None;
    let shown = egui::Panel::right("spec").default_size(WIDTH).min_size(240.0).show(&mut window, |ui| {
        turned = header(ui, &form);
        panel(ui, &form, &mut state, &mut |path| thumbnails.get(path, &mut images))
    });
    if let Some(page) = turned {
        form.page = page;
    }
    // After the panel, so the dock spans the view alone and the panel keeps
    // the window's full height, as Isaac Sim lays out its stage and content
    // browser. A pick in its graph reaches the panel on the next frame.
    let pictures = &mut |path: &str| thumbnails.get(path, &mut images);
    let docked = dock::dock(&mut window, &form, &mut state.dock, &shown.inner, pictures);
    thumbnails.evict(&mut images);
    // Measured here because this is the only place the panels' rects exist;
    // see [`PointerOverPanel`] for why egui cannot be asked instead.
    over_panel.0 = ctx
        .pointer_interact_pos()
        .is_some_and(|pointer| shown.response.rect.contains(pointer) || docked.rect.contains(pointer));
    // What the two leave of the window, which the layout is framed in rather
    // than behind them, following their edges as they are dragged.
    let view = window.available_rect_before_wrap();
    if let Some(mut camera) = camera {
        let (camera, orbit) = &mut *camera;
        camera.sub_camera_view = Some(in_the_view(ctx.viewport_rect(), view));
        // The orbit camera pans a window's height of field of view per window
        // height dragged, and the view now spreads that field over its own
        // height: scaled back up, the layout keeps pace with the pointer.
        let pace = ctx.viewport_rect().height() / view.height().max(1.0);
        if orbit.pan_sensitivity != pace {
            orbit.pan_sensitivity = pace;
        }
    }

    // The relations the panel and the graph want seen, drawn over the layout.
    // From here rather than a system of its own: the hover is this frame's,
    // and a gizmo issued in egui's pass is collected with every other at the
    // frame's end.
    let mut drawn = shown.inner;
    match docked.hovered {
        Some(Item::Relation(index)) => {
            drawn.insert(index);
        }
        Some(Item::Node(id)) => {
            if let Some(asset) = relations::find(&placed, &id) {
                relations::outline(asset, &mut gizmos);
            }
        }
        None => {}
    }
    for &index in &drawn {
        if let Some(relation) = form.spec.relations.get(index) {
            // Whatever is being dragged right now stands in for what the spec
            // says, so the view follows the number rather than the answer to it.
            let dragged = pending(relation, index, &state);
            let relation = dragged.as_ref().unwrap_or(relation);
            relations::draw(relation, form.vocabulary.relation(&relation.kind), &placed, &mut gizmos);
        }
    }

    // The view's corner above the dock and beside the panel.
    if let Some(closed) = toasts(&ctx, view, &form.errors) {
        form.errors.remove(closed);
    }
    Ok(())
}

/// What a layout that passes is said in. One that fails is said as an error
/// is, in [`FAILED`].
const PASSED: egui::Color32 = egui::Color32::from_rgb(130, 200, 140);

/// The head of the panel, over the spec: which of the layouts the last solve
/// drew the view shows, what the checks made of it, and the button that solves
/// again from a new seed. Returns the page the pager turned to.
///
/// While a solve is running a spinner takes the button's place, as it takes
/// the reindex button's: the view beside the panel is the half behind, since
/// an accepted edit reaches the form at once and the layout only once the
/// solver has caught up. The pager still turns the layouts on screen.
fn header(ui: &mut egui::Ui, form: &Form) -> Option<usize> {
    let mut turned = None;
    ui.horizontal(|ui| {
        // Right to left first, so the button keeps the far edge and the pager
        // and its verdict take what is left from the near one.
        ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
            if form.solving {
                ui.add(spinner()).on_hover_text("solving: the view catches up when the layouts land");
                ui.weak("solving…");
            } else {
                if ui.button("⟳").on_hover_text("solve again from a new seed").clicked() {
                    send(Request::Regenerate);
                }
                if let Some(seed) = form.seed {
                    ui.weak(format!("seed {seed}"))
                        .on_hover_text(format!("drawn from seed {seed}: arena-view --seed {seed} draws them again"));
                }
            }
            ui.with_layout(egui::Layout::left_to_right(egui::Align::Center), |ui| {
                let count = form.layouts.len().max(1);
                let page = form.page.min(count - 1);
                let back = ui.add_enabled(page > 0, egui::Button::new("⏴").frame(false));
                if back.on_hover_text("the layout before").clicked() {
                    turned = Some(page - 1);
                }
                ui.label(format!("{} / {count}", page + 1))
                    .on_hover_text(format!("layout {} of the {count} the solve drew, one per env", page + 1));
                let next = ui.add_enabled(page + 1 < count, egui::Button::new("⏵").frame(false));
                if next.on_hover_text("the next layout").clicked() {
                    turned = Some(page + 1);
                }
                verdict(ui, form.layouts.get(page));
            });
        });
    });
    ui.separator();
    turned
}

/// What the checks a dry solve runs made of the layout on screen.
fn verdict(ui: &mut egui::Ui, layout: Option<&Layout>) {
    let Some(layout) = layout else { return };
    match layout.success {
        Some(true) => {
            ui.colored_label(PASSED, "✔ valid")
                .on_hover_text("passes every check a dry solve runs: settling and the robot's reach are the sim's");
        }
        Some(false) => {
            let failed = layout.failed_checks.join(", ");
            let text = egui::RichText::new(format!("⚠ fails {failed}")).color(FAILED);
            ui.add(egui::Label::new(text).truncate()).on_hover_text(format!("fails {failed}"));
        }
        None => {
            ui.weak("nothing to place").on_hover_text("no relation places anything, so nothing was solved");
        }
    }
}

/// How wide an error toast is, where the view has the room.
const TOAST_WIDTH: f32 = 340.0;

/// How tall an error's text grows before it scrolls: a Python exception can
/// run to a page.
const TOAST_TEXT_HEIGHT: f32 = 160.0;

/// The errors not yet closed, a toast each in the view's bottom-right corner,
/// the newest nearest it. Returns the one whose × was clicked.
///
/// Closed by hand alone. A toast gone on a timer, or with the next message,
/// could go unread — and the next message is often only the solve catching up.
/// The text can be selected, and the copy button takes all of it.
///
/// An `Area` rather than a child of the window's `Ui`: egui hit-tests what is
/// above the background layer, so the camera gate gives it the pointer, and a
/// drag across the text selects rather than orbits.
fn toasts(ctx: &egui::Context, view: egui::Rect, errors: &[String]) -> Option<usize> {
    let margin = 12.0;
    let width = TOAST_WIDTH.min(view.width() - 4.0 * margin).max(80.0);
    let mut closed = None;
    egui::Area::new(egui::Id::new("toasts"))
        .order(egui::Order::Foreground)
        .pivot(egui::Align2::RIGHT_BOTTOM)
        .fixed_pos(view.right_bottom() - egui::vec2(margin, margin))
        .show(ctx, |ui| {
            for (index, error) in errors.iter().enumerate() {
                egui::Frame::popup(ui.style()).show(ui, |ui| {
                    ui.set_width(width);
                    ui.horizontal(|ui| {
                        ui.colored_label(FAILED, "⚠ error");
                        ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                            if cross(ui, "close").clicked() {
                                closed = Some(index);
                            }
                            if ui.small_button("🗐").on_hover_text("copy").clicked() {
                                ui.ctx().copy_text(error.clone());
                            }
                        });
                    });
                    egui::ScrollArea::vertical()
                        .id_salt(error)
                        .max_height(TOAST_TEXT_HEIGHT)
                        .show(ui, |ui| ui.label(error));
                });
            }
        });
    closed
}

/// What says a layout is still being solved. Sized down from the default, which
/// is a line high and reads as a widget rather than a status.
fn spinner() -> egui::Spinner {
    egui::Spinner::new().size(16.0)
}

/// Draw the panel, and say which relations it wants drawn in the view.
fn panel(ui: &mut egui::Ui, form: &Form, state: &mut PanelState, pictures: &mut Pictures) -> BTreeSet<usize> {
    // A half-made edit belongs to the spec it was made against, and so does a
    // pick in the graph: it names a relation by position and a node by id, and
    // an edit can hand either to another entry.
    if state.revision != form.revision {
        state.revision = form.revision;
        state.editing = None;
        state.typing.clear();
        state.dock.graph.picked = None;
    }
    // Dated a second ahead, so it goes on revealing its card for that much
    // longer: the window's first frames are slow while the scene uploads, and a
    // reveal that ends within them leaves the panel scrolled to the wrong place.
    if let Some(item) = state.pick_on_open.take() {
        state.dock.graph.picked = Some(Pick { item, at: ui.input(|input| input.time) + 1.0 });
    }
    let pick = state.dock.graph.picked.as_ref();
    state.pick = pick.and_then(|pick| pick.item.path(&form.spec));
    state.revealing = pick.is_some_and(|pick| ui.input(|input| input.time) - pick.at < REVEAL);

    let shown = egui::ScrollArea::vertical()
        .auto_shrink(false)
        .show(ui, |ui| {
            embodiment(ui, form, state);
            background(ui, form, state);
            objects(ui, form, state, pictures);
            object_sets(ui, form, state);
            object_references(ui, form, state);
            let shown = relations(ui, form, state);
            task(ui, form, state);
            shown
        })
        .inner;

    // Last, after every edit the frame's other controls sent.
    if let Some(path) = state.removing.take() {
        send(Request::Remove { path });
    }
    shown
}

/// The robot, picked from the registered embodiments.
///
/// A pick sets the registry name alone. The id stays, because relations, task
/// params and CLI overrides name the robot by it; the old robot's params are
/// Python's to sort, since only it can read what the new one's constructor
/// takes.
fn embodiment(ui: &mut egui::Ui, form: &Form, state: &mut PanelState) {
    let node = &form.spec.embodiment;
    let path = [Step::from("embodiment")];
    section(ui, "embodiment", None, form.owner("embodiment"), state.revealing(&path), |ui| {
        let card = Card::new(&path, Icon::Node(Role::Embodiment)).summary(&node.registry_name).folded();
        let card = card.problems(form.problems_at(&path));
        card.picked(state.picked(&path)).reveal(state.revealing(&path)).show(
            ui,
            state,
            |ui, state| {
                text_field(ui, &with(&path, "id"), &node.id, state);
            },
            |ui, _| {
                row(ui, "robot", None, |ui| {
                    let options = registry_options(form, "embodiment");
                    let width = ui.available_width();
                    menu(ui, "registry_name", &node.registry_name, options, set(&with(&path, "registry_name")), width);
                });
            },
        );
    });
}

/// What the objects stand in or on, picked from the registered backgrounds. A
/// pick sets the registry name alone, as for the robot, and for the same
/// reason.
fn background(ui: &mut egui::Ui, form: &Form, state: &mut PanelState) {
    let node = &form.spec.background;
    let path = [Step::from("background")];
    section(ui, "background", None, form.owner("background"), state.revealing(&path), |ui| {
        let card = Card::new(&path, Icon::Node(Role::Background)).summary(&node.registry_name).folded();
        let card = card.problems(form.problems_at(&path));
        card.picked(state.picked(&path)).reveal(state.revealing(&path)).show(
            ui,
            state,
            |ui, state| {
                text_field(ui, &with(&path, "id"), &node.id, state);
            },
            |ui, _| {
                row(ui, "asset", None, |ui| {
                    let options = registry_options(form, "background");
                    let width = ui.available_width();
                    menu(ui, "registry_name", &node.registry_name, options, set(&with(&path, "registry_name")), width);
                });
            },
        );
    });
}

/// Every registered asset in `role`, as the registry name a pick writes.
fn registry_options(form: &Form, role: &str) -> Vec<(String, Value)> {
    form.vocabulary
        .assets
        .iter()
        .filter(|asset| asset.role == role)
        .map(|asset| (asset.registry_name.clone(), Value::text(&asset.registry_name)))
        .collect()
}

/// The objects, each titled by its id and pictured by its asset. Folded, since
/// the header says what there is to say: the body holds only the asset.
fn objects(ui: &mut egui::Ui, form: &Form, state: &mut PanelState, pictures: &mut Pictures) {
    let revealing = state.revealing(&[Step::from("objects")]);
    section(ui, "objects", Some(form.spec.objects.len()), form.owner("objects"), revealing, |ui| {
        // No add button: an object is an asset somebody offers, and the
        // browser under the view is where those are. Picking there is what
        // makes "add" a decision rather than a surprise.
        ui.weak("add from the asset browser under the view");
        for (index, node) in form.spec.objects.iter().enumerate() {
            let path = vec![Step::from("objects"), Step::from(index)];
            let asset = form.asset(node);
            let name = asset.map_or(node.registry_name.as_str(), |asset| asset.name.as_str());
            let icon = asset
                .and_then(|asset| pictures(&asset.thumbnail))
                .map_or(Icon::Node(Role::Object), |picture| Icon::Picture(Role::Object, picture));
            let card = Card::new(&path, icon).summary(name).folded().removable().problems(form.problems_at(&path));
            let card = card.picked(state.picked(&path)).reveal(state.revealing(&path)).show(
                ui,
                state,
                |ui, state| {
                    text_field(ui, &with(&path, "id"), &node.id, state);
                },
                |ui, _| {
                    row(ui, "asset", None, |ui| {
                        let label = ui.add(egui::Label::new(name).truncate());
                        if let Some(asset) = asset {
                            label.on_hover_text(browser::hover(asset));
                        }
                    });
                },
            );
            if card.removed {
                state.removing = Some(path);
            }
        }
    });
}

/// The object sets, where a spec has any: one id standing for whichever of its
/// members is picked.
fn object_sets(ui: &mut egui::Ui, form: &Form, state: &mut PanelState) {
    let sets = &form.spec.object_sets;
    if sets.is_empty() {
        return;
    }
    let revealing = state.revealing(&[Step::from("object_sets")]);
    section(ui, "object sets", Some(sets.len()), form.owner("object_sets"), revealing, |ui| {
        for (index, entry) in sets.iter().enumerate() {
            let path = vec![Step::from("object_sets"), Step::from(index)];
            let members = entry.members.join(", ");
            let card = Card::new(&path, Icon::Node(Role::ObjectSet)).summary(&members).removable();
            let card = card.problems(form.problems_at(&path));
            let card = card.picked(state.picked(&path)).reveal(state.revealing(&path)).show(
                ui,
                state,
                |ui, state| {
                    text_field(ui, &with(&path, "id"), &entry.id, state);
                },
                |ui, _| {
                    row(ui, "members", None, |ui| {
                        ui.add(egui::Label::new(&members).truncate());
                    });
                    row(ui, "random_choice", None, |ui| {
                        toggle(ui, entry.random_choice, &with(&path, "random_choice"));
                    });
                },
            );
            if card.removed {
                state.removing = Some(path);
            }
        }
    });
}

/// The object references, where a spec has any: a part of another node — a
/// counter's top, a drawer — given an id of its own to be stood on or opened.
fn object_references(ui: &mut egui::Ui, form: &Form, state: &mut PanelState) {
    let references = &form.spec.object_references;
    if references.is_empty() {
        return;
    }
    let ids = form.spec.node_ids();
    let revealing = state.revealing(&[Step::from("object_references")]);
    let owner = form.owner("object_references");
    section(ui, "object references", Some(references.len()), owner, revealing, |ui| {
        for (index, entry) in references.iter().enumerate() {
            let path = vec![Step::from("object_references"), Step::from(index)];
            let card = Card::new(&path, Icon::Node(Role::ObjectReference)).summary(&entry.parent_id).removable();
            let card = card.problems(form.problems_at(&path));
            let card = card.picked(state.picked(&path)).reveal(state.revealing(&path)).show(
                ui,
                state,
                |ui, state| {
                    text_field(ui, &with(&path, "id"), &entry.id, state);
                },
                |ui, state| {
                    row(ui, "parent", None, |ui| {
                        let (options, width) = (id_options(&ids), ui.available_width());
                        menu(ui, "parent_id", &entry.parent_id, options, set(&with(&path, "parent_id")), width);
                    });
                    row(ui, "prim_path", None, |ui| {
                        let prim_path = entry.prim_path.as_deref().unwrap_or_default();
                        text_field(ui, &with(&path, "prim_path"), prim_path, state);
                    });
                    row(ui, "object_type", None, |ui| {
                        ui.label(&entry.object_type);
                    });
                },
            );
            if card.removed {
                state.removing = Some(path);
            }
        }
    });
}

/// The relations, each with a toggle to keep it drawn. Returns the ones to
/// draw this frame: hovered, pinned, or every one when asked for them all.
fn relations(ui: &mut egui::Ui, form: &Form, state: &mut PanelState) -> BTreeSet<usize> {
    let ids = form.spec.node_ids();
    let mut shown = BTreeSet::new();
    let revealing = state.revealing(&[Step::from("relations")]);
    section(ui, "relations", Some(form.spec.relations.len()), form.owner("relations"), revealing, |ui| {
        let options = relation_options(form, subject_default(form), &form.spec.background.id, &ids);
        add_menu(ui, "+ relation", options, &[Step::from("relations")]);

        for (index, relation) in form.spec.relations.iter().enumerate() {
            let path = vec![Step::from("relations"), Step::from(index)];
            let kind = form.vocabulary.relation(&relation.kind);
            let reference = relation.reference.clone().unwrap_or_default();
            // A unary relation takes no reference, and Arena refuses one that
            // has it — so the slot is not drawn rather than validated.
            let binary = kind.is_some_and(|kind| kind.unary == Some(false));
            let summary = match binary {
                // `»` and not `→`: egui ships a font without an arrow in it,
                // and a missing glyph draws as a box.
                true => format!("{} » {reference}", relation.subject),
                false => relation.subject.clone(),
            };

            let mut pinned = state.pinned.contains(&index);
            let (picked, revealing) = (state.picked(&path), state.revealing(&path));
            let card = Card::new(&path, Icon::Glyph("🔗")).summary(summary).pin(&mut pinned).removable();
            let card = card.problems(form.problems_at(&path)).picked(picked).reveal(revealing).show(
                ui,
                state,
                |ui, _| {
                    // Changing the kind rewrites the whole entry: the old
                    // kind's parameters mean nothing to the new one, and
                    // whether there is a `reference` at all is the kind's to
                    // say.
                    let options = relation_options(form, &relation.subject, &reference, &ids);
                    let width = ui.available_width();
                    let picked = menu(ui, "kind", &relation.kind, options, set(&path), width);
                    if let Some(kind) = kind.filter(|kind| !kind.summary.is_empty()) {
                        picked.on_hover_text(&kind.summary);
                    }
                },
                |ui, state| {
                    // The pair shares a line rather than taking one each,
                    // because the two of them read as the relation's direction.
                    ui.horizontal(|ui| {
                        // Half the line each, so the arrow between them falls
                        // in the same place down the panel.
                        let width = match binary {
                            true => ui.available_width() / 2.0 - ui.spacing().item_spacing.x,
                            false => ui.available_width(),
                        };
                        let options = id_options(&ids);
                        menu(ui, "subject", &relation.subject, options, set(&with(&path, "subject")), width)
                            .on_hover_text("subject — the object the relation is about");
                        if binary {
                            ui.weak("»");
                            let width = ui.available_width();
                            let options = id_options(&ids);
                            menu(ui, "reference", &reference, options, set(&with(&path, "reference")), width)
                                .on_hover_text("reference — the object it is placed against");
                        }
                    });
                    params(ui, kind, &relation.params, &ids, &with(&path, "params"), state);
                },
            );
            match pinned {
                true => state.pinned.insert(index),
                false => state.pinned.remove(&index),
            };
            if card.removed {
                state.removing = Some(path);
            }
            // The whole card, controls included: a number being dragged is
            // still the relation being looked at.
            if card.response.contains_pointer() {
                shown.insert(index);
            }
        }
    });
    // Outside the section, so a folded one keeps its pins drawn.
    let count = form.spec.relations.len();
    if state.show_all {
        shown.extend(0..count);
    } else {
        shown.extend(state.pinned.iter().copied().filter(|&index| index < count));
    }
    // A number being dragged keeps its relation drawn however far the pointer
    // has wandered: egui holds a drag after the pointer leaves the control, and
    // the card's hover does not.
    shown.extend(
        state
            .editing
            .as_ref()
            .and_then(|editing| relation_param(&editing.path))
            .map(|(index, _)| index)
            .filter(|&index| index < count),
    );
    shown
}

/// The task: what it is, how its subtasks combine, and a card per subtask
/// inside it.
///
/// A subtask is a relation's twin as far as the form goes — a kind picked from
/// a registry, and the parameters that kind declares — so it is drawn the same
/// way, down to replacing the whole entry when its kind changes.
fn task(ui: &mut egui::Ui, form: &Form, state: &mut PanelState) {
    let task = &form.spec.task;
    let path = [Step::from("task")];
    let subtasks = with(&path, "subtasks");
    let ids = form.spec.node_ids();
    section(ui, "task", None, form.owner("task"), state.revealing(&path), |ui| {
        Card::new(&path, Icon::Glyph("🎯")).summary(&task.composition).problems(form.problems_at(&path)).show(
            ui,
            state,
            |ui, state| {
                text_field(ui, &with(&path, "description"), &task.description, state)
                    .on_hover_text("description — what the task asks of the robot");
            },
            |ui, state| {
                row(ui, "composition", None, |ui| {
                    let options = form.vocabulary.compositions.iter().map(|name| (name.clone(), Value::text(name)));
                    let (options, width) = (options.collect(), ui.available_width());
                    menu(ui, "composition", &task.composition, options, set(&with(&path, "composition")), width)
                        .on_hover_text("how the subtasks combine");
                });
                add_menu(ui, "+ subtask", subtask_options(form, &ids), &subtasks);
                for (index, subtask) in task.subtasks.iter().enumerate() {
                    let path = with(&subtasks, index);
                    let kind = form.vocabulary.task(&subtask.kind);
                    let summary = nodes_named(kind, &subtask.params);
                    let card = Card::new(&path, Icon::Glyph("✔")).summary(summary).removable();
                    let card = card.problems(form.problems_at(&path)).show(
                        ui,
                        state,
                        |ui, _| {
                            let (options, width) = (subtask_options(form, &ids), ui.available_width());
                            let picked = menu(ui, "kind", &subtask.kind, options, set(&path), width);
                            if let Some(kind) = kind.filter(|kind| !kind.summary.is_empty()) {
                                picked.on_hover_text(&kind.summary);
                            }
                        },
                        |ui, state| params(ui, kind, &subtask.params, &ids, &with(&path, "params"), state),
                    );
                    if card.removed {
                        state.removing = Some(path);
                    }
                }
            },
        );
    });
}

/// The nodes a subtask's parameters name, in the order its kind declares them:
/// what it is done to, for the header of a folded card.
fn nodes_named(kind: Option<&Kind>, params: &crate::spec::Params) -> String {
    let Some(kind) = kind else {
        return String::new();
    };
    let named = kind.fields.iter().filter(|field| field.kind == "node_ref");
    let ids: Vec<&str> = named.filter_map(|field| params.get(&field.name)?.as_text()).collect();
    ids.join(", ")
}

/// A section's add button: a menu of whole entries, each already one Arena
/// accepts, inserted at `path`.
fn add_menu(ui: &mut egui::Ui, label: &str, options: Vec<(String, Value)>, path: &[Step]) {
    let width = ui.available_width();
    menu(ui, label, label, options, insert(path), width);
}

/// `relation` as the number being dragged would leave it, when that number is
/// one of its parameters.
///
/// What draws a drag before the spec carries it. Nothing more is sent for it:
/// this is the value the control is already painting, and the spec that answers
/// the end of the drag holds the same one, so the handover draws no step. A
/// refused edit brings the old value back, as it does to the control.
///
/// A path naming anything else is left alone, so a number control somewhere
/// other than a relation — a subtask's — previews nothing rather than the
/// wrong relation.
fn pending(relation: &Relation, index: usize, state: &PanelState) -> Option<Relation> {
    let editing = state.editing.as_ref()?;
    let (at, name) = relation_param(&editing.path)?;
    if at != index {
        return None;
    }
    let mut dragged = relation.clone();
    dragged.params.insert(name.to_owned(), Value::Number(editing.value));
    Some(dragged)
}

/// The relation and the parameter a path addresses, where it addresses one.
fn relation_param(path: &[Step]) -> Option<(usize, &str)> {
    match path {
        [Step::Key(relations), Step::Index(index), Step::Key(params), Step::Key(name)]
            if relations == "relations" && params == "params" =>
        {
            Some((*index, name))
        }
        _ => None,
    }
}

/// One whole relation entry per registered kind, each already valid: the kind's
/// required parameters filled in, and a `reference` only where the kind takes
/// one. Used both to change a relation's kind and to add one.
fn relation_options(form: &Form, subject: &str, reference: &str, ids: &[&str]) -> Vec<(String, Value)> {
    form.vocabulary
        .relations
        .iter()
        .map(|kind| {
            let mut pairs = vec![("kind", Value::text(&kind.name)), ("subject", Value::text(subject))];
            if kind.unary == Some(false) {
                pairs.push(("reference", Value::text(reference)));
            }
            pairs.push(("params", required_params(kind, ids)));
            (kind.name.clone(), Value::map(pairs))
        })
        .collect()
}

/// One whole subtask entry per registered task kind, its required parameters
/// filled in. Used both to change a subtask's kind and to add one.
fn subtask_options(form: &Form, ids: &[&str]) -> Vec<(String, Value)> {
    form.vocabulary
        .tasks
        .iter()
        .map(|kind| {
            let entry = Value::map([("kind", Value::text(&kind.name)), ("params", required_params(kind, ids))]);
            (kind.name.clone(), entry)
        })
        .collect()
}

/// The parameters the kind has no default for. Arena refuses an entry without
/// them, so an added relation or subtask carries a placeholder the form then
/// shows.
fn required_params(kind: &Kind, ids: &[&str]) -> Value {
    Value::map(kind.fields.iter().filter(|field| field.required).map(|field| {
        let value = match field.kind.as_str() {
            "enum" => field.options.first().map_or(Value::Null, Value::text),
            "node_ref" => ids.first().map_or(Value::Null, |id| Value::text(*id)),
            "number" | "int" => Value::Number(0.0),
            "bool" => Value::Bool(false),
            _ => Value::text(""),
        };
        (field.name.as_str(), value)
    }))
}

/// What a new relation is about, before the user says otherwise: the object
/// added last, since adding one is usually what prompts the relation.
fn subject_default(form: &Form) -> &str {
    form.spec
        .objects
        .last()
        .map_or(form.spec.background.id.as_str(), |node| node.id.as_str())
}

/// One top-level key of the spec, with the file that owns it. `count` is for a
/// list; a section holding one entry has none.
///
/// Unindented: the cards are bordered already, and the width an indent would
/// take is what their controls need. `reveal` unfolds it, for an entry in it
/// being picked in the graph.
fn section(
    ui: &mut egui::Ui,
    name: &str,
    count: Option<usize>,
    owner: &str,
    reveal: bool,
    body: impl FnOnce(&mut egui::Ui),
) {
    let count = count.map_or(String::new(), |count| format!("  ({count})"));
    let owner = match owner {
        "" => String::new(),
        owner => format!("  ·  {owner}"),
    };
    egui::CollapsingHeader::new(format!("{name}{count}{owner}"))
        .id_salt(name)
        .default_open(true)
        .open(reveal.then_some(true))
        .show_unindented(ui, body);
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::editor::graph::Pick;
    use crate::spec::{Field, Params, Problem};
    use std::collections::HashMap;

    /// Every kind in the menu writes an entry Arena can build the relation
    /// from: a `reference` exactly where the kind takes one, and the parameters
    /// it has no default for already filled in.
    #[test]
    fn a_kind_menu_offers_entries_the_kind_can_be_built_from() {
        let mut form = Form::default();
        form.vocabulary.relations = vec![
            Kind { name: "is_anchor".into(), unary: Some(true), ..Default::default() },
            Kind {
                name: "next_to".into(),
                unary: Some(false),
                fields: vec![Field {
                    name: "side".into(),
                    kind: "enum".into(),
                    required: true,
                    options: vec!["positive_y".into()],
                    ..Default::default()
                }],
                ..Default::default()
            },
        ];

        let options = relation_options(&form, "banana", "table", &["banana", "table"]);
        let by_name: HashMap<_, _> = options.into_iter().collect();

        let Value::Map(unary) = &by_name["is_anchor"] else { panic!("not an entry") };
        assert!(!unary.contains_key("reference"), "a unary relation was given a reference");

        let Value::Map(binary) = &by_name["next_to"] else { panic!("not an entry") };
        assert_eq!(binary["reference"], Value::text("table"));
        assert_eq!(
            binary["params"],
            Value::map([("side", Value::text("positive_y"))]),
            "a required parameter was left out"
        );
    }

    /// A subtask from the menu names nodes that exist, so the spec it lands in
    /// still loads: Arena refuses a task parameter naming no node.
    #[test]
    fn a_subtask_menu_offers_entries_naming_real_nodes() {
        let mut form = Form::default();
        form.vocabulary.tasks = vec![Kind {
            name: "LiftObjectTask".into(),
            fields: vec![
                Field { name: "lift_object".into(), kind: "node_ref".into(), required: true, ..Default::default() },
                Field { name: "episode_length_s".into(), kind: "number".into(), ..Default::default() },
            ],
            ..Default::default()
        }];

        let options = subtask_options(&form, &["droid", "table"]);
        let Value::Map(entry) = &options[0].1 else { panic!("not an entry") };
        assert_eq!(entry["kind"], Value::text("LiftObjectTask"));
        assert_eq!(entry["params"], Value::map([("lift_object", Value::text("droid"))]), "{entry:?}");
    }

    /// The robot is picked from the embodiments alone, and the background from
    /// the backgrounds. Arena's own validator takes a banana or a table in the
    /// robot's slot, and the scene then fails to build.
    #[test]
    fn a_registry_menu_offers_only_its_role() {
        let asset = |name: &str, role: &str| crate::spec::CatalogAsset {
            registry_name: name.into(),
            role: role.into(),
            ..Default::default()
        };
        let mut form = Form::default();
        form.vocabulary.assets = vec![
            asset("franka_ik", "embodiment"),
            asset("maple_table_robolab", "background"),
            asset("banana_ycb_robolab", "object"),
            asset("droid_abs_joint_pos", "embodiment"),
        ];

        let options = registry_options(&form, "embodiment");
        let names: Vec<&str> = options.iter().map(|(label, _)| label.as_str()).collect();
        assert_eq!(names, ["franka_ik", "droid_abs_joint_pos"]);
        assert_eq!(options[1].1, Value::text("droid_abs_joint_pos"), "a pick writes the registry name");

        let backgrounds = registry_options(&form, "background");
        assert_eq!(backgrounds.len(), 1);
        assert_eq!(backgrounds[0].0, "maple_table_robolab");
    }

    /// A number under the pointer reaches the view where it has been dragged
    /// to, not where the spec still has it, and reaches nothing else.
    #[test]
    fn a_dragged_number_is_drawn_before_the_spec_carries_it() {
        let relation = Relation {
            kind: "on".into(),
            subject: "bowl".into(),
            params: Params::from([("clearance_m".to_owned(), Value::Number(0.0))]),
            ..Default::default()
        };
        let dragging = |path: Vec<Step>| PanelState {
            editing: Some(Editing { path, value: 0.08, sent: false }),
            ..PanelState::default()
        };
        let clearance_of = |index: usize| {
            vec![
                Step::from("relations"),
                Step::from(index),
                Step::from("params"),
                Step::from("clearance_m"),
            ]
        };

        let dragged = pending(&relation, 3, &dragging(clearance_of(3))).expect("the drag is this relation's");
        assert_eq!(dragged.params["clearance_m"], Value::Number(0.08));
        assert_eq!(relation.params["clearance_m"], Value::Number(0.0), "the spec was written to");

        assert!(pending(&relation, 2, &dragging(clearance_of(3))).is_none(), "another relation's drag");
        let subtask = vec![
            Step::from("task"),
            Step::from("subtasks"),
            Step::from(3usize),
            Step::from("params"),
            Step::from("clearance_m"),
        ];
        assert!(pending(&relation, 3, &dragging(subtask)).is_none(), "not a relation parameter");
    }

    /// The texts drawn where they can be seen: inside the clip of whatever
    /// they were drawn in, which for a card is the panel's scrolled view.
    fn visible(output: &egui::FullOutput) -> Vec<String> {
        let screen = egui::Rect::from_min_size(egui::Pos2::ZERO, egui::vec2(360.0, 400.0));
        output
            .shapes
            .iter()
            .filter_map(|clipped| match &clipped.shape {
                egui::Shape::Text(text) => {
                    let drawn = egui::Rect::from_min_size(text.pos, text.galley.size());
                    let seen = clipped.clip_rect.intersect(screen);
                    seen.intersects(drawn).then(|| text.galley.text().to_owned())
                }
                _ => None,
            })
            .collect()
    }

    /// A pick in the graph unfolds the entry's card and scrolls the panel to
    /// it, however far down its list it is: the card's id comes into view,
    /// and it is the one card open.
    #[test]
    fn a_pick_in_the_graph_opens_its_card_in_view() {
        let mut form = Form::default();
        form.spec.objects = (0..40)
            .map(|at| crate::spec::Node { id: format!("obj{at}"), registry_name: format!("thing{at}"), ..Default::default() })
            .collect();
        let ctx = egui::Context::default();
        let mut state = PanelState::default();
        let mut time = 0.0;
        let mut frame = |form: &Form, state: &mut PanelState| {
            // Sixty a second, as the panel unfolds and scrolls in real time.
            time += 1.0 / 60.0;
            let input = egui::RawInput {
                time: Some(time),
                screen_rect: Some(egui::Rect::from_min_size(egui::Pos2::ZERO, egui::vec2(360.0, 400.0))),
                ..Default::default()
            };
            let output = ctx.run_ui(input, |ui| {
                panel(ui, form, state, &mut |_| None);
            });
            let seen = visible(&output);
            output.drop_without_applying_deltas();
            (seen, time)
        };

        let (seen, now) = frame(&form, &mut state);
        assert!(!seen.iter().any(|text| text == "obj35"), "already in view: {seen:?}");
        state.dock.graph.picked = Some(Pick { item: Item::Node("obj35".into()), at: now });
        let mut seen = Vec::new();
        for _ in 0..40 {
            seen = frame(&form, &mut state).0;
        }
        assert!(seen.iter().any(|text| text == "obj35"), "not scrolled to: {seen:?}");
        let open = seen.iter().filter(|text| *text == "asset").count();
        assert_eq!(open, 1, "the picked card is not the one open: {seen:?}");

        // Only until the spec changes: the entry it named may be elsewhere.
        form.revision += 1;
        frame(&form, &mut state);
        assert!(state.dock.graph.picked.is_none(), "a pick outlived the spec it was made on");
    }

    /// The header alone, a frame at a time: the page it turned to, and where
    /// it drew each text.
    struct Header {
        ctx: egui::Context,
        time: f64,
    }

    impl Header {
        fn new() -> Self {
            crate::editor::tests::SENT.with_borrow_mut(Vec::clear);
            Self { ctx: egui::Context::default(), time: 0.0 }
        }

        fn frame(&mut self, form: &Form, events: Vec<egui::Event>) -> (Option<usize>, HashMap<String, egui::Pos2>) {
            // A second a frame, so no two clicks read as a double click.
            self.time += 1.0;
            let input = egui::RawInput {
                events,
                time: Some(self.time),
                screen_rect: Some(egui::Rect::from_min_size(egui::Pos2::ZERO, egui::vec2(360.0, 200.0))),
                ..Default::default()
            };
            let mut turned = None;
            let output = self.ctx.run_ui(input, |ui| turned = header(ui, form));
            let texts = texts_at(&output).into_iter().collect();
            output.drop_without_applying_deltas();
            (turned, texts)
        }
    }

    /// Every text drawn, and the centre of where it was drawn.
    fn texts_at(output: &egui::FullOutput) -> Vec<(String, egui::Pos2)> {
        output
            .shapes
            .iter()
            .filter_map(|clipped| match &clipped.shape {
                egui::Shape::Text(text) => {
                    let at = egui::Rect::from_min_size(text.pos, text.galley.size()).center();
                    Some((text.galley.text().to_owned(), at))
                }
                _ => None,
            })
            .collect()
    }

    /// A card whose entry is wrong carries a ⚠ in its own header, and says
    /// what on hover; a card whose entry is right carries none.
    #[test]
    fn a_card_marks_what_is_wrong_with_its_entry() {
        let relation = |kind: &str| Relation { kind: kind.into(), subject: "bowl".into(), ..Default::default() };
        let line = "relations.1.params.foo: next_to takes no 'foo'";
        let mut form = Form::default();
        form.spec.relations = vec![relation("on"), relation("next_to")];
        form.problems = vec![Problem { path: vec!["relations".into(), 1usize.into()], line: line.into(), warning: false }];
        let ctx = egui::Context::default();
        let mut state = PanelState::default();
        let mut frame = |time: f64, events: Vec<egui::Event>| {
            let input = egui::RawInput {
                events,
                time: Some(time),
                screen_rect: Some(egui::Rect::from_min_size(egui::Pos2::ZERO, egui::vec2(360.0, 800.0))),
                ..Default::default()
            };
            let output = ctx.run_ui(input, |ui| {
                panel(ui, &form, &mut state, &mut |_| None);
            });
            let texts = texts_at(&output);
            output.drop_without_applying_deltas();
            texts
        };

        let texts = frame(1.0, vec![]);
        let marks: Vec<egui::Pos2> = texts.iter().filter(|(text, _)| text == "⚠").map(|(_, at)| *at).collect();
        let row = |kind: &str| texts.iter().find(|(text, _)| text == kind).map(|(_, at)| at.y);
        assert_eq!(marks.len(), 1, "{texts:?}");
        assert!((marks[0].y - row("next_to").unwrap()).abs() < 4.0, "on another card: {texts:?}");

        // Hovered, then held still past egui's tooltip delay; a tooltip is
        // laid out unseen on its first frame, and drawn from the next.
        frame(2.0, vec![egui::Event::PointerMoved(marks[0])]);
        frame(3.0, vec![]);
        let texts = frame(4.0, vec![]);
        assert!(texts.iter().any(|(text, _)| text == line), "nothing said on hover: {texts:?}");
    }

    fn click(at: egui::Pos2) -> Vec<egui::Event> {
        let button = |pressed| egui::Event::PointerButton {
            pos: at,
            button: egui::PointerButton::Primary,
            pressed,
            modifiers: egui::Modifiers::NONE,
        };
        vec![egui::Event::PointerMoved(at), button(true), button(false)]
    }

    /// Two layouts, the second failing a check.
    fn two_layouts() -> Form {
        let failing = Layout { success: Some(false), failed_checks: vec!["no_overlap".into()], ..Default::default() };
        let layouts = vec![Layout { success: Some(true), ..Default::default() }, failing];
        Form { layouts, seed: Some(42), ..Default::default() }
    }

    /// The pager turns within the layouts the solve drew, and says what the
    /// checks made of the one on screen.
    #[test]
    fn the_pager_turns_within_the_layouts() {
        let mut header = Header::new();
        let mut form = two_layouts();
        let (_, texts) = header.frame(&form, vec![]);
        assert!(texts.contains_key("1 / 2") && texts.contains_key("✔ valid"), "{texts:?}");
        assert_eq!(header.frame(&form, click(texts["⏴"])).0, None, "turned back from the first");
        let (turned, _) = header.frame(&form, click(texts["⏵"]));
        assert_eq!(turned, Some(1));

        form.page = 1;
        let (_, texts) = header.frame(&form, vec![]);
        assert!(texts.contains_key("2 / 2") && texts.contains_key("⚠ fails no_overlap"), "{texts:?}");
        assert_eq!(header.frame(&form, click(texts["⏵"])).0, None, "turned on past the last");
        assert_eq!(header.frame(&form, click(texts["⏴"])).0, Some(0));
    }

    /// The button asks Python for new layouts, and gives way to a spinner
    /// while a solve is running, as the reindex button does.
    #[test]
    fn regenerate_asks_python_and_waits_out_the_solve() {
        let mut header = Header::new();
        let mut form = two_layouts();
        let (_, texts) = header.frame(&form, vec![]);
        assert!(texts.contains_key("seed 42"), "{texts:?}");
        header.frame(&form, click(texts["⟳"]));
        assert_eq!(crate::editor::tests::SENT.with_borrow(Clone::clone), [Request::Regenerate]);

        form.solving = true;
        let (_, texts) = header.frame(&form, vec![]);
        assert!(texts.contains_key("solving…") && !texts.contains_key("⟳"), "{texts:?}");
        assert!(texts.contains_key("1 / 2"), "the pager went with the button");
    }

    /// A folded subtask says what it is done to, in the order its kind lists
    /// the parameters, and leaves out what is not a node.
    #[test]
    fn a_folded_subtask_names_its_nodes() {
        let kind = Kind {
            name: "PickAndPlaceTask".into(),
            fields: vec![
                Field { name: "pick_up_object".into(), kind: "node_ref".into(), ..Default::default() },
                Field { name: "destination_location".into(), kind: "node_ref".into(), ..Default::default() },
                Field { name: "episode_length_s".into(), kind: "number".into(), ..Default::default() },
            ],
            ..Default::default()
        };
        let params = Params::from([
            ("episode_length_s".to_owned(), Value::Number(70.0)),
            ("destination_location".to_owned(), Value::text("bowl")),
            ("pick_up_object".to_owned(), Value::text("banana")),
        ]);
        assert_eq!(nodes_named(Some(&kind), &params), "banana, bowl");
        assert_eq!(nodes_named(None, &params), "", "an unknown kind says nothing");
    }
}

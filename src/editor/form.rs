//! What the panel shows: a section per part of the spec, each entry in it a
//! [`Card`]. The asset browser docks under the 3D view beside it; see
//! [`super::browser`].
//!
//! A section says what an entry's card holds — its icon, its title, its body —
//! and the card and the controls in it do the rest; see [`super::card`] and
//! [`super::controls`].

use std::collections::{BTreeSet, HashMap};

use bevy::prelude::*;
use bevy_egui::{EguiContexts, egui};
use bevy_panorbit_camera::PanOrbitCamera;

use super::browser::{self, Browser};
use super::card::{Card, Icon};
use super::controls::{id_options, insert, menu, params, row, set, text_field, toggle, with};
use super::relations::{self, RelationGizmos};
use super::thumbnails::{Files, Images, Thumbnails};
use super::{Form, PointerOverPanel, in_the_view, send};
use crate::layout::Placed;
use crate::spec::{Kind, Relation, Request, Step, Value};

/// How wide the panel opens. Draggable from there.
const WIDTH: f32 = 360.0;

/// Everything the panel remembers that is not in the spec.
pub struct PanelState {
    /// The asset browser's search, folder and pick.
    pub(super) browser: Browser,
    /// The pictures on hand, for the browser's tiles and the objects' cards.
    pub(super) thumbnails: Thumbnails,
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
/// screenshot, where there is nothing to hover.
impl Default for PanelState {
    fn default() -> Self {
        let shown = std::env::var("ARENA_VIEWER_SHOW_RELATIONS").unwrap_or_default();
        Self {
            browser: Browser::default(),
            thumbnails: Thumbnails::default(),
            editing: None,
            typing: HashMap::new(),
            removing: None,
            revision: 0,
            pinned: shown.split(',').filter_map(|index| index.trim().parse().ok()).collect(),
            show_all: matches!(shown.trim(), "1" | "all"),
        }
    }
}

#[allow(clippy::too_many_arguments)]
pub fn draw(
    mut contexts: EguiContexts,
    form: Res<Form>,
    placed: Res<Placed>,
    files: Files,
    mut gizmos: Gizmos<RelationGizmos>,
    mut state: Local<PanelState>,
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
    let shown = egui::Panel::right("spec")
        .default_size(WIDTH)
        .min_size(240.0)
        .show(&mut window, |ui| panel(ui, &form, &mut state, &mut images));
    // After the panel, so the dock spans the view alone and the panel keeps
    // the window's full height, as Isaac Sim lays out its stage and content
    // browser.
    let PanelState { browser, thumbnails, .. } = &mut *state;
    let dock = browser::dock(&mut window, &form, browser, &mut |path| thumbnails.get(path, &mut images));
    state.thumbnails.evict(&mut images);
    // Measured here because this is the only place the panels' rects exist;
    // see [`PointerOverPanel`] for why egui cannot be asked instead.
    over_panel.0 = ctx
        .pointer_interact_pos()
        .is_some_and(|pointer| shown.response.rect.contains(pointer) || dock.contains(pointer));
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

    // The relations the panel wants seen, drawn over the layout. From here
    // rather than a system of its own: the hover is this frame's, and a gizmo
    // issued in egui's pass is collected with every other at the frame's end.
    for &index in &shown.inner {
        if let Some(relation) = form.spec.relations.get(index) {
            // Whatever is being dragged right now stands in for what the spec
            // says, so the view follows the number rather than the answer to it.
            let dragged = pending(relation, index, &state);
            let relation = dragged.as_ref().unwrap_or(relation);
            relations::draw(relation, form.vocabulary.relation(&relation.kind), &placed, &mut gizmos);
        }
    }

    // Over the 3D view, in the corner of what the panels left of it. The view
    // is the half that is behind: an accepted edit reaches the form at once and
    // the layout only once the solver has caught up.
    if form.solving {
        let spot = egui::Rect::from_min_size(view.right_top() + egui::vec2(-232.0, 12.0), egui::vec2(220.0, 20.0));
        // Right to left, so the spinner keeps the corner and the words run
        // inward from it however long they are.
        let mut status = window.new_child(
            egui::UiBuilder::new()
                .max_rect(spot)
                .layout(egui::Layout::right_to_left(egui::Align::Center)),
        );
        status.add(spinner());
        status.weak("Resolving layout…");
    }
    Ok(())
}

/// What says a layout is still being solved. Sized down from the default, which
/// is a line high and reads as a widget rather than a status.
fn spinner() -> egui::Spinner {
    egui::Spinner::new().size(16.0)
}

/// Draw the panel, and say which relations it wants drawn in the view.
fn panel(ui: &mut egui::Ui, form: &Form, state: &mut PanelState, images: &mut Images) -> BTreeSet<usize> {
    // A half-made edit belongs to the spec it was made against.
    if state.revision != form.revision {
        state.revision = form.revision;
        state.editing = None;
        state.typing.clear();
    }

    ui.heading(&form.spec.env_name);
    for error in &form.errors {
        ui.colored_label(egui::Color32::from_rgb(240, 130, 115), error);
    }

    let shown = egui::ScrollArea::vertical()
        .auto_shrink(false)
        .show(ui, |ui| {
            embodiment(ui, form, state);
            background(ui, form, state);
            objects(ui, form, state, images);
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
    section(ui, "embodiment", None, form.owner("embodiment"), |ui| {
        Card::new(&path, Icon::Glyph("🕹")).summary(&node.registry_name).folded().show(
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
    section(ui, "background", None, form.owner("background"), |ui| {
        Card::new(&path, Icon::Glyph("🏠")).summary(&node.registry_name).folded().show(
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
fn objects(ui: &mut egui::Ui, form: &Form, state: &mut PanelState, images: &mut Images) {
    section(ui, "objects", Some(form.spec.objects.len()), form.owner("objects"), |ui| {
        // No add button: an object is an asset somebody offers, and the
        // browser under the view is where those are. Picking there is what
        // makes "add" a decision rather than a surprise.
        ui.weak("add from the asset browser under the view");
        for (index, node) in form.spec.objects.iter().enumerate() {
            let path = vec![Step::from("objects"), Step::from(index)];
            // The most particular asset the entry names: a prop's own, over
            // any registered asset that shares its registry name.
            let asset = form
                .assets()
                .filter(|asset| asset.is_named_by(node))
                .max_by_key(|asset| asset.params.len());
            let name = asset.map_or(node.registry_name.as_str(), |asset| asset.name.as_str());
            let icon = asset
                .and_then(|asset| state.thumbnails.get(&asset.thumbnail, images))
                .map_or(Icon::Glyph("📦"), Icon::Picture);
            let card = Card::new(&path, icon).summary(name).folded().removable().show(
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
    section(ui, "object sets", Some(sets.len()), form.owner("object_sets"), |ui| {
        for (index, entry) in sets.iter().enumerate() {
            let path = vec![Step::from("object_sets"), Step::from(index)];
            let members = entry.members.join(", ");
            let card = Card::new(&path, Icon::Glyph("🎲")).summary(&members).removable().show(
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
    section(ui, "object references", Some(references.len()), form.owner("object_references"), |ui| {
        for (index, entry) in references.iter().enumerate() {
            let path = vec![Step::from("object_references"), Step::from(index)];
            let card = Card::new(&path, Icon::Glyph("📍")).summary(&entry.parent_id).removable().show(
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
    section(ui, "relations", Some(form.spec.relations.len()), form.owner("relations"), |ui| {
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
            let card = Card::new(&path, Icon::Glyph("🔗")).summary(summary).pin(&mut pinned).removable().show(
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
    section(ui, "task", None, form.owner("task"), |ui| {
        Card::new(&path, Icon::Glyph("🎯")).summary(&task.composition).show(
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
                    let card = Card::new(&path, Icon::Glyph("✔")).summary(summary).removable().show(
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
/// take is what their controls need.
fn section(ui: &mut egui::Ui, name: &str, count: Option<usize>, owner: &str, body: impl FnOnce(&mut egui::Ui)) {
    let count = count.map_or(String::new(), |count| format!("  ({count})"));
    let owner = match owner {
        "" => String::new(),
        owner => format!("  ·  {owner}"),
    };
    egui::CollapsingHeader::new(format!("{name}{count}{owner}"))
        .id_salt(name)
        .default_open(true)
        .show_unindented(ui, body);
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::spec::{Field, Params};
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

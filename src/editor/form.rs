//! Drawing the spec, and turning a control's click into an [`Edit`].

use std::collections::BTreeSet;

use bevy::prelude::*;
use bevy_egui::{EguiContexts, egui};

use super::relations::{self, RelationGizmos};
use super::{Form, PointerOverPanel, send};
use crate::layout::Placed;
use crate::spec::{Edit, Field, Kind, Params, Relation, Step, Value};

/// How wide the panel opens. Draggable from there.
const WIDTH: f32 = 360.0;

/// The share of a row a name takes, leaving the rest to the control. Every row
/// splits at the same place, so the controls line up down the panel however
/// long the names beside them are.
const NAMES: f32 = 0.4;

/// Everything the panel remembers that is not in the spec.
pub struct PanelState {
    /// What the asset browser is filtered to.
    pub search: String,
    /// The number being dragged or typed into: the control, the value it
    /// shows, and where in the spec that value belongs. One, because only one
    /// control can be under the pointer at a time. The path is carried so the
    /// view can draw the value before the spec comes back carrying it.
    editing: Option<(egui::Id, f64, Vec<Step>)>,
    /// The id being renamed, and the text typed so far. One, for the same
    /// reason: only one box can hold the caret.
    renaming: Option<(egui::Id, String)>,
    /// The spec `editing` was made against.
    revision: u64,
    /// Relations kept drawn in the view after the pointer has left them, by
    /// position in the list. Removing one above a pin shifts the pin.
    pinned: BTreeSet<usize>,
    /// Draw every relation, pointer or not.
    show_all: bool,
}

/// `ARENA_VIEWER_SHOW_RELATIONS=all`, or `=2,5` for those positions in the
/// list, starts with those relations drawn: the way to see them in a
/// screenshot, where there is nothing to hover.
impl Default for PanelState {
    fn default() -> Self {
        let shown = std::env::var("ARENA_VIEWER_SHOW_RELATIONS").unwrap_or_default();
        Self {
            search: String::new(),
            editing: None,
            renaming: None,
            revision: 0,
            pinned: shown.split(',').filter_map(|index| index.trim().parse().ok()).collect(),
            show_all: matches!(shown.trim(), "1" | "all"),
        }
    }
}

pub fn draw(
    mut contexts: EguiContexts,
    form: Res<Form>,
    placed: Res<Placed>,
    mut gizmos: Gizmos<RelationGizmos>,
    mut state: Local<PanelState>,
    mut over_panel: ResMut<PointerOverPanel>,
) -> Result {
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
    let shown = egui::Panel::right("spec")
        .default_size(WIDTH)
        .min_size(240.0)
        .show(&mut window, |ui| panel(ui, &form, &mut state));
    // Measured here because this is the only place the panel's rect exists;
    // see [`PointerOverPanel`] for why egui cannot be asked instead.
    over_panel.0 = ctx
        .pointer_interact_pos()
        .is_some_and(|pointer| shown.response.rect.contains(pointer));

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

    // Over the 3D view, in the corner of what the panel left of it. The view is
    // the half that is behind: an accepted edit reaches the form at once and
    // the layout only once the solver has caught up.
    if form.solving {
        let view = window.available_rect_before_wrap();
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
fn panel(ui: &mut egui::Ui, form: &Form, state: &mut PanelState) -> BTreeSet<usize> {
    // A half-made edit belongs to the spec it was made against.
    if state.revision != form.revision {
        state.revision = form.revision;
        state.editing = None;
        state.renaming = None;
    }

    ui.heading(&form.spec.env_name);
    for error in &form.errors {
        ui.colored_label(egui::Color32::from_rgb(240, 130, 115), error);
    }

    // The browser keeps the foot of the panel rather than the end of the form:
    // adding an object *is* picking one, so it is not somewhere to scroll to.
    egui::Panel::bottom("assets")
        .resizable(true)
        .default_size(200.0)
        .show(ui, |ui| super::browser::show(ui, form, &mut state.search));

    egui::ScrollArea::vertical()
        .auto_shrink(false)
        .show(ui, |ui| {
            objects(ui, form, state);
            relations(ui, form, state)
        })
        .inner
}

fn objects(ui: &mut egui::Ui, form: &Form, state: &mut PanelState) {
    group(ui, "objects", form.spec.objects.len(), form.owner("objects"), |ui| {
        for (index, node) in form.spec.objects.iter().enumerate() {
            let path = vec![Step::from("objects"), Step::from(index)];
            ui.horizontal(|ui| {
                if remove(ui) {
                    send(Edit::Remove { path: path.clone() });
                }
                name(ui, ui.make_persistent_id(index), &node.id, with(&path, "id"), state);
                ui.weak(&node.registry_name);
            });
        }
        // No add button: an object is a registered asset, and the browser below
        // is where those are. Picking there is what makes "add" a decision
        // rather than a surprise.
        ui.weak("add one from the asset list below");
    });
}

/// The relations, each with a toggle to keep it drawn. Returns the ones to
/// draw this frame: hovered, pinned, or every one when asked for them all.
fn relations(ui: &mut egui::Ui, form: &Form, state: &mut PanelState) -> BTreeSet<usize> {
    let ids = form.spec.node_ids();
    let mut shown = BTreeSet::new();
    group(ui, "relations", form.spec.relations.len(), form.owner("relations"), |ui| {
        for (index, relation) in form.spec.relations.iter().enumerate() {
            let path = vec![Step::from("relations"), Step::from(index)];
            let kind = form.vocabulary.relation(&relation.kind);
            let reference = relation.reference.clone().unwrap_or_default();

            let entry = ui.push_id(index, |ui| {
                ui.group(|ui| {
                    // The box takes the panel's width rather than its contents',
                    // so one entry's rows line up with the next one's.
                    ui.set_min_width(ui.available_width());
                    ui.horizontal(|ui| {
                        if remove(ui) {
                            send(Edit::Remove { path: path.clone() });
                        }
                        // Right to left: the toggle takes the far edge and the
                        // kind spreads over whatever is left between them.
                        ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                            let mut pinned = state.pinned.contains(&index);
                            if ui.toggle_value(&mut pinned, "👁").on_hover_text("keep drawn").changed() {
                                if pinned {
                                    state.pinned.insert(index);
                                } else {
                                    state.pinned.remove(&index);
                                }
                            }
                            // Changing the kind rewrites the whole entry: the
                            // old kind's parameters mean nothing to the new
                            // one, and whether there is a `reference` at all is
                            // the kind's to say.
                            let options = kind_options(form, &relation.subject, &reference, &ids);
                            menu(ui, "kind", &relation.kind, options, set(&path));
                        });
                    });
                    // The kind names the whole entry, so it is a heading and
                    // what it governs is ruled off from it.
                    ui.separator();
                    row(ui, "subject", |ui| {
                        menu(ui, "subject", &relation.subject, id_options(&ids), set(&with(&path, "subject")));
                    });
                    // A unary relation takes no reference, and Arena refuses one
                    // that has it — so the slot is not drawn rather than
                    // validated.
                    if kind.is_some_and(|kind| kind.unary == Some(false)) {
                        row(ui, "reference", |ui| {
                            menu(ui, "reference", &reference, id_options(&ids), set(&with(&path, "reference")));
                        });
                    }
                    params(ui, kind, &relation.params, &ids, &with(&path, "params"), state);
                })
                .response
            });
            // The whole entry, controls included: a number being dragged is
            // still the relation being looked at.
            if entry.inner.contains_pointer() {
                shown.insert(index);
            }
        }

        let options = kind_options(form, subject_default(form), &form.spec.background.id, &ids);
        menu(ui, "add", "+ relation", options, insert(&[Step::from("relations")]));
    });
    // Outside the group, so a collapsed section keeps its pins drawn.
    let count = form.spec.relations.len();
    if state.show_all {
        shown.extend(0..count);
    } else {
        shown.extend(state.pinned.iter().copied().filter(|&index| index < count));
    }
    // A number being dragged keeps its relation drawn however far the pointer
    // has wandered: egui holds a drag after the pointer leaves the control, and
    // the entry's hover does not.
    shown.extend(
        state
            .editing
            .as_ref()
            .and_then(|(_, _, path)| relation_param(path))
            .map(|(index, _)| index)
            .filter(|&index| index < count),
    );
    shown
}

/// A control per parameter the kind declares, in the order it declares them.
fn params(
    ui: &mut egui::Ui,
    kind: Option<&Kind>,
    values: &Params,
    ids: &[&str],
    base: &[Step],
    state: &mut PanelState,
) {
    let Some(kind) = kind else { return };
    for field in &kind.fields {
        let value = values.get(&field.name).unwrap_or(&field.default);
        let path = with(base, &field.name);
        row(ui, &field.name, |ui| control(ui, field, value, ids, &path, state));
    }
}

/// One named row: the name ends where the control begins, and the control takes
/// the rest of the width. A name longer than its share is cut rather than
/// allowed to push the control out of line.
///
/// The space is taken first and painted into after, because a label fills only
/// the axis its layout justifies. `add_sized` justifies across the row it is
/// in, which for a horizontal row is the vertical one, and leaves the name
/// centred in its space with `halign` doing nothing.
fn row(ui: &mut egui::Ui, name: &str, control: impl FnOnce(&mut egui::Ui)) {
    ui.horizontal(|ui| {
        let size = egui::vec2(ui.available_width() * NAMES, ui.spacing().interact_size.y);
        let (rect, _) = ui.allocate_exact_size(size, egui::Sense::hover());
        ui.place(
            rect,
            egui::Label::new(egui::RichText::new(name).weak())
                .halign(egui::Align::RIGHT)
                .truncate(),
        );
        control(ui);
    });
}

/// The control that edits one parameter.
///
/// `field.kind` is a string rather than an enum so a control added on the
/// Python side draws as text instead of failing the payload to decode.
fn control(
    ui: &mut egui::Ui,
    field: &Field,
    value: &Value,
    ids: &[&str],
    path: &[Step],
    state: &mut PanelState,
) {
    let current = value.as_text().unwrap_or_default();
    match field.kind.as_str() {
        "enum" => {
            let options = field.options.iter().map(|o| (o.clone(), Value::text(o))).collect();
            menu(ui, &field.name, current, options, set(path));
        }
        "node_ref" => menu(ui, &field.name, current, id_options(ids), set(path)),
        "number" | "int" => number(ui, ui.make_persistent_id(&field.name), value.as_number(), path, state),
        _ => {
            ui.weak(format!("{value:?}"));
        }
    }
}

/// An id: a label until it is double-clicked, a box to type in after that.
///
/// Committed when the box is left — by Enter, or by clicking away — and thrown
/// away on Escape, which is the same bargain every other control here makes.
/// Renaming is a `Set` like any other: Python carries the references to the old
/// id across, the way removing one drops them.
fn name(ui: &mut egui::Ui, id: egui::Id, current: &str, path: Vec<Step>, state: &mut PanelState) {
    if !matches!(&state.renaming, Some((renaming, _)) if *renaming == id) {
        // A plain label senses nothing, and so never reports the double click.
        let label = egui::Label::new(current).sense(egui::Sense::click());
        if ui.add(label).on_hover_text("double-click to rename").double_clicked() {
            state.renaming = Some((id, current.to_owned()));
            // The box it belongs to is drawn next frame; the focus waits there
            // for it.
            ui.memory_mut(|memory| memory.request_focus(id));
        }
        return;
    }

    let Some((_, text)) = &mut state.renaming else { return };
    let response = ui.add(egui::TextEdit::singleline(text).id(id).desired_width(140.0));
    if !response.lost_focus() {
        return;
    }
    let typed = std::mem::take(text);
    state.renaming = None;
    if !ui.input(|input| input.key_pressed(egui::Key::Escape)) && !typed.is_empty() && typed != current {
        send(Edit::Set { path, value: Value::text(typed) });
    }
}

/// A number, sent once it is let go of rather than as it moves — a solve per
/// dragged pixel is not something Python can keep up with. The view does follow
/// it as it moves, without waiting for that; see [`pending`].
fn number(ui: &mut egui::Ui, id: egui::Id, value: Option<f64>, path: &[Step], state: &mut PanelState) {
    let in_spec = value.unwrap_or_default();
    let mut shown = match &state.editing {
        Some((editing, value, _)) if *editing == id => *value,
        _ => in_spec,
    };
    let size = egui::vec2(ui.available_width(), ui.spacing().interact_size.y);
    let response = ui.add_sized(size, egui::DragValue::new(&mut shown).speed(0.005).max_decimals(4));
    if response.dragged() || response.has_focus() {
        state.editing = Some((id, shown, path.to_vec()));
    }
    if (response.drag_stopped() || response.lost_focus()) && shown != in_spec {
        send(Edit::Set { path: path.to_vec(), value: Value::Number(shown) });
    }
}

/// `relation` as the number being dragged would leave it, when that number is
/// one of its parameters.
///
/// What draws a drag before the spec carries it. Nothing more is sent for it:
/// this is the value the control is already painting, and the spec that answers
/// the end of the drag holds the same one, so the handover draws no step. A
/// refused edit brings the old value back, as it does to the control.
///
/// A path naming anything else is left alone, so a number control added
/// somewhere other than a relation previews nothing rather than the wrong
/// relation.
fn pending(relation: &Relation, index: usize, state: &PanelState) -> Option<Relation> {
    let (_, value, path) = state.editing.as_ref()?;
    let (at, name) = relation_param(path)?;
    if at != index {
        return None;
    }
    let mut dragged = relation.clone();
    dragged.params.insert(name.to_owned(), Value::Number(*value));
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

/// A dropdown showing `current`, sending `edit(value)` for whichever option is
/// picked.
///
/// Nothing is selected locally: the spec is the selection, and the next payload
/// is what shows a new one.
fn menu(
    ui: &mut egui::Ui,
    salt: &str,
    current: &str,
    options: Vec<(String, Value)>,
    edit: impl Fn(Value) -> Edit,
) {
    egui::ComboBox::from_id_salt(salt)
        .width(ui.available_width())
        .selected_text(if current.is_empty() { "—" } else { current })
        .show_ui(ui, |ui| {
            for (label, value) in options {
                if ui.selectable_label(label == current, label.as_str()).clicked() {
                    send(edit(value));
                }
            }
        });
}

fn set(path: &[Step]) -> impl Fn(Value) -> Edit {
    let path = path.to_vec();
    move |value| Edit::Set { path: path.clone(), value }
}

fn insert(path: &[Step]) -> impl Fn(Value) -> Edit {
    let path = path.to_vec();
    move |value| Edit::Insert { path: path.clone(), value }
}

fn id_options(ids: &[&str]) -> Vec<(String, Value)> {
    ids.iter().map(|id| ((*id).to_owned(), Value::text(*id))).collect()
}

/// One whole relation entry per registered kind, each already valid: the kind's
/// required parameters filled in, and a `reference` only where the kind takes
/// one. Used both to change a relation's kind and to add one.
fn kind_options(form: &Form, subject: &str, reference: &str, ids: &[&str]) -> Vec<(String, Value)> {
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

/// The parameters the kind has no default for. Arena refuses an entry without
/// them, so an added relation carries a placeholder the form then shows.
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

/// One top-level section, with the file that owns it.
fn group(ui: &mut egui::Ui, name: &str, count: usize, owner: &str, body: impl FnOnce(&mut egui::Ui)) {
    egui::CollapsingHeader::new(format!("{name}  ({count})  ·  {owner}"))
        .id_salt(name)
        .default_open(true)
        .show(ui, body);
}

fn remove(ui: &mut egui::Ui) -> bool {
    ui.small_button("×").on_hover_text("remove").clicked()
}

/// `base` with one more step on the end.
fn with(base: &[Step], step: &str) -> Vec<Step> {
    let mut path = base.to_vec();
    path.push(Step::from(step));
    path
}

#[cfg(test)]
mod tests {
    use super::*;
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

        let options = kind_options(&form, "banana", "table", &["banana", "table"]);
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
            editing: Some((egui::Id::new("clearance_m"), 0.08, path)),
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
        let elsewhere = vec![Step::from("objects"), Step::from(3usize), Step::from("id")];
        assert!(pending(&relation, 3, &dragging(elsewhere)).is_none(), "not a relation parameter");
    }
}

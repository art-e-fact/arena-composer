//! Drawing the spec, and turning a control's click into an [`Edit`].

use bevy::prelude::*;
use bevy_egui::{EguiContexts, egui};

use super::{Form, send};
use crate::spec::{Edit, Field, Kind, Params, Step, Value};

/// How wide the panel opens. Draggable from there.
const WIDTH: f32 = 360.0;

/// Everything the panel remembers that is not in the spec.
#[derive(Default)]
pub struct PanelState {
    /// What the asset browser is filtered to.
    pub search: String,
    /// The number being dragged or typed into, and the value it shows. One,
    /// because only one control can be under the pointer at a time.
    editing: Option<(egui::Id, f64)>,
    /// The id being renamed, and the text typed so far. One, for the same
    /// reason: only one box can hold the caret.
    renaming: Option<(egui::Id, String)>,
    /// The spec `editing` was made against.
    revision: u64,
}

pub fn draw(mut contexts: EguiContexts, form: Res<Form>, mut state: Local<PanelState>) -> Result {
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
    egui::Panel::right("spec")
        .default_size(WIDTH)
        .min_size(240.0)
        .show(&mut window, |ui| panel(ui, &form, &mut state));
    Ok(())
}

fn panel(ui: &mut egui::Ui, form: &Form, state: &mut PanelState) {
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

    egui::ScrollArea::vertical().auto_shrink(false).show(ui, |ui| {
        objects(ui, form, state);
        relations(ui, form, state);
    });
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

fn relations(ui: &mut egui::Ui, form: &Form, state: &mut PanelState) {
    let ids = form.spec.node_ids();
    group(ui, "relations", form.spec.relations.len(), form.owner("relations"), |ui| {
        for (index, relation) in form.spec.relations.iter().enumerate() {
            let path = vec![Step::from("relations"), Step::from(index)];
            let kind = form.vocabulary.relation(&relation.kind);
            let reference = relation.reference.clone().unwrap_or_default();

            ui.push_id(index, |ui| {
                ui.group(|ui| {
                    ui.horizontal(|ui| {
                        if remove(ui) {
                            send(Edit::Remove { path: path.clone() });
                        }
                        // Changing the kind rewrites the whole entry: the old
                        // kind's parameters mean nothing to the new one, and
                        // whether there is a `reference` at all is the kind's
                        // to say.
                        let options = kind_options(form, &relation.subject, &reference, &ids);
                        menu(ui, "kind", &relation.kind, options, set(&path));
                    });
                    ui.horizontal(|ui| {
                        menu(ui, "subject", &relation.subject, id_options(&ids), set(&with(&path, "subject")));
                        // A unary relation takes no reference, and Arena
                        // refuses one that has it — so the slot is not drawn
                        // rather than validated.
                        if kind.is_some_and(|kind| kind.unary == Some(false)) {
                            // `»` and not `→`: egui ships a font without an
                            // arrow in it, and a missing glyph draws as a box.
                            ui.weak("»");
                            menu(ui, "reference", &reference, id_options(&ids), set(&with(&path, "reference")));
                        }
                    });
                    params(ui, kind, &relation.params, &ids, &with(&path, "params"), state);
                });
            });
        }

        let options = kind_options(form, subject_default(form), &form.spec.background.id, &ids);
        menu(ui, "add", "+ relation", options, insert(&[Step::from("relations")]));
    });
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
    if kind.fields.is_empty() {
        return;
    }
    egui::Grid::new("params").num_columns(2).show(ui, |ui| {
        for field in &kind.fields {
            let value = values.get(&field.name).unwrap_or(&field.default);
            ui.weak(&field.name);
            control(ui, field, value, ids, &with(base, &field.name), state);
            ui.end_row();
        }
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
/// dragged pixel is not something Python can keep up with.
fn number(ui: &mut egui::Ui, id: egui::Id, value: Option<f64>, path: &[Step], state: &mut PanelState) {
    let in_spec = value.unwrap_or_default();
    let mut shown = match state.editing {
        Some((editing, value)) if editing == id => value,
        _ => in_spec,
    };
    let response = ui.add(egui::DragValue::new(&mut shown).speed(0.005).max_decimals(4));
    if response.dragged() || response.has_focus() {
        state.editing = Some((id, shown));
    }
    if (response.drag_stopped() || response.lost_focus()) && shown != in_spec {
        send(Edit::Set { path: path.to_vec(), value: Value::Number(shown) });
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
}

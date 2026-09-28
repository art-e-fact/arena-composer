//! The controls inside a card, one per value the spec holds, each turning an
//! edit into a [`Request`].
//!
//! None of them keeps the value it shows: the spec is the value. What a
//! control does keep is a half-made edit — a number mid-drag, a name mid-type —
//! in [`PanelState`], against the spec path it belongs to, until it is let go.
//! A path is unique in the panel, so it is also the id of the widget editing
//! it.

use bevy_egui::egui;

use super::card::cross;
use super::form::{Editing, PanelState};
use super::send;
use crate::spec::{Field, Kind, Params, Request, Step, Value};

/// The share of a row a name takes, leaving the rest to the control. Every row
/// splits at the same place, so the controls line up down the panel however
/// long the names beside them are.
const NAMES: f32 = 0.4;

/// A control per parameter the kind declares, in the order it declares them.
pub fn params(
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
        let path = with(base, field.name.as_str());
        // Unsetting drops the key, handing the parameter back to its default.
        // Offered where the parameter takes None, the one state no control
        // reaches, and only while the spec says something to drop.
        let clearable = field.optional && !field.required && values.contains_key(&field.name);
        row(ui, &field.name, clearable.then_some(path.as_slice()), |ui| {
            control(ui, field, value, ids, &path, state)
        });
    }
}

/// One named row: the name ends where the control begins, and the control takes
/// the rest of the width. A name longer than its share is cut rather than
/// allowed to push the control out of line.
///
/// The space is taken first and filled by a child laid out right to left. That
/// is what puts the name against its control: a label fills only the axis its
/// layout justifies, so neither `halign` nor `place` moves it off the centre of
/// the space it was given.
///
/// With `clear`, the row ends in a button that removes that path from the spec.
pub fn row(ui: &mut egui::Ui, name: &str, clear: Option<&[Step]>, control: impl FnOnce(&mut egui::Ui)) {
    ui.horizontal(|ui| {
        let size = egui::vec2(ui.available_width() * NAMES, ui.spacing().interact_size.y);
        let (rect, _) = ui.allocate_exact_size(size, egui::Sense::hover());
        ui.new_child(
            egui::UiBuilder::new()
                .max_rect(rect)
                .layout(egui::Layout::right_to_left(egui::Align::Center)),
        )
        .add(egui::Label::new(egui::RichText::new(name).weak()).truncate());
        let Some(path) = clear else { return control(ui) };
        // Right to left, so the button keeps the far edge and the control
        // takes what is left of the row.
        ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
            let unset = cross(ui, "unset").clicked();
            control(ui);
            // Sent after the control, which may send an edit of its own as this
            // click takes its focus: the removal has to land second.
            if unset {
                send(Request::Remove { path: path.to_vec() });
            }
        });
    });
}

/// The control that edits one parameter.
///
/// `field.kind` is a string rather than an enum so a control added on the
/// Python side draws as text instead of failing the payload to decode.
fn control(ui: &mut egui::Ui, field: &Field, value: &Value, ids: &[&str], path: &[Step], state: &mut PanelState) {
    let current = value.as_text().unwrap_or_default();
    let width = ui.available_width();
    match field.kind.as_str() {
        "enum" => {
            let options = field.options.iter().map(|o| (o.clone(), Value::text(o))).collect();
            menu(ui, &field.name, current, options, set(path), width);
        }
        "node_ref" => {
            menu(ui, &field.name, current, id_options(ids), set(path), width);
        }
        "number" | "int" => {
            number(ui, value.as_number(), path, state);
        }
        "bool" => toggle(ui, matches!(value, Value::Bool(true)), path),
        "text" => {
            text_field(ui, path, current, state);
        }
        _ => {
            ui.weak(format!("{value:?}"));
        }
    }
}

/// A line of text: an id, a description, a prim path.
///
/// Always a box to type in, as a Blender modifier's name is, and committed when
/// it is left — by Enter, or by clicking away — and thrown away on Escape,
/// which is the same bargain every other control here makes. An empty box is
/// never sent: nothing here is valid empty. Renaming an id is a `Set` like any
/// other: Python carries the references to the old id across, the way removing
/// one drops them.
///
/// Sent only by the box that holds typed text, which is what keeps it to once:
/// egui reports a lost focus on two frames running, since a box drawn before
/// the one taking the focus only hears of it a frame late.
pub fn text_field(ui: &mut egui::Ui, path: &[Step], current: &str, state: &mut PanelState) -> egui::Response {
    let mut text = state.typing.get(path).cloned().unwrap_or_else(|| current.to_owned());
    let edit = egui::TextEdit::singleline(&mut text)
        .id(egui::Id::new(path))
        .desired_width(ui.available_width());
    let response = ui.add(edit);
    if response.has_focus() {
        state.typing.insert(path.to_vec(), text);
    } else if response.lost_focus() && state.typing.remove(path).is_some() {
        let escaped = ui.input(|input| input.key_pressed(egui::Key::Escape));
        if !escaped && !text.is_empty() && text != current {
            send(Request::Set { path: path.to_vec(), value: Value::text(text) });
        }
    }
    response
}

/// A number, sent once it is let go of rather than as it moves — a solve per
/// dragged pixel is not something Python can keep up with. The view does follow
/// it as it moves, without waiting for that; see `form::pending`.
///
/// Sent only from an edit still in hand, and kept in hand once sent, so the
/// number shows what was sent until the spec comes back carrying it. Escape
/// drops the edit, and with it the text `DragValue` keeps for the value typed.
fn number(ui: &mut egui::Ui, value: Option<f64>, path: &[Step], state: &mut PanelState) -> egui::Response {
    let editing = state.editing.as_ref().filter(|editing| editing.path == path);
    let held = editing.map(|editing| editing.value);
    let live = editing.is_some_and(|editing| !editing.sent);
    let mut shown = held.or(value).unwrap_or_default();
    let mut drag = egui::DragValue::new(&mut shown).speed(0.005).max_decimals(4);
    // No value is not zero: an unset `at_position.x` leaves the axis free,
    // where 0.0 pins it to the origin. It drags from zero all the same.
    if held.or(value).is_none() {
        drag = drag.custom_formatter(|_, _| "—".into());
    }
    let size = egui::vec2(ui.available_width(), ui.spacing().interact_size.y);
    let response = ui.add_sized(size, drag);
    if response.dragged() || response.has_focus() {
        state.editing = Some(Editing { path: path.to_vec(), value: shown, sent: false });
        return response;
    }
    if !live || !(response.drag_stopped() || response.lost_focus()) {
        return response;
    }
    // Against the spec's value rather than the number drawn, so an unset
    // parameter taken to exactly zero is still set.
    // ponytail: clicking into an unset number and away again sets it to zero;
    // latch whether anything was typed in `Editing` if that bites.
    let escaped = ui.input(|input| input.key_pressed(egui::Key::Escape));
    if escaped || Some(shown) == value {
        state.editing = None;
    } else {
        send(Request::Set { path: path.to_vec(), value: Value::Number(shown) });
        state.editing = Some(Editing { path: path.to_vec(), value: shown, sent: true });
    }
    response
}

/// A checkbox, sent on the click. There is no half-made state to hold on to the
/// way a dragged number has one.
pub fn toggle(ui: &mut egui::Ui, on: bool, path: &[Step]) {
    let mut on = on;
    if ui.checkbox(&mut on, "").changed() {
        send(Request::Set { path: path.to_vec(), value: Value::Bool(on) });
    }
}

/// A dropdown showing `current`, sending `edit(value)` for whichever option is
/// picked.
///
/// Nothing is selected locally: the spec is the selection, and the next payload
/// is what shows a new one.
pub fn menu(
    ui: &mut egui::Ui,
    salt: &str,
    current: &str,
    options: Vec<(String, Value)>,
    edit: impl Fn(Value) -> Request,
    width: f32,
) -> egui::Response {
    egui::ComboBox::from_id_salt(salt)
        .width(width)
        .selected_text(if current.is_empty() { "—" } else { current })
        .show_ui(ui, |ui| {
            for (label, value) in options {
                if ui.selectable_label(label == current, label.as_str()).clicked() {
                    send(edit(value));
                }
            }
        })
        .response
}

pub fn set(path: &[Step]) -> impl Fn(Value) -> Request {
    let path = path.to_vec();
    move |value| Request::Set { path: path.clone(), value }
}

pub fn insert(path: &[Step]) -> impl Fn(Value) -> Request {
    let path = path.to_vec();
    move |value| Request::Insert { path: path.clone(), value }
}

/// `base` with one more step on the end.
pub fn with(base: &[Step], step: impl Into<Step>) -> Vec<Step> {
    let mut path = base.to_vec();
    path.push(step.into());
    path
}

pub fn id_options(ids: &[&str]) -> Vec<(String, Value)> {
    ids.iter().map(|id| ((*id).to_owned(), Value::text(*id))).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::editor::tests::SENT;

    /// A headless egui, driven a frame at a time with the input a person
    /// would give it. What matters here happens between frames — egui reports
    /// a lost focus on two of them — so nothing short of real frames shows it.
    struct Panel {
        ctx: egui::Context,
        state: PanelState,
        /// Seconds, a whole one a frame: two clicks on one spot are then never
        /// read as a double click, which would select the word under them.
        time: f64,
    }

    impl Panel {
        fn new() -> Self {
            SENT.with_borrow_mut(Vec::clear);
            Self { ctx: egui::Context::default(), state: PanelState::default(), time: 0.0 }
        }

        /// One frame with `events`, drawing `draw`. Returns what it drew.
        fn frame<R>(
            &mut self,
            events: Vec<egui::Event>,
            mut draw: impl FnMut(&mut egui::Ui, &mut PanelState) -> R,
        ) -> R {
            self.time += 1.0;
            let input = egui::RawInput {
                events,
                time: Some(self.time),
                screen_rect: Some(egui::Rect::from_min_size(egui::Pos2::ZERO, egui::vec2(400.0, 300.0))),
                ..Default::default()
            };
            let mut drawn = None;
            self.ctx
                .run_ui(input, |ui| drawn = Some(draw(ui, &mut self.state)))
                .drop_without_applying_deltas();
            drawn.expect("the frame drew nothing")
        }
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

    fn key(key: egui::Key) -> Vec<egui::Event> {
        let event = |pressed| egui::Event::Key {
            key,
            physical_key: None,
            pressed,
            repeat: false,
            modifiers: egui::Modifiers::NONE,
        };
        vec![event(true), event(false)]
    }

    fn sent() -> Vec<Request> {
        SENT.with_borrow(Clone::clone)
    }

    fn set(path: &[Step], value: Value) -> Request {
        Request::Set { path: path.to_vec(), value }
    }

    /// Clicking from one box straight into another sends what was typed in
    /// the first, once, whichever way the click goes. Upward is the hard way:
    /// the box above takes the caret before the one below is drawn, which only
    /// hears it has lost it a frame later.
    #[test]
    fn a_name_typed_then_clicked_away_from_is_sent_once() {
        let upper = [Step::from("objects"), Step::from(0usize), Step::from("id")];
        let lower = [Step::from("objects"), Step::from(1usize), Step::from("id")];
        let boxes = |ui: &mut egui::Ui, state: &mut PanelState| {
            let a = text_field(ui, &upper, "bowl", state).rect.center();
            let b = text_field(ui, &lower, "apple", state).rect.center();
            (a, b)
        };
        let mut panel = Panel::new();
        let (a, b) = panel.frame(vec![], boxes);
        for (from, to) in [(a, b), (b, a)] {
            panel.frame(click(from), boxes);
            panel.frame(vec![egui::Event::Text("_2".into())], boxes);
            panel.frame(click(to), boxes);
            for _ in 0..3 {
                panel.frame(vec![], boxes);
            }
        }
        let (down, up) = (set(&upper, Value::text("bowl_2")), set(&lower, Value::text("apple_2")));
        assert_eq!(sent(), [down, up]);
    }

    /// Escape throws the name away, on the frame it is pressed and after.
    #[test]
    fn a_name_escaped_from_is_not_sent() {
        let path = [Step::from("objects"), Step::from(0usize), Step::from("id")];
        let field = |ui: &mut egui::Ui, state: &mut PanelState| text_field(ui, &path, "bowl", state).rect.center();
        let mut panel = Panel::new();
        let at = panel.frame(vec![], field);
        panel.frame(click(at), field);
        panel.frame(vec![egui::Event::Text("_2".into())], field);
        panel.frame(key(egui::Key::Escape), field);
        for _ in 0..3 {
            panel.frame(vec![], field);
        }
        assert_eq!(sent(), []);
    }

    /// A number typed and entered goes once, and stays on screen after, so it
    /// does not flick back to the old value before the spec carries it.
    #[test]
    fn a_number_entered_is_sent_once_and_kept() {
        let path = [Step::from("relations"), Step::from(1usize), Step::from("params"), Step::from("x")];
        let field = |ui: &mut egui::Ui, state: &mut PanelState| number(ui, Some(0.5), &path, state).rect.center();
        let mut panel = Panel::new();
        let at = panel.frame(vec![], field);
        panel.frame(click(at), field);
        // Over the old value: a `DragValue` selects all of it as it takes the
        // caret.
        panel.frame(vec![egui::Event::Text("0.25".into())], field);
        panel.frame(key(egui::Key::Enter), field);
        for _ in 0..3 {
            panel.frame(vec![], field);
        }
        assert_eq!(sent(), [set(&path, Value::Number(0.25))]);
        let kept = panel.state.editing.as_ref().map(|editing| (editing.value, editing.sent));
        assert_eq!(kept, Some((0.25, true)));
    }

    /// Escape out of a number sends nothing — not even a frame later, when
    /// egui reports the lost focus again and `DragValue` still holds the text.
    #[test]
    fn a_number_escaped_from_is_not_sent() {
        let path = [Step::from("relations"), Step::from(1usize), Step::from("params"), Step::from("x")];
        let field = |ui: &mut egui::Ui, state: &mut PanelState| number(ui, None, &path, state).rect.center();
        let mut panel = Panel::new();
        let at = panel.frame(vec![], field);
        panel.frame(click(at), field);
        panel.frame(vec![egui::Event::Text("3".into())], field);
        panel.frame(key(egui::Key::Escape), field);
        for _ in 0..3 {
            panel.frame(vec![], field);
        }
        assert_eq!(sent(), []);
        assert!(panel.state.editing.is_none(), "the escaped value is still shown");
    }
}

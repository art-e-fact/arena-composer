//! The menu a right click on something in the 3D view opens: what it takes
//! part in, and what can be done to it.
//!
//! Its id heads the menu, and under it every relation naming it, as subject or
//! reference. Hovered, a relation is drawn over the layout, as hovering its
//! card draws it; clicked, an entry is picked, as a click on it in the graph
//! picks it. Under those, `+ relation` adds a relation about it, the kind and
//! then, for a kind that takes one, the reference each picked from a submenu,
//! so what is written is whole; and an object, a set or a reference can be
//! removed. The robot and the background are swapped rather than removed, as
//! on their cards.
//!
//! egui's own context menu, opened by hand: the view is no widget for one to
//! hang off. While it is open egui holds the pointer, so the camera and the
//! pick leave the view alone until it closes, and the click that closes it
//! does nothing else.

use bevy_egui::egui;

use super::browser::on_params;
use super::form::{relation_entry, required_params};
use super::graph::Item;
use super::{Form, send};
use crate::spec::{Relation, Request, Step};

/// What the menu was used for, on a frame it is open.
#[derive(Debug, Default, PartialEq)]
pub struct Used {
    /// The entry clicked, to pick as a click on it in the graph would.
    pub picked: Option<Item>,
    /// The relation under the pointer, by position, to draw over the layout.
    pub hovered: Option<usize>,
}

/// The menu for the node `id`, opened at the pointer where `open`; None while
/// it is closed.
pub fn show(ctx: &egui::Context, id: &str, open: bool, form: &Form) -> Option<Used> {
    let mut used = Used::default();
    let anchor = egui::PopupAnchor::PointerFixed;
    egui::Popup::new(egui::Id::new("view menu"), ctx.clone(), anchor, egui::LayerId::background())
        .kind(egui::PopupKind::Menu)
        .layout(egui::Layout::top_down_justified(egui::Align::Min))
        .style(egui::containers::menu::menu_style)
        .open_memory(open.then_some(egui::SetOpenCommand::Bool(true)))
        .show(|ui| {
            entries(ui, id, form, &mut used);
            ui.separator();
            add_relation(ui, id, form);
            remove(ui, id, form);
        })?;
    Some(used)
}

/// The node, and every relation naming it: a row each that picks it.
fn entries(ui: &mut egui::Ui, id: &str, form: &Form, used: &mut Used) {
    if ui.button(egui::RichText::new(id).strong()).clicked() {
        used.picked = Some(Item::Node(id.to_owned()));
    }
    let naming = |relation: &Relation| relation.subject == id || relation.reference.as_deref() == Some(id);
    for (index, relation) in form.spec.relations.iter().enumerate().filter(|(_, relation)| naming(relation)) {
        // Said as a sentence, subject first: `bowl on table`.
        let words = [&relation.subject, &relation.kind].into_iter().chain(&relation.reference);
        let row = ui.button(format!("🔗 {}", words.map(String::as_str).collect::<Vec<_>>().join(" ")));
        if row.hovered() {
            used.hovered = Some(index);
        }
        if row.clicked() {
            used.picked = Some(Item::Relation(index));
        }
    }
}

/// `+ relation`: a relation about `id`, of the kind picked and then, where the
/// kind takes one, against the reference picked.
fn add_relation(ui: &mut egui::Ui, id: &str, form: &Form) {
    let ids = form.spec.node_ids();
    ui.menu_button("+ relation", |ui| {
        for kind in &form.vocabulary.relations {
            let add = |reference: &str| {
                let mut params = required_params(kind, &ids);
                // As an add from the browser stands one object on another.
                if kind.name == "on" {
                    params.extend(on_params(&form.spec, reference));
                }
                let value = relation_entry(kind, id, reference, params);
                send(Request::Insert { path: vec![Step::from("relations")], value });
            };
            if kind.unary == Some(false) {
                ui.menu_button(&kind.name, |ui| {
                    for &reference in ids.iter().filter(|other| **other != id) {
                        if ui.button(reference).clicked() {
                            add(reference);
                        }
                    }
                });
            } else if ui.button(&kind.name).clicked() {
                add("");
            }
        }
    });
}

/// Removes `id`'s entry, where it is one of a list's: the robot and the
/// background are swapped rather than removed.
fn remove(ui: &mut egui::Ui, id: &str, form: &Form) {
    let Some(path) = Item::Node(id.to_owned()).path(&form.spec).filter(|path| path.len() == 2) else {
        return;
    };
    if ui.button("remove").clicked() {
        send(Request::Remove { path });
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::editor::tests::SENT;
    use crate::spec::{Kind, Node, Value};

    /// A table, the anchor, with a bowl on it and a cup on it beside the bowl.
    fn form() -> Form {
        let relation = |kind: &str, subject: &str, reference: Option<&str>| Relation {
            kind: kind.into(),
            subject: subject.into(),
            reference: reference.map(Into::into),
            ..Default::default()
        };
        let kind = |name: &str, unary| Kind { name: name.into(), unary: Some(unary), ..Default::default() };
        let mut form = Form::default();
        form.spec.background = Node { id: "table".into(), ..Default::default() };
        form.spec.objects = ["bowl", "cup"].map(|id| Node { id: id.into(), ..Default::default() }).into();
        form.spec.relations = vec![
            relation("is_anchor", "table", None),
            relation("on", "bowl", Some("table")),
            relation("next_to", "cup", Some("bowl")),
            relation("on", "cup", Some("table")),
        ];
        form.vocabulary.relations = vec![kind("is_anchor", true), kind("on", false)];
        form
    }

    /// Every text drawn, and the middle of where it was drawn.
    type Texts = Vec<(String, egui::Pos2)>;

    fn at(texts: &Texts, text: &str) -> egui::Pos2 {
        let found = texts.iter().find(|(drawn, _)| drawn == text);
        found.unwrap_or_else(|| panic!("no {text:?} in {texts:?}")).1
    }

    fn count(texts: &Texts, text: &str) -> usize {
        texts.iter().filter(|(drawn, _)| drawn == text).count()
    }

    /// The menu alone, a frame at a time, opened on one node.
    struct View {
        ctx: egui::Context,
        form: Form,
        time: f64,
    }

    impl View {
        /// Opened on `id` with the pointer at the middle of the screen, and
        /// drawn once more: a popup is laid out unseen on the frame it opens.
        fn open(id: &str) -> (Self, Texts) {
            SENT.with_borrow_mut(Vec::clear);
            let mut view = Self { ctx: egui::Context::default(), form: form(), time: 0.0 };
            view.frame(id, false, vec![egui::Event::PointerMoved(egui::pos2(200.0, 150.0))]);
            view.frame(id, true, vec![]);
            let (_, texts) = view.frame(id, false, vec![]);
            (view, texts)
        }

        /// One frame with `events`: what the menu was used for, if it is open,
        /// and where it drew each text.
        fn frame(&mut self, id: &str, open: bool, events: Vec<egui::Event>) -> (Option<Used>, Texts) {
            // A second a frame, so no two clicks read as a double click.
            self.time += 1.0;
            let input = egui::RawInput {
                events,
                time: Some(self.time),
                screen_rect: Some(egui::Rect::from_min_size(egui::Pos2::ZERO, egui::vec2(800.0, 600.0))),
                ..Default::default()
            };
            let mut used = None;
            let output = self.ctx.run_ui(input, |ui| used = show(ui.ctx(), id, open, &self.form));
            let texts = output
                .shapes
                .iter()
                .filter_map(|clipped| match &clipped.shape {
                    egui::Shape::Text(text) => {
                        let at = egui::Rect::from_min_size(text.pos, text.galley.size()).center();
                        Some((text.galley.text().to_owned(), at))
                    }
                    _ => None,
                })
                .collect();
            output.drop_without_applying_deltas();
            (used, texts)
        }

        /// Hovers `text`, then waits a frame for whatever that opens to be
        /// laid out, and another for it to be drawn.
        fn hover(&mut self, id: &str, texts: &Texts, text: &str) -> Texts {
            self.frame(id, false, vec![egui::Event::PointerMoved(at(texts, text))]);
            self.frame(id, false, vec![]);
            self.frame(id, false, vec![]).1
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

    /// The bowl's menu lists the relations naming the bowl, either way round,
    /// and no other. Hovering one asks for it to be drawn, and clicking it
    /// picks it and closes the menu.
    #[test]
    fn the_menu_lists_the_relations_naming_its_node() {
        let (mut view, texts) = View::open("bowl");
        let listed = ["bowl", "🔗 bowl on table", "🔗 cup next_to bowl", "🔗 cup on table", "🔗 table is_anchor"];
        assert_eq!(listed.map(|text| count(&texts, text)), [1, 1, 1, 0, 0], "{texts:?}");

        let row = at(&texts, "🔗 cup next_to bowl");
        let (used, _) = view.frame("bowl", false, vec![egui::Event::PointerMoved(row)]);
        assert_eq!(used, Some(Used { picked: None, hovered: Some(2) }));
        let (used, _) = view.frame("bowl", false, click(row));
        assert_eq!(used.and_then(|used| used.picked), Some(Item::Relation(2)));
        assert_eq!(view.frame("bowl", false, vec![]).0, None, "still open");
        assert_eq!(SENT.with_borrow(Clone::clone), [], "a pick edits nothing");
    }

    /// `+ relation`, then a kind, then a reference, writes one whole relation
    /// about the node, and none naming itself is offered. An `on` onto an
    /// object asks for the footprints to overlap, as an add from the browser
    /// does.
    #[test]
    fn a_relation_is_added_whole_from_submenus() {
        let (mut view, texts) = View::open("bowl");
        let texts = view.hover("bowl", &texts, "+ relation");
        let texts = view.hover("bowl", &texts, "on");
        // The bowl once, heading the menu, and not again among what it can
        // stand on.
        assert_eq!(["table", "cup", "bowl"].map(|text| count(&texts, text)), [1, 1, 1], "{texts:?}");
        view.frame("bowl", false, click(at(&texts, "cup")));

        let on_the_cup = Value::map([
            ("kind", Value::text("on")),
            ("subject", Value::text("bowl")),
            ("reference", Value::text("cup")),
            ("params", Value::map([("overlap", Value::Bool(true))])),
        ]);
        let added = Request::Insert { path: vec![Step::from("relations")], value: on_the_cup };
        assert_eq!(SENT.with_borrow(Clone::clone), [added]);
        assert_eq!(view.frame("bowl", false, vec![]).0, None, "still open");
    }

    /// An object is removed by its place in the list. The background is
    /// swapped, never removed, so its menu offers no removal.
    #[test]
    fn only_a_list_entry_can_be_removed() {
        let (mut view, texts) = View::open("cup");
        view.frame("cup", false, click(at(&texts, "remove")));
        let removed = Request::Remove { path: vec![Step::from("objects"), Step::from(1usize)] };
        assert_eq!(SENT.with_borrow(Clone::clone), [removed]);

        let (_, texts) = View::open("table");
        assert_eq!(["+ relation", "remove"].map(|text| count(&texts, text)), [1, 0], "{texts:?}");
    }
}

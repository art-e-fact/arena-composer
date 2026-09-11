//! The asset browser: the registry, searchable, one click to add an object.

use bevy_egui::egui;

use super::{Form, send};
use crate::spec::{CatalogAsset, Edit, Step, Value};

pub fn show(ui: &mut egui::Ui, form: &Form, search: &mut String) {
    ui.horizontal(|ui| {
        ui.label("assets");
        ui.add(
            egui::TextEdit::singleline(search)
                .hint_text("search name and tags")
                .desired_width(f32::INFINITY),
        );
    });

    let terms: Vec<String> = search.split_whitespace().map(str::to_lowercase).collect();
    let found: Vec<&CatalogAsset> = form
        .vocabulary
        .assets
        .iter()
        .filter(|asset| asset.role == "object" && matches(asset, &terms))
        .collect();

    let row = ui.spacing().interact_size.y;
    egui::ScrollArea::vertical()
        .auto_shrink(false)
        // Only the visible rows are drawn, which is what makes listing the
        // whole registry — a few hundred assets — cost nothing.
        .show_rows(ui, row, found.len(), |ui, visible| {
            for asset in &found[visible] {
                if ui.button(&asset.registry_name).clicked() {
                    add(form, &asset.registry_name);
                }
            }
        });
}

/// Whether every whitespace-separated term appears in the name or a tag, the
/// same test `arena_composer.catalog.search` makes.
fn matches(asset: &CatalogAsset, terms: &[String]) -> bool {
    let haystack = format!("{} {}", asset.registry_name, asset.tags.join(" ")).to_lowercase();
    terms.iter().all(|term| haystack.contains(term))
}

/// The id is worked out on the click rather than when the row is drawn, so
/// adding two of the same asset gives two ids and not one.
fn add(form: &Form, registry_name: &str) {
    let id = unused(registry_name, &form.spec.node_ids());
    send(Edit::Insert {
        path: vec![Step::from("objects")],
        value: Value::map([
            ("id", Value::text(id)),
            ("registry_name", Value::text(registry_name)),
            ("params", Value::map([])),
        ]),
    });
}

/// `stem`, or the lowest numbered variant of it that is not already an id.
fn unused(stem: &str, taken: &[&str]) -> String {
    if !taken.contains(&stem) {
        return stem.to_owned();
    }
    (2..)
        .map(|n| format!("{stem}_{n}"))
        .find(|id| !taken.contains(&id.as_str()))
        .expect("an unused id")
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Adding the same asset twice has to give two objects, not one id twice —
    /// Arena refuses a duplicate, and a refused add looks like a dead button.
    #[test]
    fn an_added_object_does_not_take_an_id_that_is_used() {
        assert_eq!(unused("apple", &["banana"]), "apple");
        assert_eq!(unused("apple", &["apple"]), "apple_2");
        assert_eq!(unused("apple", &["apple", "apple_2"]), "apple_3");
    }

    /// Every term has to land, and a tag counts the same as the name.
    #[test]
    fn a_search_narrows_by_every_term() {
        let asset = CatalogAsset {
            registry_name: "banana_ycb_robolab".into(),
            tags: vec!["fruit".into(), "small".into()],
            ..Default::default()
        };
        assert!(matches(&asset, &["banana".into(), "fruit".into()]));
        assert!(matches(&asset, &[]));
        assert!(!matches(&asset, &["banana".into(), "large".into()]));
    }
}

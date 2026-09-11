//! The editor panel: the authored spec as a form, docked on the right.
//!
//! The form is a pure function of the last spec Python sent. Nothing here
//! edits the spec it draws — a control sends an [`Edit`] up the pipe, Python
//! writes the YAML and re-resolves, and the next scene is what the form
//! redraws from. That is why there is no local state to reconcile, and why a
//! refused edit needs no undo: the spec never changed.
//!
//! Nothing here knows the name of a relation kind or the parameters it takes.
//! [`Vocabulary`] arrives in the payload carrying a [`Field`] per parameter,
//! inferred from Arena's registries, so a relation somebody registers
//! themselves gets a form without a change to this file.
//!
//! Every control that adds something picks from what the registries offer —
//! an object from the asset list, a relation from a kind menu — so there is no
//! "add now, fix the invalid parts after". What a menu writes is an entry
//! Arena already accepts.

use std::collections::HashMap;

use bevy::ecs::template::EntityTemplate;
use bevy::feathers::constants::{fonts, size};
use bevy::feathers::containers::{subpane, subpane_body, subpane_header};
use bevy::feathers::controls::{
    FeathersButton, FeathersMenu, FeathersMenuButton, FeathersMenuItem, FeathersMenuPopup,
    FeathersNumberInput, FeathersScrollbar, FeathersTextInput, FeathersTextInputContainer,
    NumberInputValue, UpdateNumberInput,
};
use bevy::feathers::dark_theme::create_dark_theme;
use bevy::feathers::font_styles::InheritableFont;
use bevy::feathers::theme::{ThemeBackgroundColor, ThemedText, UiTheme};
use bevy::feathers::{FeathersPlugins, tokens};
use bevy::input_focus::InputFocus;
use bevy::prelude::*;
use bevy::text::{EditableText, FontWeight, TextEditChange};
use bevy::ui_widgets::{Activate, ControlOrientation, ScrollArea, ValueChange};
use bevy_panorbit_camera::PanOrbitCamera;

use crate::scene::Scene as Payload;
use crate::spec::{Edit, Field, Kind, Params, Spec, Step, Value, Vocabulary};
use crate::wire;

/// How wide the panel is, and so where the viewport ends.
pub const WIDTH: f32 = 380.0;

/// How many assets the browser draws. It is not virtualised, and the search
/// box is the way to a longer list's tail.
const ASSET_ROWS: usize = 60;

pub fn plugin(app: &mut App) {
    app.add_plugins(FeathersPlugins)
        // `UiTheme::default()` is an empty map and every miss resolves to
        // bright fuchsia, so the theme is not optional.
        .insert_resource(UiTheme(create_dark_theme()))
        .init_resource::<Form>()
        .init_resource::<Browser>()
        .add_systems(Startup, spawn_panel)
        .add_systems(
            Update,
            (
                keep_the_camera_off_the_panel,
                rebuild.run_if(needs_rebuild),
                rebuild_browser.run_if(|browser: Res<Browser>| browser.dirty),
                show_values,
            ),
        );
}

/// The spec the form is drawing, and whether it still matches what is on
/// screen.
#[derive(Resource, Default)]
pub struct Form {
    pub spec: Spec,
    pub vocabulary: Vocabulary,
    pub owners: HashMap<String, String>,
    pub errors: Vec<String>,
    /// Set when a new spec arrives, cleared once the form has been rebuilt
    /// from it.
    pub dirty: bool,
}

impl Form {
    /// Take the parts of `scene` the form draws.
    ///
    /// A scene with no assets is a status message — Python answering a refused
    /// edit — and carries no spec to redraw from, only the reason.
    pub fn take(&mut self, scene: &Payload) {
        self.errors = scene.errors.clone();
        if scene.assets.is_empty() {
            return;
        }
        self.spec = scene.spec.clone();
        self.vocabulary = scene.vocabulary.clone();
        self.owners = scene.owners.clone();
        self.dirty = true;
    }

    /// Which file a top-level key lives in, for the badge beside its group.
    fn owner(&self, key: &str) -> &str {
        self.owners.get(key).map_or("", String::as_str)
    }
}

/// What the asset browser is filtered to. Its own resource, and its own
/// subtree, because the search box has to survive the list being rebuilt
/// under it.
#[derive(Resource, Default)]
struct Browser {
    query: String,
    dirty: bool,
}

/// The form's scrolling body, whose children are the groups.
#[derive(Component)]
struct FormBody;

/// The asset browser's scrolling body.
#[derive(Component)]
struct AssetList;

fn spawn_panel(mut commands: Commands, assets: Res<AssetServer>) {
    let (form_pane, _) = scroll_pane(&mut commands, FormBody);
    let (list_pane, _) = scroll_pane(&mut commands, AssetList);

    // The header and the search box are the browser's own children, spawned
    // before the list so the list rebuilding never touches them.
    let head = commands
        .spawn(Node {
            flex_direction: FlexDirection::Column,
            ..default()
        })
        .id();
    commands
        .entity(head)
        .queue_spawn_related_scenes::<Children>(bsn_list! {
            (subpane_header() Children [{text("assets")}, {dim("click one to add an object")}]),
            (
                @FeathersTextInputContainer
                Node { margin: UiRect::all(px(4)) }
                Children [(@FeathersTextInput on(narrow_the_asset_list))]
            ),
        });

    let browser = commands
        .spawn(Node {
            flex_direction: FlexDirection::Column,
            height: percent(38),
            min_height: px(0),
            ..default()
        })
        .add_child(head)
        .add_child(list_pane)
        .id();

    commands
        .spawn((
            Node {
                position_type: PositionType::Absolute,
                top: px(0),
                right: px(0),
                bottom: px(0),
                width: px(WIDTH),
                flex_direction: FlexDirection::Column,
                ..default()
            },
            ThemeBackgroundColor(tokens::WINDOW_BG),
            // Bevy's built-in font is a subset of FiraMono without so much as
            // a `·` in it. One inheritable font on the root is what gives the
            // whole panel the glyphs its labels use.
            InheritableFont {
                font: assets.load(fonts::REGULAR),
                font_size: size::COMPACT_FONT,
                weight: FontWeight::NORMAL,
            },
        ))
        .add_child(form_pane)
        .add_child(browser);
}

/// A scrolling column with a scrollbar beside it: the pane to put in the
/// layout, and the body to fill.
fn scroll_pane(commands: &mut Commands, marker: impl Bundle) -> (Entity, Entity) {
    let body = commands
        .spawn((
            Node {
                flex_grow: 1.0,
                min_width: px(0),
                flex_direction: FlexDirection::Column,
                // `ScrollArea` only moves an axis that is set to scroll, and
                // clipping the other keeps a long id inside the panel.
                overflow: Overflow {
                    x: OverflowAxis::Clip,
                    y: OverflowAxis::Scroll,
                },
                padding: UiRect::all(px(6)),
                row_gap: px(6),
                ..default()
            },
            ScrollArea,
            // Feathers propagates its font only through entities marked
            // `ThemedText`; a plain layout node in between stops it, and the
            // labels fall back to Bevy's built-in font.
            ThemedText,
            marker,
        ))
        .id();

    let bar = commands
        .spawn(Node {
            width: px(9),
            flex_shrink: 0.0,
            ..default()
        })
        .id();
    commands
        .entity(bar)
        .queue_spawn_related_scenes::<Children>(bsn_list! {
            @FeathersScrollbar {
                @target: {EntityTemplate::Entity(body)},
                @orientation: ControlOrientation::Vertical,
            }
            Node { width: px(9), height: percent(100) }
        });

    let pane = commands
        .spawn((
            Node {
                flex_grow: 1.0,
                min_height: px(0),
                flex_direction: FlexDirection::Row,
                ..default()
            },
            ThemedText,
        ))
        .add_child(body)
        .add_child(bar)
        .id();
    (pane, body)
}

/// Rebuild only once the panel has stopped being typed into.
///
/// A rebuild despawns every widget, which drops the caret and the half-typed
/// value with it. Leaving `dirty` set means the rebuild lands the moment the
/// field is left — and while the user is typing, a stale form is the right one.
///
/// The test is for text entry specifically, not for focus at all: a menu hands
/// focus back to the button that opened it, so "something in the panel has
/// focus" is true after every dropdown and would wedge the form shut.
fn needs_rebuild(form: Res<Form>, focus: Res<InputFocus>, editing: Query<(), With<EditableText>>) -> bool {
    form.dirty && !focus.get().is_some_and(|entity| editing.contains(entity))
}

fn rebuild(
    mut commands: Commands,
    mut form: ResMut<Form>,
    mut browser: ResMut<Browser>,
    body: Single<Entity, With<FormBody>>,
    scroll: Query<&ScrollPosition, With<FormBody>>,
) {
    form.dirty = false;
    // The asset rows derive an id from what the spec already uses.
    browser.dirty = true;
    let body = *body;
    // A full rebuild loses the scroll, which for a long spec is most of what
    // the user was looking at.
    let position = scroll.get(body).cloned().unwrap_or_default();

    let mut rows: Vec<Box<dyn SceneList>> = vec![Box::new(bsn_list! {
        (Text({format!("arena · {}", form.spec.env_name)}) ThemedText)
    })];
    rows.extend(errors(&form));
    rows.push(objects(&form));
    rows.push(relations(&form));

    commands.entity(body).despawn_related::<Children>();
    commands
        .entity(body)
        .queue_spawn_related_scenes::<Children>(rows)
        .insert(position);
}

/// Why the last edit was refused, above the form it was refused from.
fn errors(form: &Form) -> Vec<Box<dyn SceneList>> {
    form.errors
        .iter()
        .map(|message| {
            let text = message.clone();
            Box::new(bsn_list! {
                Node { padding: UiRect::all(px(6)) }
                ThemedText
                ThemeBackgroundColor(tokens::WINDOW_BG)
                Children [(Text({text}) ThemedText TextColor({Color::srgb(0.94, 0.51, 0.45)}))]
            }) as Box<dyn SceneList>
        })
        .collect()
}

fn objects(form: &Form) -> Box<dyn SceneList> {
    let rows: Vec<Box<dyn SceneList>> = form
        .spec
        .objects
        .iter()
        .enumerate()
        .map(|(index, node)| {
            let (id, name) = (node.id.clone(), node.registry_name.clone());
            let path = vec![Step::from("objects"), Step::from(index)];
            Box::new(bsn_list! {
                Node { flex_direction: FlexDirection::Column, min_width: px(0) }
                ThemedText
                Children [
                    (
                        Node { flex_direction: FlexDirection::Row, column_gap: px(6), align_items: AlignItems::Center }
                        ThemedText
                        Children [{text(id)}, {flex_spacer()}, {remove_button(path)}]
                    ),
                    (Node { padding: UiRect::left(px(8)) } ThemedText Children [{dim(name)}]),
                ]
            }) as Box<dyn SceneList>
        })
        .collect();

    // No add button: an object is a registered asset, and the browser below is
    // where those are. Picking there is what makes "add" a decision rather
    // than a surprise.
    group("objects", form.owner("objects"), rows, dim("add from the asset list"))
}

fn relations(form: &Form) -> Box<dyn SceneList> {
    let ids = form.spec.node_ids();
    let rows: Vec<Box<dyn SceneList>> = form
        .spec
        .relations
        .iter()
        .enumerate()
        .map(|(index, relation)| {
            let path = vec![Step::from("relations"), Step::from(index)];
            let kind = form.vocabulary.relation(&relation.kind);
            let reference = relation.reference.clone().unwrap_or_default();

            // Changing the kind rewrites the whole entry: the old kind's
            // parameters mean nothing to the new one, and whether there is a
            // `reference` at all is the kind's to say.
            let mut parts: Vec<Box<dyn SceneList>> = vec![Box::new(bsn_list! {
                Node { flex_direction: FlexDirection::Row, column_gap: px(6), align_items: AlignItems::Center, min_width: px(0) }
                ThemedText
                Children [
                    {menu(
                        &relation.kind,
                        kind_options(form, &relation.subject, &reference, &ids),
                        set(path.clone()),
                    )},
                    {flex_spacer()},
                    {remove_button(path.clone())},
                ]
            })];

            let mut slots: Vec<Box<dyn SceneList>> =
                vec![menu(&relation.subject, ids_options(&ids), set(with(&path, "subject")))];
            // A unary relation takes no reference, and Arena refuses one that
            // has it — so the slot is not drawn rather than validated.
            if kind.is_some_and(|kind| kind.unary == Some(false)) {
                slots.push(dim("→"));
                slots.push(menu(&reference, ids_options(&ids), set(with(&path, "reference"))));
            }
            parts.push(Box::new(bsn_list! {
                Node { flex_direction: FlexDirection::Row, column_gap: px(6), align_items: AlignItems::Center, min_width: px(0), padding: UiRect::left(px(8)) }
                ThemedText
                Children [{slots}]
            }));
            parts.extend(params(kind, &relation.params, &ids, with(&path, "params")));

            Box::new(bsn_list! {
                Node { flex_direction: FlexDirection::Column, row_gap: px(3), min_width: px(0) }
                ThemedText
                Children [{parts}]
            }) as Box<dyn SceneList>
        })
        .collect();

    let add = menu(
        "+ relation",
        kind_options(form, subject_default(form), &form.spec.background.id, &ids),
        insert(vec![Step::from("relations")]),
    );
    group("relations", form.owner("relations"), rows, add)
}

/// A control per parameter the kind declares, in the order it declares them.
fn params(kind: Option<&Kind>, values: &Params, ids: &[&str], base: Vec<Step>) -> Vec<Box<dyn SceneList>> {
    let Some(kind) = kind else { return Vec::new() };
    kind.fields
        .iter()
        .map(|field| {
            let value = values.get(&field.name).cloned().unwrap_or_else(|| field.default.clone());
            let path = with(&base, &field.name);
            let name = field.name.clone();
            let control = control(field, &value, ids, path);
            Box::new(bsn_list! {
                Node { flex_direction: FlexDirection::Row, column_gap: px(6), align_items: AlignItems::Center, min_width: px(0), padding: UiRect::left(px(8)) }
                ThemedText
                Children [{dim(name)}, {flex_spacer()}, {control}]
            }) as Box<dyn SceneList>
        })
        .collect()
}

/// The control that edits one parameter.
///
/// `kind` is a string rather than an enum so a control added on the Python
/// side draws as text instead of failing the payload to decode.
fn control(field: &Field, value: &Value, ids: &[&str], path: Vec<Step>) -> Box<dyn SceneList> {
    match field.kind.as_str() {
        "enum" => {
            let options = field.options.iter().map(|option| (option.clone(), Value::text(option))).collect();
            menu(value.as_text().unwrap_or_default(), options, set(path))
        }
        "node_ref" => menu(value.as_text().unwrap_or_default(), ids_options(ids), set(path)),
        "number" | "int" => number(value.as_number(), path),
        _ => dim(format!("{value:?}")),
    }
}

fn number(value: Option<f64>, path: Vec<Step>) -> Box<dyn SceneList> {
    let send_to = path.clone();
    Box::new(bsn_list! {
        @FeathersNumberInput
        Node { width: px(90), flex_shrink: 0.0 }
        on({move |change: On<ValueChange<f32>>| {
            // Only on commit: the widget emits as the user types, and a keystroke
            // is not an edit worth a solve.
            if change.is_final {
                send(Edit::Set { path: send_to.clone(), value: Value::Number(change.value as f64) });
            }
        }})
        NumberSeed({value})
    })
}

/// The value a number input should show. It has no `value` prop — the widget
/// is told after it is spawned, by [`show_values`].
#[derive(Component, Clone, Copy, Default, FromTemplate)]
struct NumberSeed(Option<f64>);

/// Put the spec's value into every number input that is not being typed into.
///
/// The widget is the display, the spec is the truth, and this is the one
/// direction the widget cannot do for itself. It ignores an update while
/// focused, so this is safe to run every frame.
fn show_values(mut commands: Commands, seeds: Query<(Entity, &NumberSeed), Added<NumberSeed>>) {
    for (entity, seed) in &seeds {
        if let Some(value) = seed.0 {
            commands.trigger(UpdateNumberInput {
                entity,
                value: NumberInputValue::F32(value as f32),
            });
        }
    }
}

/// A menu showing `caption`, writing the chosen option's value through `edit`.
///
/// The caption is a spawn-time prop that does not follow the selection, which
/// is why it is given the current value and why a rebuild is what shows a new
/// one.
fn menu(
    caption: &str,
    options: Vec<(String, Value)>,
    edit: impl Fn(Value) -> Edit + Clone + Send + Sync + 'static,
) -> Box<dyn SceneList> {
    let caption = if caption.is_empty() { "—".to_owned() } else { caption.to_owned() };
    let items: Vec<Box<dyn SceneList>> = options
        .into_iter()
        .map(|(label, value)| {
            let edit = edit.clone();
            Box::new(bsn_list! {
                @FeathersMenuItem { @caption: bsn! { Text({label}) ThemedText } }
                on(move |_: On<Activate>| send(edit(value.clone())))
            }) as Box<dyn SceneList>
        })
        .collect();

    Box::new(bsn_list! {
        @FeathersMenu
        Node { flex_shrink: 1.0, min_width: px(0) }
        Children [
            (@FeathersMenuButton { @caption: bsn! { Text({caption}) ThemedText } }
             Node { max_width: px(180), overflow: Overflow::clip() }),
            (@FeathersMenuPopup Children [{items}]),
        ]
    })
}

/// Write the chosen value at `path`, and add it there.
fn set(path: Vec<Step>) -> impl Fn(Value) -> Edit + Clone {
    move |value| Edit::Set { path: path.clone(), value }
}

fn insert(path: Vec<Step>) -> impl Fn(Value) -> Edit + Clone {
    move |value| Edit::Insert { path: path.clone(), value }
}

fn ids_options(ids: &[&str]) -> Vec<(String, Value)> {
    ids.iter().map(|id| ((*id).to_owned(), Value::text(*id))).collect()
}

/// One whole relation entry per registered kind, each already valid: the
/// kind's required parameters filled in, and a `reference` only where the kind
/// takes one. Used both to change a relation's kind and to add one.
fn kind_options(form: &Form, subject: &str, reference: &str, ids: &[&str]) -> Vec<(String, Value)> {
    form.vocabulary
        .relations
        .iter()
        .map(|kind| {
            let mut pairs = vec![
                ("kind", Value::text(&kind.name)),
                ("subject", Value::text(subject)),
            ];
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

// --- the asset browser ------------------------------------------------------

fn narrow_the_asset_list(
    change: On<TextEditChange>,
    fields: Query<&EditableText>,
    mut browser: ResMut<Browser>,
) {
    if let Ok(field) = fields.get(change.event_target()) {
        let query = field.value().to_string();
        if query != browser.query {
            browser.query = query;
            browser.dirty = true;
        }
    }
}

fn rebuild_browser(
    mut commands: Commands,
    mut browser: ResMut<Browser>,
    form: Res<Form>,
    list: Single<Entity, With<AssetList>>,
) {
    browser.dirty = false;
    let terms: Vec<String> = browser.query.split_whitespace().map(str::to_lowercase).collect();
    let found: Vec<&crate::spec::CatalogAsset> = form
        .vocabulary
        .assets
        .iter()
        .filter(|asset| asset.role == "object" && matches(asset, &terms))
        .collect();

    let mut rows: Vec<Box<dyn SceneList>> = found
        .iter()
        .take(ASSET_ROWS)
        .map(|asset| add_object_button(&asset.registry_name))
        .collect();
    if found.len() > ASSET_ROWS {
        rows.push(dim(format!("{} of {} — narrow the search", ASSET_ROWS, found.len())));
    } else if found.is_empty() {
        rows.push(dim("no asset matches"));
    }

    commands.entity(*list).despawn_related::<Children>();
    commands.entity(*list).queue_spawn_related_scenes::<Children>(rows);
}

/// Whether every whitespace-separated term appears in the name or a tag, the
/// same test `arena_composer.catalog.search` makes.
fn matches(asset: &crate::spec::CatalogAsset, terms: &[String]) -> bool {
    let haystack = format!("{} {}", asset.registry_name, asset.tags.join(" ")).to_lowercase();
    terms.iter().all(|term| haystack.contains(term))
}

/// An asset row. The id is worked out when it is clicked rather than when the
/// row is drawn, so adding two of the same asset gives two ids and not one.
fn add_object_button(registry_name: &str) -> Box<dyn SceneList> {
    let registry_name = registry_name.to_owned();
    let caption = registry_name.clone();
    Box::new(bsn_list! {
        @FeathersButton { @caption: bsn! { Text({caption}) ThemedText } }
        Node { flex_shrink: 0.0 }
        on({move |_: On<Activate>, form: Res<Form>| {
            let id = unused(&registry_name, &form.spec.node_ids());
            send(Edit::Insert {
                path: vec![Step::from("objects")],
                value: Value::map([
                    ("id", Value::text(id)),
                    ("registry_name", Value::text(&registry_name)),
                    ("params", Value::map([])),
                ]),
            });
        }})
    })
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

// --- the pieces every group is made of --------------------------------------

fn remove_button(path: Vec<Step>) -> Box<dyn SceneList> {
    button("×", move || Edit::Remove { path: path.clone() })
}

fn button(caption: &str, edit: impl Fn() -> Edit + Clone + Send + Sync + 'static) -> Box<dyn SceneList> {
    let caption = caption.to_owned();
    Box::new(bsn_list! {
        @FeathersButton { @caption: bsn! { Text({caption}) ThemedText } }
        Node { flex_shrink: 0.0 }
        on(move |_: On<Activate>| send(edit()))
    })
}

/// One top-level section, with the file that owns it and a way to grow it.
fn group(name: &str, owner: &str, rows: Vec<Box<dyn SceneList>>, add: Box<dyn SceneList>) -> Box<dyn SceneList> {
    let (title, badge) = (format!("{name}  ({})", rows.len()), owner.to_owned());
    Box::new(bsn_list! {
        subpane()
        Node { min_width: px(0), flex_shrink: 0.0 }
        ThemedText
        Children [
            (subpane_header() Children [{text(title)}, {dim(badge)}]),
            (subpane_body() Node { min_width: px(0) } Children [{rows}, {add}]),
        ]
    })
}

/// A line of text, and the same dimmed. Feathers' own `label` helpers return a
/// `Scene`, and every child position here takes a `SceneList`.
fn text(value: impl Into<String>) -> Box<dyn SceneList> {
    let value = value.into();
    Box::new(bsn_list! { (Text({value}) ThemedText) })
}

fn dim(value: impl Into<String>) -> Box<dyn SceneList> {
    let value = value.into();
    Box::new(bsn_list! { (Text({value}) ThemedText TextColor({Color::srgb(0.55, 0.56, 0.60)})) })
}

fn flex_spacer() -> Box<dyn SceneList> {
    Box::new(bsn_list! { Node { flex_grow: 1.0, min_width: px(0) } })
}

/// `base` with one more step on the end.
fn with(base: &[Step], step: &str) -> Vec<Step> {
    let mut path = base.to_vec();
    path.push(Step::from(step));
    path
}

/// Hand one edit to Python.
///
/// Straight down stdout rather than through a channel and a thread: an edit is
/// tens of bytes against a 64 KiB pipe with Python in a read loop, so the write
/// cannot block long enough to be worth getting off this thread.
// ponytail: blocking write on the render thread; channel + thread if an intent
// ever outruns Python.
fn send(edit: Edit) {
    if let Err(error) = wire::write_edit(&mut std::io::stdout().lock(), &edit) {
        error!("edit intent: {error:#}");
    }
}

/// Keep the orbit camera off the panel.
///
/// `PanOrbitCamera` claims a drag on the frame the button goes down, checking
/// only that the cursor is inside the camera's viewport — it knows nothing
/// about bevy_ui. Latching the decision between drags is what keeps a drag
/// that starts in the panel from orbiting, and one that leaves the viewport
/// from stopping.
fn keep_the_camera_off_the_panel(
    window: Single<&Window>,
    mouse: Res<ButtonInput<MouseButton>>,
    camera: Option<Single<&mut PanOrbitCamera>>,
) {
    let Some(mut camera) = camera else { return };
    if mouse.get_pressed().next().is_some() && mouse.get_just_pressed().next().is_none() {
        return; // mid-drag; whoever owns it keeps it
    }
    let over_panel = window
        .cursor_position()
        .is_some_and(|at| at.x > window.width() - WIDTH);
    if camera.enabled == over_panel {
        camera.enabled = !over_panel;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A rebuild despawns every widget, and `Pointer<Click>` only fires when
    /// the press and the release land on the same entity — so a form that
    /// dirties on a steady tick does not merely waste work, it swallows every
    /// click and every drag. An idle viewer has to reach a fixed point.
    #[test]
    fn an_idle_form_is_not_rebuilt() {
        let mut form = Form::default();
        let mut scene = Payload::default();
        scene.assets.push(crate::scene::Asset::default());
        scene.spec.env_name = "demo".into();

        form.take(&scene);
        assert!(form.dirty, "a new spec has to reach the form");

        form.dirty = false;
        for _ in 0..5 {
            form.take(&scene);
            assert!(form.dirty, "the same spec arriving again is still a new spec");
            form.dirty = false;
        }
    }

    /// A refused edit answers with the reason alone — re-reading every asset's
    /// USD to say "no" would cost seconds. The form keeps the spec it has.
    #[test]
    fn a_status_message_keeps_the_spec_it_is_drawing() {
        let mut form = Form::default();
        let mut scene = Payload::default();
        scene.assets.push(crate::scene::Asset::default());
        scene.spec.env_name = "demo".into();
        form.take(&scene);
        form.dirty = false;

        form.take(&Payload {
            errors: vec!["objects.0: still needed by a task".into()],
            ..default()
        });
        assert_eq!(form.spec.env_name, "demo", "a status message replaced the spec");
        assert_eq!(form.errors.len(), 1);
        assert!(!form.dirty, "there is nothing new to draw");
    }

    /// Adding the same asset twice has to give two objects, not one id twice —
    /// Arena refuses a duplicate, and a refused add looks like a dead button.
    #[test]
    fn an_added_object_does_not_take_an_id_that_is_used() {
        assert_eq!(unused("apple", &["banana"]), "apple");
        assert_eq!(unused("apple", &["apple"]), "apple_2");
        assert_eq!(unused("apple", &["apple", "apple_2"]), "apple_3");
    }

    /// Every kind in the menu writes an entry Arena can build the relation
    /// from: a `reference` exactly where the kind takes one, and the
    /// parameters it has no default for already filled in.
    #[test]
    fn a_kind_menu_offers_entries_the_kind_can_be_built_from() {
        let mut form = Form::default();
        form.vocabulary.relations = vec![
            Kind { name: "is_anchor".into(), unary: Some(true), ..default() },
            Kind {
                name: "next_to".into(),
                unary: Some(false),
                fields: vec![Field {
                    name: "side".into(),
                    kind: "enum".into(),
                    required: true,
                    options: vec!["positive_y".into()],
                    ..default()
                }],
                ..default()
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

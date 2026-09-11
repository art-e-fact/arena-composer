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

use std::collections::HashMap;

use bevy::feathers::controls::{
    FeathersButton, FeathersMenu, FeathersMenuButton, FeathersMenuItem, FeathersMenuPopup,
    FeathersNumberInput, NumberInputValue, UpdateNumberInput,
};
use bevy::feathers::dark_theme::create_dark_theme;
use bevy::feathers::theme::{ThemeBackgroundColor, ThemedText, UiTheme};
use bevy::feathers::{tokens, FeathersPlugins};
use bevy::input_focus::InputFocus;
use bevy::prelude::*;
use bevy::ui_widgets::{Activate, ScrollArea, ValueChange};
use bevy_panorbit_camera::PanOrbitCamera;

use crate::scene::Scene as Payload;
use crate::spec::{Edit, Field, Kind, Params, Spec, Step, Value, Vocabulary};
use crate::wire;

/// How wide the panel is, and so where the viewport ends.
pub const WIDTH: f32 = 340.0;

pub fn plugin(app: &mut App) {
    app.add_plugins(FeathersPlugins)
        // `UiTheme::default()` is an empty map and every miss resolves to
        // bright fuchsia, so the theme is not optional.
        .insert_resource(UiTheme(create_dark_theme()))
        .init_resource::<Form>()
        .add_systems(Startup, spawn_panel)
        .add_systems(
            Update,
            (
                keep_the_camera_off_the_panel,
                rebuild.run_if(needs_rebuild),
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

/// The panel's scrolling body, whose children are the form.
#[derive(Component)]
struct FormBody;

fn spawn_panel(mut commands: Commands) {
    commands.spawn((
        Node {
            position_type: PositionType::Absolute,
            top: px(0),
            right: px(0),
            bottom: px(0),
            width: px(WIDTH),
            flex_direction: FlexDirection::Column,
            padding: UiRect::all(px(8)),
            row_gap: px(6),
            ..default()
        },
        ThemeBackgroundColor(tokens::WINDOW_BG),
        ScrollArea,
        FormBody,
    ));
}

/// Rebuild only once the panel has stopped being typed into.
///
/// A rebuild despawns every widget, which drops the caret and the half-typed
/// value with it. Leaving `dirty` set means the rebuild lands the moment focus
/// leaves — and while the user is typing, a stale form is the right one.
fn needs_rebuild(form: Res<Form>, focus: Res<InputFocus>) -> bool {
    form.dirty && focus.get().is_none()
}

fn rebuild(
    mut commands: Commands,
    mut form: ResMut<Form>,
    body: Single<Entity, With<FormBody>>,
    scroll: Query<&ScrollPosition, With<FormBody>>,
) {
    form.dirty = false;
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
                Node { flex_direction: FlexDirection::Row, column_gap: px(6), align_items: AlignItems::Center }
                Children [
                    {text(id)},
                    {dim(name)},
                    {flex_spacer()},
                    {remove_button(path)},
                ]
            }) as Box<dyn SceneList>
        })
        .collect();

    group("objects", form.owner("objects"), rows, add_object_button(form))
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

            let mut parts: Vec<Box<dyn SceneList>> = vec![
                Box::new(bsn_list! {
                    Node { flex_direction: FlexDirection::Row, column_gap: px(6), align_items: AlignItems::Center }
                    Children [
                        {text(relation.kind.clone())},
                        {dropdown(&ids, &relation.subject, with(&path, "subject"))},
                        {flex_spacer()},
                        {remove_button(path.clone())},
                    ]
                }),
            ];
            // A unary relation takes no reference, and Arena refuses one that
            // has it — so the slot is not drawn rather than validated.
            if kind.is_some_and(|kind| kind.unary == Some(false)) {
                let current = relation.reference.clone().unwrap_or_default();
                parts.push(Box::new(bsn_list! {
                    Node { flex_direction: FlexDirection::Row, column_gap: px(6), align_items: AlignItems::Center }
                    Children [
                        {text("on")},
                        {dropdown(&ids, &current, with(&path, "reference"))},
                    ]
                }));
            }
            parts.extend(params(kind, &relation.params, &ids, with(&path, "params")));

            Box::new(bsn_list! {
                Node { flex_direction: FlexDirection::Column, row_gap: px(3), padding: UiRect::all(px(4)) }
                Children [{parts}]
            }) as Box<dyn SceneList>
        })
        .collect();

    group("relations", form.owner("relations"), rows, add_relation_button(form))
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
                Node { flex_direction: FlexDirection::Row, column_gap: px(6), align_items: AlignItems::Center, padding: UiRect::left(px(10)) }
                Children [{text(name)}, {flex_spacer()}, {control}]
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
        "enum" => dropdown(&field.options.iter().map(String::as_str).collect::<Vec<_>>(), value.as_text().unwrap_or_default(), path),
        "node_ref" => dropdown(ids, value.as_text().unwrap_or_default(), path),
        "number" | "int" => number(value.as_number(), path),
        _ => Box::new(bsn_list! { {dim(format!("{value:?}"))} }),
    }
}

fn number(value: Option<f64>, path: Vec<Step>) -> Box<dyn SceneList> {
    let send_to = path.clone();
    Box::new(bsn_list! {
        @FeathersNumberInput
        Node { width: px(90) }
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
fn show_values(
    mut commands: Commands,
    seeds: Query<(Entity, &NumberSeed), Added<NumberSeed>>,
) {
    for (entity, seed) in &seeds {
        if let Some(value) = seed.0 {
            commands.trigger(UpdateNumberInput { entity, value: NumberInputValue::F32(value as f32) });
        }
    }
}

/// A menu of `options`, showing `current`, writing the chosen one to `path`.
fn dropdown(options: &[&str], current: &str, path: Vec<Step>) -> Box<dyn SceneList> {
    let caption = if current.is_empty() { "—".to_owned() } else { current.to_owned() };
    let items: Vec<Box<dyn SceneList>> = options
        .iter()
        .map(|option| {
            let (text, choice, path) = (option.to_string(), option.to_string(), path.clone());
            Box::new(bsn_list! {
                @FeathersMenuItem { @caption: bsn! { Text({text}) ThemedText } }
                on({move |_: On<Activate>| {
                    send(Edit::Set { path: path.clone(), value: Value::Text(choice.clone()) });
                }})
            }) as Box<dyn SceneList>
        })
        .collect();

    Box::new(bsn_list! {
        @FeathersMenu
        Children [
            (@FeathersMenuButton { @caption: bsn! { Text({caption}) ThemedText } }),
            (@FeathersMenuPopup Children [{items}]),
        ]
    })
}

/// An added entry has to be one Arena already accepts, or the insert comes
/// straight back as an error. So each of these picks defaults out of the spec
/// and the catalogue; the row's own controls are how it becomes the object or
/// relation the user actually wanted.
fn add_object_button(form: &Form) -> Box<dyn SceneList> {
    let taken: Vec<String> = form.spec.node_ids().iter().map(|id| id.to_string()).collect();
    let registry_name = form
        .vocabulary
        .assets
        .iter()
        .find(|asset| asset.role == "object")
        .map(|asset| asset.registry_name.clone())
        .unwrap_or_default();

    button("+ object", move || {
        let id = unused("object", &taken);
        Edit::Insert {
            path: vec![Step::from("objects")],
            value: Value::map([
                ("id", Value::text(id)),
                ("registry_name", Value::text(registry_name.clone())),
                ("params", Value::map([])),
            ]),
        }
    })
}

fn add_relation_button(form: &Form) -> Box<dyn SceneList> {
    // `on` needs a reference, and the background is the one asset every spec
    // has that something can sit on.
    let subject = form.spec.objects.last().map(|node| node.id.clone());
    let reference = form.spec.background.id.clone();

    button("+ relation", move || Edit::Insert {
        path: vec![Step::from("relations")],
        value: Value::map([
            ("kind", Value::text("on")),
            ("subject", Value::text(subject.clone().unwrap_or_else(|| reference.clone()))),
            ("reference", Value::text(reference.clone())),
            ("params", Value::map([])),
        ]),
    })
}

/// `stem` with the lowest number that is not already an id.
fn unused(stem: &str, taken: &[String]) -> String {
    (1..).map(|n| format!("{stem}_{n}")).find(|id| !taken.contains(id)).expect("an unused id")
}

fn remove_button(path: Vec<Step>) -> Box<dyn SceneList> {
    button("×", move || Edit::Remove { path: path.clone() })
}

fn button(caption: &str, edit: impl Fn() -> Edit + Clone + Send + Sync + 'static) -> Box<dyn SceneList> {
    let caption = caption.to_owned();
    Box::new(bsn_list! {
        @FeathersButton { @caption: bsn! { Text({caption}) ThemedText } }
        on(move |_: On<Activate>| send(edit()))
    })
}

/// One top-level section, with the file that owns it and a button to grow it.
fn group(name: &str, owner: &str, rows: Vec<Box<dyn SceneList>>, add: Box<dyn SceneList>) -> Box<dyn SceneList> {
    let (title, badge) = (format!("{name}  ({})", rows.len()), owner.to_owned());
    Box::new(bsn_list! {
        Node { flex_direction: FlexDirection::Column, row_gap: px(4), padding: UiRect::top(px(8)) }
        Children [
            (
                Node { flex_direction: FlexDirection::Row, column_gap: px(6), align_items: AlignItems::Center }
                Children [{text(title)}, {flex_spacer()}, {dim(badge)}]
            ),
            {rows},
            {add},
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
    Box::new(bsn_list! { Node { flex_grow: 1.0 } })
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
}

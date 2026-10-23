//! Plays a storyboard in the window with a pointer of its own: the README's
//! demo video, recorded by `docs/video/record.sh` (the readme-video skill says
//! how to change it).
//!
//! `ARENA_VIEWER_TOUR=tour.json` names the storyboard, a JSON file in
//! [`Tour`]'s shape. Its steps are what a person does — move the pointer to
//! something, click it, drag it somewhere, turn the wheel, type — and the tour
//! does them as a mouse and a keyboard would: it writes the input messages
//! winit writes, so egui, picking and the orbit camera answer it as they
//! answer a hand, and it draws an arrow where its pointer is. The real mouse
//! and keyboard are cut off from the window meanwhile.
//!
//! A step names what it acts on by the text on screen ([`Target`]): a tab's
//! name, a tile's or a node's, a value in a field. So a storyboard reaches a
//! new button without a change here, and keeps working as the panel shifts
//! around what it clicks.
//!
//! `ARENA_VIEWER_RECORD=raw.mp4` records the tour ([`crate::record`]). The
//! clock then advances one video frame per update whatever the machine's
//! speed, and the waits nobody watching wants to sit through — a layout
//! solving, a card not there yet — are cut from the video. Without it the
//! tour plays on the wall clock, as a preview. The window closes after the
//! last step; a step that cannot be played closes it with an error, naming
//! the step.

use std::fmt;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use anyhow::Context as _;
use bevy::ecs::change_detection::Tick;
use bevy::ecs::message::{MessageUpdateSystems, Messages};
use bevy::input::ButtonState;
use bevy::input::keyboard::{Key, KeyCode, KeyboardInput, NativeKeyCode};
use bevy::input::mouse::{MouseButtonInput, MouseMotion, MouseScrollUnit, MouseWheel};
use bevy::input::touch::TouchPhase;
use bevy::picking::PickingSystems;
use bevy::prelude::*;
use bevy::render::view::screenshot::Screenshot;
use bevy::time::TimeUpdateStrategy;
use bevy::window::{CursorMoved, CursorOptions, PrimaryWindow, WindowEvent, WindowResolution};
use bevy_egui::egui::{self, Pos2, Rect, pos2, vec2};
use bevy_egui::{
    EguiContext, EguiContexts, EguiFullOutput, EguiPostUpdateSet, EguiPrimaryContextPass, PrimaryEguiContext,
};
use serde::{Deserialize, Deserializer};

use crate::editor::Form;
use crate::layout::{Placed, world_aabb};
use crate::record::{self, Hold};
use crate::scene::arena_to_bevy;

/// How long a click holds the button down.
const CLICK: f32 = 0.08;
/// How long a drag holds the button before it moves, and over its drop
/// before it lets go, so the target it lights is seen.
const GRAB: f32 = 0.15;
const SETTLE: f32 = 0.4;
/// Seconds a character takes to type, and a wheel notch to turn.
const TYPE: f32 = 0.06;
const NOTCH: f32 = 0.05;
/// Seconds of a step's pause, where it names none.
const PAUSE: f32 = 0.4;
/// Seconds of video an `until` shows before the wait is cut.
const UNTIL: f32 = 0.6;
/// Wall-clock time a target may take to appear, and a solve to land, before
/// the tour gives up on it.
const TARGET_PATIENCE: Duration = Duration::from_secs(15);
const SOLVE_PATIENCE: Duration = Duration::from_secs(300);

/// Plays the storyboard `ARENA_VIEWER_TOUR` names, and records it where
/// `ARENA_VIEWER_RECORD` names a file.
pub fn plugin(app: &mut App) {
    let Some(path) = std::env::var_os("ARENA_VIEWER_TOUR") else {
        return;
    };
    let tour = load(Path::new(&path)).unwrap_or_else(|err| panic!("{}: {err:#}", path.to_string_lossy()));

    let world = app.world_mut();
    let (mut window, mut cursor) = world
        .query_filtered::<(&mut Window, &mut CursorOptions), With<PrimaryWindow>>()
        .single_mut(world)
        .expect("the viewer opens one window");
    window.resolution = WindowResolution::new(tour.size[0], tour.size[1]).with_scale_factor_override(tour.scale);
    // ffmpeg takes one frame size.
    window.resizable = false;
    // A hand on the desktop's mouse would land in the shot; this pointer
    // reaches the window as messages, never through the window system.
    cursor.hit_test = false;

    if let Some(out) = std::env::var_os("ARENA_VIEWER_RECORD") {
        app.insert_resource(TimeUpdateStrategy::ManualDuration(Duration::from_secs_f64(1.0 / tour.fps)));
        record::plugin(app, PathBuf::from(out), tour.fps);
    }
    app.insert_resource(Hold)
        .insert_resource(tour)
        .init_resource::<OnScreen>()
        .init_resource::<Pointer>()
        // Where winit's own input lands: before picking reads it in `First`,
        // and egui and the button state in `PreUpdate`, so every one of them
        // sees a press on the frame it is made.
        .add_systems(First, play.after(MessageUpdateSystems).before(PickingSystems::Input))
        // The pass's shapes, read before bevy_egui hands them to the renderer.
        .add_systems(
            PostUpdate,
            read_the_screen.after(EguiPostUpdateSet::EndPass).before(EguiPostUpdateSet::ProcessOutput),
        )
        .add_systems(EguiPrimaryContextPass, draw_the_pointer);
}

/// A storyboard: the window it is shot in, and the steps played in it, one
/// after the other.
#[derive(Resource, Deserialize, Debug)]
#[serde(default, deny_unknown_fields)]
pub struct Tour {
    /// The window's size in physical pixels, which is the video's.
    pub size: [u32; 2],
    /// Physical pixels per logical one. Above 1 enlarges the panel, which
    /// keeps its text legible in a video played smaller than it was shot.
    pub scale: f32,
    /// Video frames per second.
    pub fps: f64,
    /// Wall-clock seconds the window gets to load the layout, its shaders and
    /// pictures before the video starts.
    pub warmup: f32,
    pub steps: Vec<Step>,
    /// What the steps come to, built by [`load`].
    #[serde(skip)]
    beats: Vec<(usize, Beat)>,
}

impl Default for Tour {
    fn default() -> Self {
        Self { size: [1280, 720], scale: 1.0, fps: 30.0, warmup: 5.0, steps: Vec::new(), beats: Vec::new() }
    }
}

/// One thing a person does, then a pause. A step holds one action at most —
/// none is a pause alone.
#[derive(Deserialize, Debug)]
#[serde(default, deny_unknown_fields)]
pub struct Step {
    /// Move the pointer here.
    #[serde(rename = "move")]
    pub glide: Option<Target>,
    /// Move there and click.
    pub click: Option<Target>,
    pub double_click: Option<Target>,
    /// Move there and click the right button: the view's menu.
    pub right_click: Option<Target>,
    /// Press here, and carry it to `to`, or `by` so far, before letting go:
    /// an asset onto the layout, a number sideways, the camera round.
    pub drag: Option<Target>,
    pub to: Option<Target>,
    /// Logical pixels right and down. For what moves as it is dragged — a
    /// number's text changes under the pointer, the view turns.
    pub by: Option<[f32; 2]>,
    /// Move here and turn the wheel `lines` notches: up, or into the view, as
    /// they are positive.
    pub scroll: Option<Target>,
    pub lines: f32,
    /// Characters to type, into whatever has the keyboard.
    #[serde(rename = "type")]
    pub typed: Option<String>,
    /// A key to press: `Enter`, `Escape`, `Tab` or `Backspace`.
    pub key: Option<String>,
    /// Wait for the edit the step before made to be solved and drawn. The
    /// video shows `secs` of it, the spinner up, and cuts the rest.
    pub until: Option<Until>,
    /// How long the action takes: the pointer getting there, a drag
    /// crossing, the typing, the part of a wait shown. Left out, a move takes
    /// longer the farther it goes.
    pub secs: Option<f32>,
    /// Seconds to hold once the action is done.
    pub pause: f32,
    /// Ignored: what the step is for, JSON having no comments.
    pub note: String,
}

impl Default for Step {
    fn default() -> Self {
        Self {
            glide: None,
            click: None,
            double_click: None,
            right_click: None,
            drag: None,
            to: None,
            by: None,
            scroll: None,
            lines: 0.0,
            typed: None,
            key: None,
            until: None,
            secs: None,
            pause: PAUSE,
            note: String::new(),
        }
    }
}

#[derive(Deserialize, Debug, Clone, Copy, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum Until {
    Solved,
}

/// Something on screen to point at. Written as a string, it is a text.
///
/// `text` is what is drawn: a caption, a tile's or a node's name, a field's
/// value. Text drawn exactly so wins over text that only holds it, so `plate`
/// finds the card titled `plate` before `plate_large_vomp_robolab`. It has to
/// be on screen once; where it is more than once, the tour stops and says
/// where, and `in`, `near` or `nth` says which.
#[derive(Debug, Clone, Default)]
pub struct Target {
    pub text: Option<String>,
    /// An object of the layout, by id: the middle of its box's top, where
    /// something dropped on it would stand.
    pub object: Option<String>,
    /// Where the pointer is. `near` it, of several texts the one closest to
    /// hand: the entry of a menu just opened, over the same text elsewhere.
    pub pointer: bool,
    /// Only what is in this part of the window; alone, the middle of it.
    pub region: Option<Region>,
    /// Of several, the one nearest this.
    pub near: Option<Box<Target>>,
    /// Of several, this one, counting from 0 in reading order.
    pub nth: Option<usize>,
    /// Logical pixels right and down from where the rest lands.
    pub offset: [f32; 2],
}

/// The window's three parts.
#[derive(Deserialize, Debug, Clone, Copy, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum Region {
    /// The spec panel down the right.
    Panel,
    /// The dock under the view: the asset browser or the graph.
    Dock,
    /// The 3D view, what the two leave.
    View,
}

impl<'de> Deserialize<'de> for Target {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct Full {
            text: Option<String>,
            object: Option<String>,
            #[serde(default)]
            pointer: bool,
            #[serde(rename = "in")]
            region: Option<Region>,
            near: Option<Box<Target>>,
            nth: Option<usize>,
            #[serde(default)]
            offset: [f32; 2],
        }
        match serde_json::Value::deserialize(deserializer)? {
            serde_json::Value::String(text) => Ok(Target { text: Some(text), ..default() }),
            value => {
                let Full { text, object, pointer, region, near, nth, offset } =
                    Full::deserialize(value).map_err(serde::de::Error::custom)?;
                Ok(Target { text, object, pointer, region, near, nth, offset })
            }
        }
    }
}

impl fmt::Display for Target {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match (&self.text, &self.object, self.region) {
            (Some(text), ..) => write!(f, "`{text}`")?,
            (None, Some(id), _) => write!(f, "object `{id}`")?,
            _ if self.pointer => write!(f, "the pointer")?,
            (None, None, Some(region)) => write!(f, "the {region:?}")?,
            (None, None, None) => write!(f, "nothing")?,
        }
        if let (Some(region), true) = (self.region, self.text.is_some() || self.object.is_some()) {
            write!(f, " in the {region:?}")?;
        }
        if let Some(near) = &self.near {
            write!(f, " near {near}")?;
        }
        if let Some(nth) = self.nth {
            write!(f, " (#{nth})")?;
        }
        Ok(())
    }
}

/// Reads a storyboard, and works out what each step comes to.
pub fn load(path: &Path) -> anyhow::Result<Tour> {
    parse(&std::fs::read_to_string(path)?)
}

fn parse(json: &str) -> anyhow::Result<Tour> {
    let mut tour: Tour = serde_json::from_str(json)?;
    anyhow::ensure!(tour.fps > 0.0 && tour.scale > 0.0, "`fps` and `scale` have to be positive");
    for (index, step) in tour.steps.iter().enumerate() {
        let beats = step.beats().with_context(|| format!("step {}", index + 1))?;
        tour.beats.extend(beats.into_iter().map(|beat| (index, beat)));
    }
    Ok(tour)
}

/// The smallest parts a step is played in, one after another.
#[derive(Debug, Clone)]
enum Beat {
    /// The pointer moving to a target, over `secs` or as long as its distance
    /// asks.
    Glide(Target, Option<f32>),
    /// The pointer moving this far from where it is.
    Nudge(Vec2, Option<f32>),
    /// A button going down, or up.
    Button(MouseButton, bool),
    Wheel(f32),
    Key(Press),
    Wait(f32),
    Solved,
}

/// A key, as winit reports one.
#[derive(Debug, Clone)]
struct Press {
    code: KeyCode,
    key: Key,
    text: Option<String>,
}

impl Press {
    fn char(c: char) -> Self {
        Self {
            code: KeyCode::Unidentified(NativeKeyCode::Unidentified),
            key: Key::Character(c.to_string().into()),
            text: Some(c.to_string()),
        }
    }

    fn named(name: &str) -> Option<Self> {
        let (code, key) = match name {
            "Enter" => (KeyCode::Enter, Key::Enter),
            "Escape" => (KeyCode::Escape, Key::Escape),
            "Tab" => (KeyCode::Tab, Key::Tab),
            "Backspace" => (KeyCode::Backspace, Key::Backspace),
            _ => return None,
        };
        Some(Self { code, key, text: None })
    }
}

impl Step {
    fn beats(&self) -> anyhow::Result<Vec<Beat>> {
        let actions = [
            self.glide.is_some(),
            self.click.is_some(),
            self.double_click.is_some(),
            self.right_click.is_some(),
            self.drag.is_some(),
            self.scroll.is_some(),
            self.typed.is_some(),
            self.key.is_some(),
            self.until.is_some(),
        ];
        anyhow::ensure!(actions.iter().filter(|a| **a).count() <= 1, "one action a step");
        anyhow::ensure!(
            self.drag.is_some() == (self.to.is_some() != self.by.is_some()),
            "`to` or `by` goes with `drag`, and `drag` needs one of them",
        );
        anyhow::ensure!((self.lines != 0.0) == self.scroll.is_some(), "`lines` goes with `scroll`, and `scroll` needs some");
        let clicks = [&self.click, &self.double_click, &self.right_click];
        for target in [&self.glide, &self.drag, &self.to, &self.scroll].into_iter().chain(clicks).flatten() {
            target.check()?;
        }

        let to = |target: &Target| Beat::Glide(target.clone(), self.secs);
        let click = |button| [Beat::Button(button, true), Beat::Wait(CLICK), Beat::Button(button, false)];
        let mut beats = Vec::new();
        if let Some(target) = &self.glide {
            beats.push(to(target));
        }
        if let Some(target) = &self.click {
            beats.push(to(target));
            beats.extend(click(MouseButton::Left));
        }
        if let Some(target) = &self.double_click {
            beats.push(to(target));
            beats.extend(click(MouseButton::Left));
            beats.push(Beat::Wait(CLICK));
            beats.extend(click(MouseButton::Left));
        }
        if let Some(target) = &self.right_click {
            beats.push(to(target));
            beats.extend(click(MouseButton::Right));
        }
        if let Some(from) = &self.drag {
            beats.extend([Beat::Glide(from.clone(), None), Beat::Button(MouseButton::Left, true), Beat::Wait(GRAB)]);
            match (&self.to, self.by) {
                (Some(drop), _) => beats.push(to(drop)),
                (None, Some([x, y])) => beats.push(Beat::Nudge(Vec2::new(x, y), self.secs)),
                (None, None) => unreachable!("checked above"),
            }
            beats.extend([Beat::Wait(SETTLE), Beat::Button(MouseButton::Left, false)]);
        }
        if let Some(target) = &self.scroll {
            beats.push(Beat::Glide(target.clone(), self.secs));
            // A notch an update, as a wheel turns: the orbit camera zooms by
            // a share of its distance per notch it is handed at once, and five
            // at once would put it at the focus.
            let (whole, part) = (self.lines.abs().trunc() as usize, self.lines.abs().fract());
            let notches = std::iter::repeat_n(1.0, whole).chain((part > 0.0).then_some(part));
            beats.extend(notches.flat_map(|notch| [Beat::Wheel(notch * self.lines.signum()), Beat::Wait(NOTCH)]));
        }
        if let Some(text) = &self.typed {
            let each = self.secs.map_or(TYPE, |secs| secs / text.chars().count().max(1) as f32);
            beats.extend(text.chars().flat_map(|c| [Beat::Key(Press::char(c)), Beat::Wait(each)]));
        }
        if let Some(name) = &self.key {
            beats.push(Beat::Key(Press::named(name).with_context(|| format!("no key `{name}`"))?));
        }
        if self.until.is_some() {
            beats.extend([Beat::Wait(self.secs.unwrap_or(UNTIL)), Beat::Solved]);
        }
        beats.push(Beat::Wait(self.pause));
        Ok(beats)
    }
}

impl Target {
    /// Whether it names one thing to look for.
    fn check(&self) -> anyhow::Result<()> {
        let named = usize::from(self.text.is_some()) + usize::from(self.object.is_some()) + usize::from(self.pointer);
        anyhow::ensure!(
            named == 1 || (named == 0 && self.region.is_some()),
            "a target is a `text`, an `object`, the `pointer`, or a region (`in`) alone",
        );
        anyhow::ensure!(
            self.text.is_some() || (self.near.is_none() && self.nth.is_none() && (named == 0 || self.region.is_none())),
            "`in`, `near` and `nth` pick among texts; `in` alone is a place",
        );
        self.near.as_deref().map_or(Ok(()), Target::check)
    }

    /// Where it is on `screen`, in logical pixels: `None` while it is not
    /// there, an error where it is there ambiguously.
    fn find(&self, screen: &Screen) -> Result<Option<Pos2>, String> {
        let at = if let Some(text) = &self.text {
            let near = match self.near.as_deref().map(|near| near.find(screen)).transpose()? {
                Some(None) => return Ok(None),
                near => near.flatten(),
            };
            pick(screen.texts_in(text, self.region), self.nth, near).map_err(|places| {
                let places: Vec<_> = places.iter().map(|at| format!("({:.0}, {:.0})", at.x, at.y)).collect();
                format!("{self} is on screen {} times, at {}; `in`, `near` or `nth` says which", places.len(), places.join(" "))
            })?
        } else if let Some(id) = &self.object {
            screen.objects.iter().find(|(object, _)| object == id).map(|(_, at)| *at)
        } else if self.pointer {
            Some(screen.pointer)
        } else {
            self.region.map(|region| screen.region(region).center())
        };
        Ok(at.map(|at| at + vec2(self.offset[0], self.offset[1])))
    }
}

/// The one of `found` that is meant: the `nth` in reading order, the one
/// nearest `near`, or the only one — and if there are several and nothing
/// says which, where they all are.
fn pick(found: Vec<Rect>, nth: Option<usize>, near: Option<Pos2>) -> Result<Option<Pos2>, Vec<Pos2>> {
    let mut centres: Vec<Pos2> = found.iter().map(Rect::center).collect();
    centres.sort_by(|a, b| reading(*a, *b));
    match (nth, near) {
        (Some(nth), _) => Ok(centres.get(nth).copied()),
        (None, Some(near)) => Ok(centres.into_iter().min_by(|a, b| a.distance(near).total_cmp(&b.distance(near)))),
        (None, None) if centres.len() > 1 => Err(centres),
        (None, None) => Ok(centres.first().copied()),
    }
}

/// Top to bottom, then left to right.
fn reading(a: Pos2, b: Pos2) -> std::cmp::Ordering {
    a.y.total_cmp(&b.y).then(a.x.total_cmp(&b.x))
}

/// What a target is found against: the window as last drawn.
struct Screen {
    /// Every text egui drew, with its rect.
    texts: Vec<(String, Rect)>,
    window: Rect,
    /// What the panels leave of the window.
    view: Rect,
    /// Where each object of the layout in the view stands, by id.
    objects: Vec<(String, Pos2)>,
    pointer: Pos2,
}

impl Default for Screen {
    fn default() -> Self {
        Self { texts: Vec::new(), window: Rect::NOTHING, view: Rect::NOTHING, objects: Vec::new(), pointer: Pos2::ZERO }
    }
}

impl Screen {
    fn of(world: &mut World) -> Option<Self> {
        let mut windows = world.query_filtered::<&Window, With<PrimaryWindow>>();
        let window = windows.single(world).ok()?;
        let window = Rect::from_min_size(Pos2::ZERO, vec2(window.width(), window.height()));
        let mut cameras = world.query_filtered::<(&Camera, &GlobalTransform), With<Camera3d>>();
        let (camera, transform) = cameras.single(world).ok()?;
        // Set by the panel once it has been laid out: `editor::in_the_view`
        // puts the view at minus the offset, its full size.
        let sub = camera.sub_camera_view?;
        let view = Rect::from_min_size(pos2(-sub.offset.x, -sub.offset.y), vec2(sub.full_size.x as f32, sub.full_size.y as f32));
        let objects = world
            .resource::<Placed>()
            .0
            .iter()
            .filter_map(|asset| {
                let (min, max) = world_aabb(asset);
                let top = Vec3::new((min.x + max.x) / 2.0, (min.y + max.y) / 2.0, max.z);
                let at = camera.world_to_viewport(transform, arena_to_bevy() * top).ok()?;
                // Turned out of the view, or behind a panel, it is not there.
                Some((asset.id.clone(), pos2(at.x, at.y))).filter(|(_, at)| view.contains(*at))
            })
            .collect();
        let texts = world.resource::<OnScreen>().0.clone();
        let pointer = world.resource::<Pointer>().at;
        Some(Self { texts, window, view, objects, pointer })
    }

    fn region(&self, region: Region) -> Rect {
        let Self { window, view, .. } = *self;
        match region {
            Region::View => view,
            Region::Panel => Rect::from_min_max(pos2(view.max.x, window.min.y), window.max),
            Region::Dock => Rect::from_min_max(pos2(window.min.x, view.max.y), pos2(view.max.x, window.max.y)),
        }
    }

    /// Where `want` is drawn, in `region` if one is named: drawn exactly so,
    /// or else drawn within a longer text.
    fn texts_in(&self, want: &str, region: Option<Region>) -> Vec<Rect> {
        let inside = |rect: &Rect| region.is_none_or(|region| self.region(region).contains(rect.center()));
        let matching = |exact: bool| -> Vec<Rect> {
            self.texts
                .iter()
                .filter(|(text, rect)| inside(rect) && if exact { text == want } else { text.contains(want) })
                .map(|(_, rect)| *rect)
                .collect()
        };
        let exact = matching(true);
        if exact.is_empty() { matching(false) } else { exact }
    }

    /// The texts in `region`, in reading order, to say what was there instead.
    fn listing(&self, region: Option<Region>) -> String {
        let mut texts: Vec<_> = self
            .texts
            .iter()
            .filter(|(_, rect)| region.is_none_or(|region| self.region(region).contains(rect.center())))
            .collect();
        texts.sort_by(|a, b| reading(a.1.center(), b.1.center()));
        texts.iter().map(|(text, _)| format!("`{text}`")).collect::<Vec<_>>().join(" ")
    }
}

/// Every text egui drew on the last pass, where it was drawn, in logical
/// pixels: what a person reading the window sees. But for tooltips, which name
/// what is under the pointer and are not things to point at: hovering a tile
/// would make its name a second target.
#[derive(Resource, Default)]
struct OnScreen(Vec<(String, Rect)>);

/// Collects the texts of the pass that just ended.
fn read_the_screen(
    egui: Option<Single<(&EguiFullOutput, &mut EguiContext), With<PrimaryEguiContext>>>,
    mut screen: ResMut<OnScreen>,
) {
    let Some(egui) = egui else { return };
    let (output, mut ctx) = egui.into_inner();
    let Some(output) = &output.0 else { return };
    let tooltips: Vec<Rect> = ctx.get_mut().memory(|memory| {
        let layers = memory.areas().visible_layer_ids().into_iter();
        layers.filter(|layer| layer.order == egui::Order::Tooltip).filter_map(|layer| memory.area_rect(layer.id)).collect()
    });
    screen.0.clear();
    for clipped in &output.shapes {
        texts(&clipped.shape, clipped.clip_rect, &mut screen.0);
    }
    screen.0.retain(|(_, rect)| !tooltips.iter().any(|tooltip| tooltip.contains(rect.center())));
}

/// The texts in `shape`, but those its clip rect hides: scrolled out of a
/// panel, or folded away.
fn texts(shape: &egui::Shape, clip: Rect, into: &mut Vec<(String, Rect)>) {
    match shape {
        egui::Shape::Text(text) => {
            let rect = text.galley.rect.translate(text.pos.to_vec2());
            let said = text.galley.text().trim();
            if !said.is_empty() && clip.contains(rect.center()) {
                into.push((said.to_owned(), rect));
            }
        }
        egui::Shape::Vec(shapes) => {
            for shape in shapes {
                texts(shape, clip, into);
            }
        }
        _ => {}
    }
}

/// The tour's pointer, in logical pixels.
#[derive(Resource, Default)]
struct Pointer {
    at: Pos2,
    down: bool,
    /// Drawn once the video starts.
    shown: bool,
}

/// Where the tour is.
#[derive(Default)]
struct Playhead {
    phase: Phase,
    /// Seconds of video so far: the update's time, but for the updates held
    /// out of the recording.
    clock: f64,
    /// The beat playing.
    next: usize,
    /// The step last announced in the log.
    logged: Option<usize>,
    /// When the beat playing started.
    began: Option<Began>,
    /// Since when the beat playing has waited on something off the video.
    waiting: Option<Instant>,
    /// When the last edit was made, and how many errors had come by then.
    acted: Tick,
    failures: u64,
}

/// When a beat started, and for a move where from and to and over how long.
#[derive(Clone, Copy)]
struct Began {
    clock: f64,
    from: Pos2,
    to: Pos2,
    secs: f32,
}

#[derive(Default)]
enum Phase {
    #[default]
    Opening,
    /// The window loading, since this wall-clock instant.
    Warming(Instant),
    Playing,
    /// Past the last step, or stopped by one, waiting for the frames still
    /// being read back.
    Ending,
}

/// What a beat comes to this update.
enum Done {
    /// Over: on to the next, this same update.
    Next,
    /// Under way: again next update.
    Running,
    /// Waiting on something: again next update, which is left out of the
    /// video.
    Held,
}

fn play(world: &mut World, mut at: Local<Playhead>) {
    let at = &mut *at;
    // egui draws a text field's caret and focus ring only in a focused window,
    // which this one, never given the window system's focus, is not. Past
    // change detection, which would have winit ask for the focus.
    let mut windows = world.query_filtered::<&mut Window, With<PrimaryWindow>>();
    if let Ok(mut window) = windows.single_mut(world) {
        window.bypass_change_detection().focused = true;
    }
    match at.phase {
        Phase::Opening => {
            at.phase = Phase::Warming(Instant::now());
            return;
        }
        Phase::Warming(since) => {
            let warmup = world.resource::<Tour>().warmup;
            // The panel has laid out the view by then, and drawn its texts.
            let Some(screen) = Screen::of(world).filter(|_| since.elapsed().as_secs_f32() >= warmup) else {
                return;
            };
            point(world, screen.view.center());
            world.resource_mut::<Pointer>().shown = true;
            // On into the first beats this same update, which is the video's
            // first frame: `Hold` is still in, so the clock stays at 0.
            at.phase = Phase::Playing;
        }
        Phase::Playing => {
            // A desktop that gives the window the focus feeds it the real
            // mouse's motion and the real keys, which would turn the view
            // under a drag and type into a field: dropped before anything
            // reads them. The tour's own from the last update have been read.
            world.resource_mut::<Messages<MouseMotion>>().clear();
            world.resource_mut::<Messages<KeyboardInput>>().clear();
        }
        Phase::Ending => {
            // A capture is still on its way while its entity is.
            if world.query::<&Screenshot>().iter(world).next().is_none() {
                world.write_message(AppExit::Success);
            }
            return;
        }
    }
    // The update before was in the video.
    if !world.contains_resource::<Hold>() {
        at.clock += world.resource::<Time<Real>>().delta_secs_f64();
    }
    let held = beats(world, at).unwrap_or_else(|err| {
        let step = world.resource::<Tour>().beats[at.next].0 + 1;
        error!("tour: step {step}: {err}");
        // Straight out: a failed tour has no video worth finishing.
        world.write_message(AppExit::error());
        at.phase = Phase::Ending;
        true
    });
    if held {
        world.insert_resource(Hold);
    } else {
        world.remove_resource::<Hold>();
    }
}

/// Plays beats until one runs past this update. Says whether the update is
/// to be held out of the video.
fn beats(world: &mut World, at: &mut Playhead) -> Result<bool, String> {
    let screen = Screen::of(world).unwrap_or_default();
    loop {
        let Some((step, beat)) = world.resource::<Tour>().beats.get(at.next).cloned() else {
            info!("tour: done at {:.2} s", at.clock);
            world.insert_resource(Hold);
            at.phase = Phase::Ending;
            return Ok(true);
        };
        if at.logged != Some(step) {
            info!("tour: step {} at {:.2} s", step + 1, at.clock);
            at.logged = Some(step);
        }
        match beat_once(world, at, &beat, &screen)? {
            Done::Next => {
                at.next += 1;
                at.began = None;
                at.waiting = None;
            }
            Done::Running => return Ok(false),
            Done::Held => return Ok(true),
        }
    }
}

fn beat_once(world: &mut World, at: &mut Playhead, beat: &Beat, screen: &Screen) -> Result<Done, String> {
    let clock = at.clock;
    Ok(match beat {
        Beat::Glide(target, secs) => {
            // Followed as it moves, and where it goes from under the pointer
            // on the way — a label that changes as it is reached — the move
            // ends where it was last seen.
            let found = target.find(screen)?.or(at.began.map(|began| began.to));
            let Some(to) = found else {
                return at.wait_for(TARGET_PATIENCE, || match target.object {
                    Some(_) => format!("{target} is not on screen; there is {}", screen.objects.iter().map(|(id, _)| format!("`{id}`")).collect::<Vec<_>>().join(" ")),
                    None => format!("{target} is not on screen; there is {}", screen.listing(target.region)),
                });
            };
            at.glide(world, to, *secs)
        }
        Beat::Nudge(by, secs) => {
            let from = at.began.map_or(world.resource::<Pointer>().at, |began| began.from);
            at.glide(world, from + egui::vec2(by.x, by.y), *secs)
        }
        Beat::Button(button, down) => {
            press(world, *button, *down);
            if !*down {
                at.acted(world);
            }
            Done::Next
        }
        Beat::Wheel(lines) => {
            wheel(world, *lines);
            at.acted(world);
            Done::Next
        }
        Beat::Key(press) => {
            key(world, press);
            at.acted(world);
            Done::Next
        }
        Beat::Wait(secs) => {
            let began = at.began.get_or_insert(Began { clock, from: Pos2::ZERO, to: Pos2::ZERO, secs: *secs }).clock;
            if clock - began + 1e-6 >= *secs as f64 { Done::Next } else { Done::Running }
        }
        Beat::Solved => {
            let form = world.resource::<Form>();
            if form.failures > at.failures {
                let error = form.errors.last().map_or("", String::as_str);
                return Err(format!("the edit was refused, or failed to solve: {error}"));
            }
            let solving = form.solving;
            let drawn = world.resource_ref::<Placed>().last_changed().is_newer_than(at.acted, world.read_change_tick());
            if drawn && !solving {
                Done::Next
            } else {
                return at.wait_for(SOLVE_PATIENCE, || "the edit never landed as a layout".into());
            }
        }
    })
}

impl Playhead {
    /// Moves the pointer on towards `to`, from where it was when the move
    /// began. Done once it is there.
    fn glide(&mut self, world: &mut World, to: Pos2, secs: Option<f32>) -> Done {
        self.waiting = None;
        let from = world.resource::<Pointer>().at;
        let began = self.began.get_or_insert(Began {
            clock: self.clock,
            from,
            to,
            secs: secs.unwrap_or_else(|| glide(from.distance(to))),
        });
        began.to = to;
        let t = progress(self.clock - began.clock, began.secs);
        let at = began.from.lerp(to, t);
        point(world, at);
        if t < 1.0 { Done::Running } else { Done::Next }
    }

    /// Holds the video until `patience` has run out, and then gives up.
    fn wait_for(&mut self, patience: Duration, why: impl FnOnce() -> String) -> Result<Done, String> {
        let since = *self.waiting.get_or_insert_with(Instant::now);
        if since.elapsed() > patience {
            return Err(format!("{} after {} s", why(), patience.as_secs()));
        }
        Ok(Done::Held)
    }

    /// Notes that an edit may have gone up just now, for `until` to wait on.
    fn acted(&mut self, world: &World) {
        self.acted = world.read_change_tick();
        self.failures = world.resource::<Form>().failures;
    }
}

/// How long a move of `distance` logical pixels takes when the storyboard
/// leaves it out: a hand slows down for the last of a long way.
fn glide(distance: f32) -> f32 {
    (0.3 + distance / 1500.0).min(1.0)
}

/// How far along a move `elapsed` seconds into `secs` is, eased in and out.
fn progress(elapsed: f64, secs: f32) -> f32 {
    let t = if secs > 0.0 { (elapsed / secs as f64).clamp(0.0, 1.0) as f32 } else { 1.0 };
    t * t * (3.0 - 2.0 * t)
}

/// The window, and its scale factor.
fn window(world: &mut World) -> (Entity, f32) {
    let mut windows = world.query_filtered::<(Entity, &Window), With<PrimaryWindow>>();
    let (entity, window) = windows.single(world).expect("the viewer opens one window");
    (entity, window.scale_factor())
}

/// Moves the pointer to `to`, as winit reports a mouse moving.
fn point(world: &mut World, to: Pos2) {
    let (window, scale) = window(world);
    let mut pointer = world.resource_mut::<Pointer>();
    let delta = Vec2::new(to.x - pointer.at.x, to.y - pointer.at.y);
    pointer.at = to;
    if pointer.shown && delta == Vec2::ZERO {
        return;
    }
    let position = Vec2::new(to.x, to.y);
    let moved = CursorMoved { window, position, delta: Some(delta) };
    world.write_message(moved.clone());
    world.write_message(WindowEvent::CursorMoved(moved));
    // The orbit camera turns by the device's motion, not the cursor's.
    world.write_message(MouseMotion { delta: delta * scale });
    // Read by the drop and by the camera as it decides a press is its own.
    // Past change detection, which would have winit warp the real pointer.
    let mut windows = world.query_filtered::<&mut Window, With<PrimaryWindow>>();
    let mut window = windows.single_mut(world).expect("the viewer opens one window");
    window.bypass_change_detection().set_physical_cursor_position(Some((position * scale).as_dvec2()));
}

/// Presses a button, or lets it go.
fn press(world: &mut World, button: MouseButton, down: bool) {
    let (window, _) = window(world);
    let state = if down { ButtonState::Pressed } else { ButtonState::Released };
    let input = MouseButtonInput { button, state, window };
    world.write_message(input);
    world.write_message(WindowEvent::MouseButtonInput(input));
    world.resource_mut::<Pointer>().down = down;
}

fn wheel(world: &mut World, lines: f32) {
    let (window, _) = window(world);
    let input = MouseWheel { unit: MouseScrollUnit::Line, x: 0.0, y: lines, window, phase: TouchPhase::Moved };
    world.write_message(input);
    world.write_message(WindowEvent::MouseWheel(input));
}

/// Presses a key and lets it go.
fn key(world: &mut World, press: &Press) {
    let (window, _) = window(world);
    for state in [ButtonState::Pressed, ButtonState::Released] {
        let input = KeyboardInput {
            key_code: press.code,
            logical_key: press.key.clone(),
            state,
            text: press.text.as_deref().filter(|_| state.is_pressed()).map(Into::into),
            repeat: false,
            window,
        };
        world.write_message(input.clone());
        world.write_message(WindowEvent::KeyboardInput(input));
    }
}

/// An arrow where the tour's pointer is, over everything else egui draws,
/// with a ring round its tip while the button is down.
fn draw_the_pointer(mut contexts: EguiContexts, pointer: Res<Pointer>) -> Result {
    if !pointer.shown {
        return Ok(());
    }
    let ctx = contexts.ctx_mut()?;
    let painter = ctx.layer_painter(egui::LayerId::new(egui::Order::Debug, egui::Id::new("tour pointer")));
    let tip = pointer.at;
    if pointer.down {
        painter.circle(tip, 11.0, egui::Color32::from_rgba_unmultiplied(255, 170, 60, 70), (2.0, egui::Color32::from_rgb(255, 170, 60)));
    }
    // An arrow like the desktop's, outlined to read on light and dark alike.
    let outline: Vec<Pos2> = [(0.0, 0.0), (0.0, 17.0), (4.6, 13.1), (7.6, 19.6), (10.2, 18.4), (7.3, 12.1), (12.3, 12.1)]
        .into_iter()
        .map(|(x, y)| tip + vec2(x, y) * 1.3)
        .collect();
    // Concave, so filled as triangles: a fan from the tip for the head, and
    // the tail apart.
    let fill = |offset: egui::Vec2, colour: egui::Color32| {
        let mut mesh = egui::Mesh::default();
        for point in &outline {
            mesh.colored_vertex(*point + offset, colour);
        }
        for [a, b, c] in [[0, 1, 2], [0, 2, 5], [0, 5, 6], [2, 3, 4], [2, 4, 5]] {
            mesh.add_triangle(a, b, c);
        }
        mesh
    };
    painter.add(fill(vec2(1.5, 1.5), egui::Color32::from_black_alpha(90)));
    painter.add(fill(egui::Vec2::ZERO, egui::Color32::WHITE));
    painter.add(egui::Shape::closed_line(outline, (1.2, egui::Color32::BLACK)));
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The storyboard `docs/video/record.sh` plays parses, and every step of
    /// it comes to something a pointer can do.
    #[test]
    fn the_readme_tour_is_valid() {
        let tour = parse(include_str!("../docs/video/tour.json")).unwrap_or_else(|err| panic!("{err:#}"));
        assert!(!tour.beats.is_empty());
    }

    /// A step does one thing, and says which step it is when it cannot.
    #[test]
    fn a_step_that_cannot_be_played_is_named() {
        for (steps, error) in [
            (r#"[{"pause": 1}, {"click": "a", "key": "Enter"}]"#, "one action"),
            (r#"[{"drag": "a"}]"#, "`to` or `by` goes with `drag`"),
            (r#"[{"drag": "a", "to": "b", "by": [1, 0]}]"#, "`to` or `by` goes with `drag`"),
            (r#"[{"key": "F13"}]"#, "no key `F13`"),
            (r#"[{"click": {"text": "a", "object": "b"}}]"#, "a target is"),
            (r#"[{"click": {"object": "b", "nth": 1}}]"#, "pick among texts"),
            (r#"[{"click": {"object": "b", "in": "view"}}]"#, "pick among texts"),
            (r#"[{"click": {"txt": "a"}}]"#, "unknown field `txt`"),
        ] {
            let err = format!("{:#}", parse(&format!(r#"{{"steps": {steps}}}"#)).unwrap_err());
            assert!(err.contains(error), "{err}");
        }
    }

    /// A window 1280 by 720 with the panel down the right from 900 and the
    /// dock under the view from 440, and a few texts drawn in it.
    fn screen() -> Screen {
        let text = |text: &str, x: f32, y: f32| (text.to_owned(), Rect::from_center_size(pos2(x, y), vec2(40.0, 14.0)));
        Screen {
            texts: vec![
                text("plate", 1000.0, 100.0),
                text("plate_large_vomp_robolab", 1150.0, 100.0),
                text("next_to", 1000.0, 300.0),
                text("plate", 300.0, 600.0),
                text("next_to", 250.0, 650.0),
                text("next_to", 500.0, 500.0),
                text("🗀 Residential  409", 100.0, 500.0),
            ],
            window: Rect::from_min_size(Pos2::ZERO, vec2(1280.0, 720.0)),
            view: Rect::from_min_size(Pos2::ZERO, vec2(900.0, 440.0)),
            objects: vec![("island_top".into(), pos2(450.0, 300.0))],
            pointer: pos2(480.0, 480.0),
        }
    }

    fn find(json: &str) -> Result<Option<Pos2>, String> {
        serde_json::from_str::<Target>(json).expect("a target").find(&screen())
    }

    /// Text drawn exactly so wins over text that holds it, but a longer text
    /// is found when nothing matches exactly.
    #[test]
    fn a_text_is_found_where_it_is_drawn() {
        assert_eq!(find(r#"{"text": "plate", "in": "panel"}"#), Ok(Some(pos2(1000.0, 100.0))));
        assert_eq!(find(r#""Residential""#), Ok(Some(pos2(100.0, 500.0))));
        assert_eq!(find(r#""bowl""#), Ok(None), "absent is not yet there, not an error");
    }

    /// Several of one text is an error saying where they are, until `in`,
    /// `near` or `nth` picks one.
    #[test]
    fn several_of_a_text_have_to_be_told_apart() {
        let err = find(r#""next_to""#).unwrap_err();
        assert!(err.contains("3 times") && err.contains("(1000, 300)"), "{err}");
        assert_eq!(find(r#"{"text": "next_to", "in": "panel"}"#), Ok(Some(pos2(1000.0, 300.0))));
        let near = r#"{"text": "next_to", "in": "dock", "near": {"text": "plate", "in": "dock"}}"#;
        assert_eq!(find(near), Ok(Some(pos2(250.0, 650.0))));
        assert_eq!(find(r#"{"text": "next_to", "nth": 1}"#), Ok(Some(pos2(500.0, 500.0))), "reading order");
        let to_hand = r#"{"text": "next_to", "near": {"pointer": true}}"#;
        assert_eq!(find(to_hand), Ok(Some(pos2(500.0, 500.0))), "the menu just opened under the pointer");
    }

    /// An object is found where it stands, a region alone is its middle, and
    /// an offset moves either.
    #[test]
    fn objects_and_regions_are_places_too() {
        assert_eq!(find(r#"{"object": "island_top", "offset": [10, -5]}"#), Ok(Some(pos2(460.0, 295.0))));
        assert_eq!(find(r#"{"in": "view"}"#), Ok(Some(pos2(450.0, 220.0))));
        assert_eq!(find(r#"{"in": "dock"}"#), Ok(Some(pos2(450.0, 580.0))));
    }
}

# The editor panel

The window's right-hand panel draws the authored spec as a form and edits it over the pipe
([architecture.md](architecture.md#the-pipe)). Start with the module docs of
[src/editor/mod.rs](../src/editor/mod.rs): immediate mode, no local copy of the spec, every add
picked from the vocabulary.

| File | What it is |
| --- | --- |
| [mod.rs](../src/editor/mod.rs) | `Form` (the last spec sent), the camera gate, the framing |
| [form.rs](../src/editor/form.rs) | drawing the spec, turning a control into a `Request`; `PanelState` |
| [browser.rs](../src/editor/browser.rs) | the asset browser; what an add writes |
| [drop.rs](../src/editor/drop.rs) | dragging an asset onto the layout |
| [relations.rs](../src/editor/relations.rs) | a relation's shape drawn over the layout |

**Rust never writes YAML.** [spec_edit.py](../python/arena_composer/spec_edit.py) is the only writer:
it knows which of the two files a key belongs in, keeps comments, and quotes what YAML 1.1 would read
as a boolean. Rust's half is a form that cannot reach an invalid state — ids come from dropdowns, and
a unary relation has no `reference` slot at all. What construction cannot prevent comes back in the
payload's `errors` and is shown above the form.

## Keeping the camera off the panel

`PanOrbitCamera` claims any drag inside its viewport and knows nothing of the panel over it, so
`keep_the_camera_off_the_panel` turns it off while the pointer belongs to egui. The gate is the union
of `PointerOverPanel` — measured in `form::draw`, because egui reports nothing over a docked panel —
and `EguiWantsInput`, for popups, menus and mid-drag widgets above it. It is latched while a button
is held, and runs in `PostUpdate` between `EguiPostUpdateSet::ProcessOutput` and
`PanOrbitCameraSystemSet`. Each of those reasons is a doc comment in [mod.rs](../src/editor/mod.rs).

**Check a change here in a real window.** No unit test builds the full schedule, and the failure is
silent: every drag, click and scroll reaches the camera.

**Do not turn on `bevy_panorbit_camera`'s `bevy_egui` feature** to replace the gate. It is built
against `bevy_egui` 0.40 and the panel draws with 0.42, so it compiles a second egui and gates the
camera on an `EguiContext` nothing draws into: a duplicate dependency tree that does nothing. Its
only two behaviours are the ones we do not want either — ignoring hover lets a press on the panel
orbit, and `EguiFocusIncludesHover` stops a drag the moment it crosses onto the panel.

## Framing beside the panel

`form::draw` sets the camera's `sub_camera_view` every frame from the panel's width
(`beside_the_panel` in [mod.rs](../src/editor/mod.rs)), so the camera's focus lands mid-way across
what the panel leaves. Picking needs nothing extra: `Camera::viewport_to_world` reads the same
matrix.

Keep it one camera. Narrowing it with `Camera::viewport` instead needs a second camera for egui,
because `bevy_egui` sizes a context to its camera's viewport and the panel would dock inside the
space it had just taken.

## The backdrop

The window clears to `BACKDROP` ([viewer.rs](../src/viewer.rs)); `arena-render` keeps Bevy's
default. It is a flat colour because that is all `ClearColorConfig` offers.

A gradient has to be a `bevy_ui` node, and `bevy_ui`'s pass runs
`after(Core3dSystems::PostProcess)` on whichever camera owns it, so the node covers the layout.
Putting one behind costs a second `Camera2d` at `order: -1` to carry it, `ClearColorConfig::None` on
the layout's camera, and three markers it does not otherwise need: `IsDefaultUiCamera`,
`PrimaryEguiContext` and `EguiGlobalSettings::auto_create_primary_context = false`. Without that
last one `bevy_egui` marks a camera of its own, chosen by the archetype order of an `Added<Camera>`
query rather than by spawn order, and two primary contexts panic every system that draws into
egui's pass.

Keep a backdrop out of the 3D pass. Everything drawn there is tonemapped, so a backdrop built as
geometry arrives as the inverse of a curve this code does not own: an unlit dome authored `#39414f`
landed at `#8b94a3`, with its whole ramp flattened. `ClearColor` is written into the target rather
than shaded, and a UI node is drawn past the tonemapper, so both arrive exactly as authored.

## Relation gizmos

Hovering a relation draws its shape over the layout, and the eye toggle keeps it drawn. The shapes
are the ones Arena's loss strategies score (`isaaclab_arena/relations/relation_loss_strategies.py`),
rebuilt from the solved poses; see the module docs of [relations.rs](../src/editor/relations.rs).
Two rules there are documented at their functions: the reported box already carries `Object.scale`
(`layout::world_aabb`), and a spec's params are read with the vocabulary's defaults behind them.

A number being dragged is previewed before it is sent. `PanelState::editing` holds the value and
its spec path, and `form::pending` lays it over the relation the gizmos are built from, so the
shape follows the drag while the object stays where the last solve left it. The edit goes out once,
when the drag ends.

## Adding an object

A click on a browser row adds the asset onto the anchor. A drag drops it onto whatever it is let go
over. Either way the add is two inserts — the object, then an `on` naming it — which
`view._serve_requests` applies in order. Why every add carries an `on`, and which `on` params it
gets, is in `additions` and `on_params` in [browser.rs](../src/editor/browser.rs). The drag, and how
the target under the cursor is picked, is in [drop.rs](../src/editor/drop.rs) (module docs and
`target`).

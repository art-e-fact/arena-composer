# The editor panel

The window's right-hand panel draws the authored spec as a form and edits it over the pipe
([architecture.md](architecture.md#the-pipe)). A dock under the 3D view beside it holds the asset
browser and the spatial graph, a tab each, the way Isaac Sim docks its content browser. Start with
the module docs of [src/editor/mod.rs](../src/editor/mod.rs): immediate mode, no local copy of the
spec, every add picked from the vocabulary.

| File | What it is |
| --- | --- |
| [mod.rs](../src/editor/mod.rs) | `Form` (the last spec sent), the camera gate, the framing |
| [form.rs](../src/editor/form.rs) | the panel: a section per top-level key, what each entry's card holds; `PanelState` |
| [card.rs](../src/editor/card.rs) | the card every entry is drawn as |
| [controls.rs](../src/editor/controls.rs) | the controls inside a card, each turning an edit into a `Request` |
| [dock.rs](../src/editor/dock.rs) | the dock under the view: its fold, and a tab each for the browser and the graph |
| [browser.rs](../src/editor/browser.rs) | the asset browser: its tree, tiles and details; what an add writes |
| [graph.rs](../src/editor/graph.rs) | the spatial graph: its layout, and a pick that opens a card |
| [thumbnails.rs](../src/editor/thumbnails.rs) | asset pictures, for the browser's tiles, the objects' cards and their nodes |
| [drop.rs](../src/editor/drop.rs) | dragging an asset onto the layout |
| [relations.rs](../src/editor/relations.rs) | a relation's shape drawn over the layout |

**Rust never writes YAML.** [spec_edit.py](../python/arena_composer/spec_edit.py) is the only writer:
it knows which of the two files a key belongs in, keeps comments, and quotes what YAML 1.1 would read
as a boolean. Rust's half is a form that cannot reach an invalid state — ids come from dropdowns, and
a unary relation has no `reference` slot at all. What construction cannot prevent comes back in the
payload's `errors`, each shown as a toast in the corner of the 3D view until it is closed (`toasts` in
[form.rs](../src/editor/form.rs)).

## Cards

Every entry of the spec — the robot, the background, each object, set, reference, relation, the task
and each subtask — is a `Card`, after Blender's modifier stack. The header is the fold arrow, an
icon, the title, and at the far edge the eye (relations only) and the × that removes the entry. The
title is the entry's id, always a box to type in; a relation or subtask has no id, so its kind menu
takes the title's place. An object's icon is its asset's picture, where the asset has one.

A section only says what goes in an entry's title and body; the card draws the rest. To give a new
part of the spec a panel, write a section function in [form.rs](../src/editor/form.rs) and add it
to `panel`. Three rules keep it working like the others:

- **A control is keyed by its spec path.** `Step` is hashable, and the path is the widget's id and
  the key of `PanelState::editing` and `typing`. Build paths with `controls::with`; never key a
  control by its label, which two cards can share.
- **A removal waits for the end of the frame.** A card reports its × click, and the section puts
  the path in `PanelState::removing`, which `panel` sends after everything else. A control that held
  the focus sends its edit as the click takes it, naming its entry by position; a removal sent first
  would hand that position, and the edit, to the next entry.
- **A control's timing is tested in real frames.** egui reports a lost focus on two frames running,
  so a control that sends on it can send twice or lose an edit, and only frames show it. The tests
  in [controls.rs](../src/editor/controls.rs) drive a headless `egui::Context` with clicks and keys
  and read what was sent (`send` collects instead of writing under `cfg(test)`).
- **Only glyphs egui bundles.** An icon or label glyph missing from egui's fonts draws as a box.
  Check a new one against `Ubuntu-Light`, `NotoEmoji-Regular` and `emoji-icon-font` in
  `epaint_default_fonts`, or look at a screenshot.

## The dock

The dock runs along the foot of what the spec panel leaves, and is drawn after it, which is what
keeps that panel the window's full height. Its header is the fold, a tab each for the asset browser
and the graph, and the open tab's own controls. Folded, it is its header alone, and a tab clicked
there opens it on that tab ([dock.rs](../src/editor/dock.rs)).

## The asset browser

The browser is the dock's first tab: a folder tree, a grid of tiles and the details of the tile
picked, each a panel docked inside it.

The tree is built each frame from the assets themselves: Arena's under `Arena/` by role, and each
SimReady prop under `SimReady/` by the `category` the index recorded for it. A folder whose assets
a search filters out stays in the tree at nought, so the tree does not reshape as someone types.

A tile is painted rather than laid out: it is one widget sensing clicks, and drags where it is an
object, so it can be picked up anywhere, and a label laid over it would take the pointer. Tiles are
a fixed size because the grid draws only the rows on screen. The browser takes its pictures through
a closure rather than [thumbnails.rs](../src/editor/thumbnails.rs) itself, which is what lets the
tests in [browser.rs](../src/editor/browser.rs) drive the grid in a headless `egui::Context`.

## The spatial graph

The dock's second tab draws the spec as Arena's review GUI draws it with Mermaid: a node per id, an
edge per relation naming a reference, from its subject to it and labelled with its kind, and a
dashed edge from each object reference to its parent. A relation naming no other node — a unary
one, or one naming its own subject — is a chip on its subject's node. Nothing in it knows a relation
kind's name.

It reads down the way the scene stands: what a node is placed against is drawn below it, so the
anchor, the background and whatever nothing names end up along the foot. The layout is Sugiyama's
layered one, dagre's, the one Mermaid draws with, built afresh every frame from the spec; `layout`
in [graph.rs](../src/editor/graph.rs) lists its steps. A label takes a place in a row of its own
between two rows of nodes, so no label is drawn over a node or another label.

A click on a node, chip or label picks that entry, and the panel opens its card. The pick travels
as an `Item` — a node by its id, a relation by its position, as pins keep one — which the panel
turns into the card's spec path at the start of every frame (`PanelState::pick`). A change to the
spec drops the pick: a rename or a removal can hand its id or position to another entry. For `REVEAL` seconds after the click, the card's section
and the card itself are unfolded and `Response::scroll_to_me(None)` is asked for on every frame:
the card grows as it unfolds, so how far to scroll changes until it is open, and `None` scrolls no
further than the card needs. After that the card is only outlined, until the background is
clicked or the spec changes. The pick reaches the panel a frame late, since the dock is drawn after it.

Hovering one shows it in the view as hovering its card does: a relation's shape, or a node's box
(`relations::outline`). Whatever the view is drawing is orange in the graph too, the colour of a
relation's subject there, and a relation hovered or pinned in the panel lights its edge.

The pan and zoom are the graph's own, a `TSTransform` from its points to the pane's. Keep it off
`egui::Scene`: a scene draws text at the graph's size and scales the mesh, so at any zoom but 1:1
thin strokes go first, and an id like `table_top` reads as `table top`. Here text is laid out at the
size it is drawn. The wheel zooms about the pointer, as it zooms the view above; a scene would pan
with it.

## Keeping the camera off the panels

`PanOrbitCamera` claims any drag inside its viewport and knows nothing of the panels over it, so
`keep_the_camera_off_the_panel` turns it off while the pointer belongs to egui. The gate is the union
of `PointerOverPanel` — measured in `form::draw` over the spec panel and the browser's dock both,
because egui reports nothing over a docked panel — and `EguiWantsInput`, for popups, menus and mid-drag widgets above it. It is latched while a button
is held, and runs in `PostUpdate` between `EguiPostUpdateSet::ProcessOutput` and
`PanOrbitCameraSystemSet`. Each of those reasons is a doc comment in [mod.rs](../src/editor/mod.rs).

**Check a change here in a real window.** No unit test builds the full schedule, and the failure is
silent: every drag, click and scroll reaches the camera.

**Do not turn on `bevy_panorbit_camera`'s `bevy_egui` feature** to replace the gate. It is built
against `bevy_egui` 0.40 and the panel draws with 0.42, so it compiles a second egui and gates the
camera on an `EguiContext` nothing draws into: a duplicate dependency tree that does nothing. Its
only two behaviours are the ones we do not want either — ignoring hover lets a press on the panel
orbit, and `EguiFocusIncludesHover` stops a drag the moment it crosses onto the panel.

## Framing in what the panels leave

`form::draw` sets the camera's `sub_camera_view` every frame from the rect the two panels leave
(`in_the_view` in [mod.rs](../src/editor/mod.rs)). The picture is drawn as though the window were
that rect alone: centred in it, with the vertical field of view spanning its height, so a taller
dock zooms the layout out rather than covering it, and a side panel alone moves the picture without
zooming it. Picking needs nothing extra: `Camera::viewport_to_world` reads the same matrix.

Culling does. Bevy builds a camera's `Frustum` from its projection and never from
`sub_camera_view`, so it culls against a picture the window's shape. Wherever the view is wider for
its height than the window, that frustum is narrower than the picture, and what stands at the
view's edges goes undrawn. `cull_to_the_picture` rebuilds the frustum from `Camera::clip_from_view`
between Bevy's `UpdateFrusta` and `CheckVisibility`.

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

A click on a tile picks it. The details' add button, or a double click, adds it onto the node named
in the menu beside the button: the anchor, unless another is picked. A drag drops it onto whatever
it is let go over. Either way the add is two inserts — the object, then an `on` naming it — which
`view._serve_requests` applies in order. Why every add carries an `on`, and which `on` params it
gets, is in `additions` and `on_params` in [browser.rs](../src/editor/browser.rs). The drag, and how
the target under the cursor is picked, is in [drop.rs](../src/editor/drop.rs) (module docs and
`target`).

A background or a robot is set from the details alone, with the `Set` of the registry name its card's
menu sends: its tile senses no drag, and a double click only picks it. Either replaces a whole part
of the scene, which a stray click should not do.

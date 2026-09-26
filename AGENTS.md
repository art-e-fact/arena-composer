# arena_composer

A mixed maturin project: Python under `python/`, Rust under `src/`, one package. **Python owns
everything that needs OpenUSD** — Arena's registries and placement solver, plus reading the assets'
geometry and materials. **Rust owns Bevy** and opens no USD at all. The contract between them is the
MessagePack payload described below; both ends change together. This is meant to become its own
project, so its documentation lives here rather than at the repo root.

Also read
[python/arena_composer/usd/AGENTS.md](python/arena_composer/usd/AGENTS.md) — how assets are read into
the payload, and what currently doesn't render.

Commands below run from the repo root, where `source setup.sh` leaves you.

## The payload

MessagePack rather than JSON, because the geometry is megabytes of float arrays: JSON spends ~760 ms
formatting them as decimal text against ~2 ms for `bin` blobs. Arrays travel as little-endian
`f32`/`u32` and are cast back with one copy through `bytemuck::pod_collect_to_vec` — a `bin` payload
lands at an arbitrary offset, so it cannot be assumed aligned. Everything around the arrays stays an
ordinary map, so unknown keys are ignored and absent ones default.

The same payload is also what a running viewer is fed: `arena-view` writes each new scene to
the child's stdin behind its length in four big-endian bytes, because a pipe has no message
boundaries of its own. `arena_composer.viewer_process.pack_scene` writes them and `src/wire.rs`
reads them; the end of the stream is what closes the window.

Geometry arrives in **Arena's frame** — Z-up, metres — with the transforms already baked into the
vertex positions. That is what lets the Rust side hold no USD conventions: no up-axis, no unit scale,
no xform-op ordering, and no matrix convention to reconcile (USD composes row-vector transforms, glam
column-vector ones). Keep it that way — USD concepts do not belong under `src/`.

Baking costs two things: a mesh repeated inside one asset is emitted twice (assets sharing a USD are
still extracted only once), and nothing can move at runtime. Revisit both when the viewer needs to
animate.

## The two front ends

Both draw the same payload, and neither imports the other.

```bash
arena-view environments/tasks/banana_in_bowl.yaml   # a window, for a person
arena-mcp                                           # the MCP server, over stdio
```

`arena-view` draws in a child process (`arena_composer.viewer_process`): the window's event
loop wants a main thread and never gives it back. The parent stays an asyncio program beside
it, and it holds Arena — so when the spec (or the scene its `external_yaml` includes) changes
it re-resolves and sends the result down the child's stdin, and the viewer swaps the layout
without dropping the window or moving the camera. Closing that pipe closes the window.

## The editor

The window has a panel (`src/editor/`) that draws the authored spec as a form and edits it.
It runs on the same pipe, in both directions:

- **Down stdin** — every payload carries the merged spec, which file owns each top-level key, and
  the `vocabulary`: the registered assets, and every relation and task kind with a control per
  parameter. That last part is *inferred* from Arena's registries in `arena_composer.catalog`,
  never listed by hand, so a relation somebody registers themselves gets a form nobody wrote. All
  of it is ~40 kB against megabytes of geometry, so it rides every scene rather than being asked
  for. The SimReady props are the exception and ride `remote`, below.
- **Up stdout** — one framed request per click (`src/wire.rs`, `spec::Request`). Three edit the
  spec — `{op, path, value}`, applied through `spec_edit.apply` and answered twice, as below — and
  two ask for assets, served by `view._serve_assets` and answered by the prop list. **Nothing else
  may write to the child's stdout**: Bevy logs to stderr, and one stray `println!` would land
  mid-message and desynchronise the reader.

Everything that adds something picks from the vocabulary first: an object comes from the asset
browser at the foot of the panel, a relation from a menu of registered kinds, each entry already
carrying the parameters that kind has no default for. So there is no "add now, fix the invalid
parts after", and no default that was picked because something had to be.

Rust never writes YAML. `spec_edit` is the only writer, because it knows which of the two files a
key belongs in, keeps comments through ruamel, and quotes what YAML 1.1 would read as a boolean.
Rust's job is the other half — a form built so the invalid states are unreachable: ids come from
dropdowns, and a unary relation is drawn without a `reference` slot at all. What construction
cannot prevent comes back in the payload's `errors` and is shown above the form.

### Keeping the camera off the panel

`PanOrbitCamera` claims a drag from the cursor being inside the camera's viewport and knows
nothing about the panel drawn over it, so `keep_the_camera_off_the_panel` clears its `enabled`
while the pointer is busy elsewhere. It runs in `PostUpdate` between
`EguiPostUpdateSet::ProcessOutput` and `PanOrbitCameraSystemSet`: the panel is drawn and
`EguiWantsInput` written in that schedule, so anywhere earlier reads the previous frame's
pointer. The decision is latched for as long as a button is held, which is what lets a drag
cross the edge without changing hands.

**`EguiWantsInput` does not see the panel.** Every field of it is false over the panel, so a gate
built on it alone passes every drag, click and scroll straight through to the camera.
`bevy_egui` builds a root `Ui` and discards it — the Bevy schedule runs in its place — so a panel
has to be docked into a `Ui` of our own on the background layer, the way `bevy_egui`'s own
`side_panel` example does it. `Context::is_pointer_over_egui` answers for that layer out of the
root ui's leftover space, and nothing ever shrinks it, so it reads false everywhere. What egui
does still hit-test is anything on a layer above the background: a combo popup, a menu, a widget
mid-drag. So the gate is the union of two halves — `PointerOverPanel`, measured in `form::draw`
from the rect `Panel::show` returns, and `EguiWantsInput` for everything above it. Check a change
here against a real window: no unit test builds the full schedule, and the failure is silent.

Do not turn on `bevy_panorbit_camera`'s `bevy_egui` feature to replace it. That feature is built
against `bevy_egui` 0.40 and the panel draws with 0.42, so enabling it compiles a second egui and
gates the camera on an `EguiContext` nothing ever draws into — it costs a duplicate dependency
tree and does nothing. It also offers only the two behaviours we do not want: ignore hover, which
lets a press on the panel orbit, or `EguiFocusIncludesHover`, which stops a drag the moment it
crosses onto the panel.

### Framing beside the panel

The camera draws the whole window, and the panel is opaque over its part of it. So that what the
camera looks at lands in the middle of what the panel leaves, `form::draw` sets the camera's
`sub_camera_view` every frame from the panel's width (`editor::beside_the_panel`): the window shows
the right-hand part of a picture one panel wider. The framing follows the panel's edge as it is
dragged, and picking needs nothing of its own — `Camera::viewport_to_world` reads the same matrix.

Keep it one camera. Narrowing it with `Camera::viewport` instead needs a second camera for egui,
because `bevy_egui` sizes a context to its camera's viewport and the panel would dock inside the
space it had just taken.

### The backdrop

The window clears to `BACKDROP` (`src/viewer.rs`), dark enough for a white robot on light tables
to read against and distinct from the grey the panel is drawn in. `arena-render` does not set one
and clears to Bevy's default.

It is a flat colour because that is all `ClearColorConfig` offers. A gradient has to be a
`bevy_ui` node, and `bevy_ui`'s pass is scheduled `after(Core3dSystems::PostProcess)` on whichever
camera owns it — so such a node covers the layout, and putting one behind costs a second
`Camera2d` at `order: -1` to carry it, `ClearColorConfig::None` on the layout's, and three markers
the layout's camera does not otherwise need: `IsDefaultUiCamera`, `PrimaryEguiContext`, and
`EguiGlobalSettings::auto_create_primary_context = false`. Without that last one `bevy_egui` marks
a camera of its own — which of the two follows the archetype order of an `Added<Camera>` query,
not the spawn order — and two primary contexts panic every system that draws into egui's pass.

Keep a backdrop out of the 3D pass. Everything drawn there is tonemapped, so a backdrop built as
geometry arrives as the inverse of a curve this code does not own: an unlit dome authored
`#39414f` landed at `#8b94a3`, with its whole ramp flattened. `ClearColor` is written into the
target rather than shaded, and a UI node is drawn past the tonemapper, so both reach the frame as
authored.

### Two answers per edit

Writing the YAML takes milliseconds and solving the layout it implies takes seconds, so an edit is
answered twice. `payload.encode_spec` goes out as soon as `spec_edit` has written the file — the
editor's half alone, ~40 kB, no resolve, `solving: true` — and the full payload follows when the
solve lands. The viewer keeps drawing the layout it has in between, and a spinner in each top-right
corner says the view is behind the form.

Only `assets`, `remote` and `indexing` may be absent from a message. Every one carries the whole
editor half, so `editor::Form::take` has no half a message to piece together; and
`viewer::apply_updates` drains the newest of *each* kind rather than simply the newest, since both
can land in one frame. `remote` and `indexing` are taken from every message in the frame instead of
the newest, because a crawl finishing during an edit answers on a message of its own.

A layout's copy of the editor half is as old as the solve that produced it, though, so the panel is
answered only by the messages that carry no layout. `view._draw` sends one behind every layout, read
from the file with nothing awaited between the read and the write, and that is also what ends the
spinner. A layout answers the panel in one place, `viewer::setup`, opening it on the first message.
That is why the full payload still carries a spec at all: a packed payload run through
`arena-viewer`, with no Python behind it, has no other message to fill the panel from.

A refused edit is answered the same way with `solving: false`: the file did not change, so what goes
back is that same spec carrying the reason. Re-resolving to say "no" would cost the seconds the edit
was refused in.

`view._solve_when_asked` is a task of its own, so reading edits never waits on a solve, and it clears
its event *before* resolving — edits made during one are answered by the next, and a burst costs one
solve rather than one each. Do not reach for `max_placement_attempts` to make the solve itself
cheaper: Arena solves its candidates in one batch, so asking for fewer costs the same and ranks a
worse layout first.

The panel is egui (`bevy_egui`), drawn each frame from `editor::Form` in the `EguiPrimaryContextPass`
schedule — not `Update`, where there is no context to draw into yet. Immediate mode is
what keeps the panel a pure function of the last payload: there is no widget tree to keep in step
with the spec, so nothing here diffs, rebuilds or restores scroll. The only state it owns is UI
state — the asset search, the browser's uploaded pictures, and the number under the pointer, which
is held against `Form::revision` so a value does not snap back between letting go of it and the spec
carrying it coming back.

`PanOrbitCamera` claims a drag from the cursor position alone and knows nothing about the panel over
it, so `keep_the_camera_off_the_panel` toggles `enabled` from `EguiWantsInput`, latched between
drags: a drag that starts in the panel must not orbit, and one that leaves it must not stop.

### Relation gizmos

Hovering a relation in the panel draws what it asks for over the layout, and the eye toggle on
each keeps it drawn (`src/editor/relations.rs`). The shapes are the ones Arena's loss strategies
score (`isaaclab_arena/relations/relation_loss_strategies.py`) — the `on` footprint inset by its
margin, `next_to`'s target face, `not_next_to`'s keep-out band, the limits around an object's
*origin* — rebuilt in Arena's frame from `layout::Placed`, the assets last drawn, and
`layout::world_aabb`, the reported local box under the solved pose. Two rules there: the reported
box already carries `Object.scale`, so the pose's scale is not applied to it again; and a spec's
params are read with the vocabulary's defaults behind them, the way the panel shows them. The
gizmos are issued in egui's pass from the panel's own hover, on a gizmo group with
`depth_bias = -1`, so a ring under a table or a rectangle on its top is not hidden by the surface.

A number being dragged draws before it is sent. `PanelState::editing` already holds the value
under the pointer, to paint the control with; it carries that value's spec path too, and
`form::pending` lays it over the relation the gizmos are built from. So a parameter's shape
follows the drag, while the object it is about stays where the last solve left it. The edit still
goes out once, when the drag ends.

`ARENA_VIEWER_SHOW_RELATIONS=all` (or `=2,5`, positions in the relations list) starts the viewer
with those drawn — with `ARENA_VIEWER_SCREENSHOT`, the way to look at them without a pointer.

### Adding an object

A click on a browser row adds the asset onto the anchor; a drag drops it onto whatever it is let go
over. Either way the add is two inserts — the object, then an `on` naming it — which
`view._serve_requests` applies in order. Every add carries that `on` because Arena places only
objects that have a relation: one without keeps its default pose, and the solver steers the rest
around it as a fixed obstacle. An `on` to a background or an object reference keeps the kind's
defaults; one to an object asks for `overlap`, since containment inside an object's inset top
rarely fits what is put on it.

The drag is egui's from end to end. The row's button hands egui the asset as its drag-and-drop
payload, and `drop::drop_onto_the_layout`, chained after `form::draw` in egui's pass, is the only
thing that reads it. Keep the button sensing click *and* drag: egui gives a click on a drag-only
widget laid over a button (what `Ui::dnd_drag_source` builds) to neither of them.

While the payload is held off the panel, a mesh ray cast under the cursor picks the target: the
smallest placed box holding the hit, never the embodiment, with a reference winning a tie with its
parent. That is what makes a hit on a counter name the `table_top` reference, which has no mesh of
its own to be hit. With no target the anchor stands in. The surface the `on` will stand the object
on is drawn over the target with the relation gizmos. Let go over the panel, or press Escape, and
nothing is added.

`arena_composer.render` draws a packed scene from named views and writes a PNG each, in a
child process that exits when it is done (`arena_composer.render_process` → `src/render.rs`).
No window and no display server: Bevy runs headless with `synchronous_pipeline_compilation`,
without which the first frames come back empty because the shaders are still compiling. That
is what lets the MCP server show a layout without holding a GPU open between calls.

An asset Bevy cannot load does not stop it drawing — it writes the picture around the gap, so a
failed load arrives as a good-looking frame with an object missing from it. `render.render` treats
any `ERROR` line on the child's stderr as a failed render and raises rather than return that image.

The views are `default` (perspective, framed like the window) plus `top`, `front` and `side`.
The axis views are **orthographic**, because the questions they answer — does this rest on the
table, do these two overlap — have no stable answer under perspective.

## Iterating on the Rust side

Much faster through a packed payload, which skips Arena's import, the solve and the USD reads:

```bash
arena-resolve-layout environments/tasks/banana_in_bowl.yaml --payload scene.msgpack
cd arena_composer && cargo run --bin arena-viewer -- ../scene.msgpack
```

```bash
cd arena_composer && cargo run --bin arena-render -- ../scene.msgpack top=top.png front=front.png
```

`ARENA_VIEWER_SCREENSHOT=shot.png` on either viewer draws for a moment, writes the window to that
path and quits — how to see what the panel actually looks like without a person at the keyboard.
It captures the window and nothing else on the desktop. `ARENA_VIEWER_SEARCH=basket` opens the asset
browser already filtered, and `ARENA_VIEWER_SHOW_RELATIONS=all` opens with the relations drawn,
since a screenshot has nobody to type or hover for it.

`--output` still writes the resolver's JSON dump. That stays the human- and agent-readable
artifact; the payload is the renderer's, and carries a strict subset of its fields.

## The MCP server

`arena_composer.mcp_server` — tools for authoring specs, started by the client configs at the repo
root. The server describes itself through its `INSTRUCTIONS` and tool docstrings; that is what a
client shows the model, so document the tools there and nowhere else.

What matters when changing this code:

- **stdout is the protocol.** Arena and the solver report progress with `print`, and one
  `[placement] Validated ...` line in the stream stops the client parsing the message it lands
  in. Every tool does its work inside `_quiet()`. The SDK's own file-descriptor diversion does
  not cover it — this is tested, not assumed.
- **No session.** Every tool takes a spec path and leaves its result on disk, so an
  `arena-view` window open on the same spec redraws itself as the agent works.
- **Failures are return values**, `{"ok": false, "errors": [...]}`, not exceptions. A spec an
  agent got wrong is the normal case, and the errors are the repair loop.
- `arena_composer.spec_edit` writes with `ruamel.yaml` so comments and key order survive, and
  validates by **reading the text back with PyYAML** — the reader Arena uses. The two
  disagree: PyYAML is YAML 1.1, where a bare `on` is the boolean true, so the `on` relation
  kind has to go out quoted. Validating the in-memory documents would not catch that.

### Remote assets

NVIDIA's SimReady props are not in Arena's registry. A spec names one as
`registry_name: simready_usd_object` with the prop's URL in `params.usd_path`, the form Arena's
own generator writes. Where those URLs come from is `arena_composer.simready`: one listing of the
public bucket, written as `simready-index.json` into the asset cache directory
(`ARENA_COMPOSER_ASSET_CACHE`, default `~/.cache/arena-composer/usd`) beside the USDs it names, so
the MCP server and a viewer read the same file and either may rebuild it. `catalog.search` merges it
with the registry, and the viewer's asset browser lists both.

The props do not ride the vocabulary. They are a megabyte where it is forty kilobytes, and they
change only when the index is rebuilt or pictures are fetched, so they go as `remote` on the message
that changed them and the panel keeps the last list it was sent. Four things send one: the payload
that opens the window, `view._catch_up_on_pictures` when it fetched something, an answer from
`view._serve_assets`, and the poll in `view._reload_on_change` noticing that another process rebuilt
the index.

### Pictures

A picture is published beside a layer, whoever published the layer: `.thumbs/256x256/<layer>.png`,
derived from the layer's URL by `simready.thumbnail_url` with nothing to look up. So a registered
asset gets one the same way a prop does — most of Arena's YCB objects have a sidecar, and the ones
that compose their USD rather than naming a published one have no `usd_path` and no picture to want.
That is why `payload._asset` sends `usd_path` beside `params`: a registered asset's `params` is
empty and has to stay empty, because it is what the entry a pick writes carries.

They are fetched in bulk, never per row. `simready.fetch_thumbnails` takes the layers, skips
whatever is already on disk without asking the server about it, and fetches the rest
`WORKERS`-wide — the cost is a round trip each, not the ~100 kB each. A picture counts as fetched
once it is on disk, so a layer with nothing beside it, or one that is already a local file, is never
counted however often it is offered. Two callers, both in the viewer:

- `view._serve_assets`, after a crawl, for every prop it indexed — up to ~170 MB, which is why only
  the reindex button asks for it. Skipping what is on disk makes an interrupted crawl resumable.
- `view._catch_up_on_pictures`, at startup, for the **registered assets only** (~20 MB the first
  time, a stat each after). They are in no index, so nothing else would fetch theirs. Never widen
  it to `catalog.remote()`: an index built before its pictures — by the MCP server, or by an older
  viewer — would turn every launch into an unrequested 170 MB download.

`simready.build` fetches nothing. It is shared with the MCP server's `reindex_assets`, whose caller
is an agent searching by name that has no use for a picture.

The viewer never asks for a picture and never receives one over the pipe. `payload._asset` reports
the mirrored file's path, or empty, and the panel loads that path off disk through Bevy's asset
server. Empty therefore means *there is none*, not *not yet* — so a blank row is final and the panel
has nothing to wait for. The vocabulary's asset list is rebuilt per message for this reason where
`payload._kinds` stays cached: the assets do not change, but which of their pictures are on disk
does.

### Crawling

`Request::Reindex` is the one thing the browser asks for that is not an edit, and it touches no
spec. The viewer lists the bucket (about a minute), writes the index, then fetches every prop's
picture (about as long again, ~170 MB at a mean of 110 kB each) — which is why it is behind an
explicit button. The names are usable before the pictures are: `view._reload_on_change` notices
the index within a poll, so the panel gets a list while the pictures are still landing and the
crawl's own answer carries them behind it.

A message about assets knows nothing of the last edit or the solve behind it, so it leaves
`errors` and `solving` out, and the panel keeps what it had — the same *absent means no news*
rule as `remote` and `indexing`. `encode_spec` sends them only when given; pass `[]` to say an
edit landed. `Form::take` bumps `revision`, which drops a half-made edit, only when the spec
differs or a refusal arrives — so pictures landing never cost a name being typed, and a refused
value still snaps back.

Whether a crawl is running is `indexing`, which Python answers the way it answers `solving` — the
panel cannot tell from the prop lists, since one arrives whenever pictures land or another process
rebuilds the index. It is set as soon as the request is *read*, not when the crawl starts, because a
crawl queued behind another is still minutes of a live button.

An asset request's answer is retried (`view._answer`). It carries the spec as every message does, so
a spec caught mid-write builds no message — and where an edit's answer can be dropped, because
`_reload_on_change` sends another the moment the file parses, this is the request's only reply and
the request has already been served.

Never offer a prop as an object-set member. Members are built with no arguments, so there is nowhere
to put the `usd_path` a `simready_usd_object` asserts on, and Arena's validator refuses the name
outright.

Nothing downloads a prop until a spec naming it is resolved. That resolve checks the URL and the
rigid-body count first and raises `resolve_layout.SpecError` naming the object, because those are
the two things Arena's validator does not check: it verifies that a registry name exists, not what
the asset behind it takes. The other half of that gap is `catalog.missing_params`, which
`spec_edit` runs on every write so a `simready_usd_object` without its `usd_path` is refused rather
than written.

## Tests

```bash
pytest arena_composer/tests
```

```bash
cd arena_composer && cargo test
```

`cargo test` covers the payload decode, mesh/material building and the frame conventions, and needs
no fixtures. `pytest` covers the spec edits against the real validator, and the render path as far
as it goes without a GPU. Both suites have tests that need a real packed scene and are opt-in:

```bash
arena-resolve-layout environments/tasks/droid_banana_on_plate_maple_table.yaml --payload scene.msgpack
cd arena_composer && ARENA_COMPOSER_PAYLOAD=../scene.msgpack cargo test -- --ignored
ARENA_COMPOSER_PAYLOAD=scene.msgpack pytest arena_composer/tests
```

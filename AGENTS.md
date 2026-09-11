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

The window has a sidebar (`src/sidebar.rs`) that draws the authored spec as a form and edits it.
It runs on the same pipe, in both directions:

- **Down stdin** — every payload carries the merged spec, which file owns each top-level key, and
  the `vocabulary`: the assets, and every relation and task kind with a control per parameter.
  That last part is *inferred* from Arena's registries in `arena_composer.catalog`, never listed
  by hand, so a relation somebody registers themselves gets a form nobody wrote. All of it is
  ~40 kB against megabytes of geometry, so it rides every scene rather than being asked for.
- **Up stdout** — one `{op, path, value}` per edit, framed the way a scene is (`src/wire.rs`).
  Python applies it through `spec_edit.apply` and answers with the next scene. **Nothing else may
  write to the child's stdout**: Bevy logs to stderr, and one stray `println!` would land
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

A refused edit is answered by `errors` alone, with no assets — re-reading every asset's USD to say
"no" would cost seconds. `sidebar::Form::take` treats such a payload as a status message and keeps
the spec it is drawing.

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
path and quits — how to see what the sidebar actually looks like without a person at the keyboard.
It captures the window and nothing else on the desktop.

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

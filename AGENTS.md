# arena_composer

A live editor, headless renderer and MCP server for IsaacLab-Arena environment specs. A mixed
maturin project: **Python owns everything that needs OpenUSD** — Arena's registries, its placement
solver, reading the assets — and **Rust owns Bevy** and opens no USD. They meet in one MessagePack
payload, and both ends change together.

[README.md](README.md) is the user-facing side: what the commands do and how to install a release.
This file is the map for working on the repo itself.

## Commands

```bash
uv pip install --python <Arena's venv>/bin/python -e .   # once; builds the extension (minutes)
cargo test
pytest
```

Run these from this repo with Arena's venv active. The `arena-*` commands run from an Arena project,
which owns the specs.
Python edits take effect at once; a Rust edit takes effect when that install runs again.

To change the Rust side without paying for Arena on every run, pack a payload once and run the dev
`arena-viewer` on it, in its window or headless. That loop, the screenshot variables, the opt-in
tests, the wheel and releasing are in [docs/development.md](docs/development.md).

A change to what the window looks like retakes the README's screenshots:
`docs/screenshots/shoot.sh`, and the [readme-screenshots](.claude/skills/readme-screenshots/SKILL.md)
skill for changing what they show. One to how it is used re-records its video:
`docs/video/record.sh`, and the [readme-video](.claude/skills/readme-video/SKILL.md) skill.

## Where things are

### Python, under `python/arena_composer/`

| Path | What it is |
| --- | --- |
| [resolve_layout.py](python/arena_composer/resolve_layout.py) | a spec → the solved layout, without a SimulationApp; `arena-resolve-layout` |
| [payload.py](python/arena_composer/payload.py) | the layout and its geometry → MessagePack; the contract with Rust |
| [usd/](python/arena_composer/usd/) | reading asset geometry and materials into Arena's frame; has its own [AGENTS.md](python/arena_composer/usd/AGENTS.md) |
| [usd_assets.py](python/arena_composer/usd_assets.py) | mirroring remote USDs into the local asset cache |
| [catalog.py](python/arena_composer/catalog.py) | what a spec can name: assets, and relation and task kinds inferred from Arena's registries |
| [simready.py](python/arena_composer/simready.py) | the SimReady prop index and its pictures |
| [spec_edit.py](python/arena_composer/spec_edit.py) | the only YAML writer: routes each edit to the file that owns the key |
| [view.py](python/arena_composer/view.py) | `arena-view`: watches the spec, re-solves, serves the panel's requests |
| [viewer_process.py](python/arena_composer/viewer_process.py), [render_process.py](python/arena_composer/render_process.py) | the child processes Bevy runs in |
| [render.py](python/arena_composer/render.py) | `arena-render`: specs → PNGs, headless; and the one-shot render `render_views` shares |
| [mcp_server.py](python/arena_composer/mcp_server.py) | `arena-mcp` |
| [cli.py](python/arena_composer/cli.py) | `arena-composer init`: the MCP launcher, client configs and skill links; `arena-composer new`; the `--seed`, `--asset-cache` and `--import` the spec commands share |
| [skills/](python/arena_composer/skills/) | Agent Skills the wheel ships and `init` links into a project; `new` copies `author-arena-spec`'s templates |
| [_core.pyi](python/arena_composer/_core.pyi) | typed signatures for the compiled extension |

### Rust, under `src/`

| Path | What it is |
| --- | --- |
| [lib.rs](src/lib.rs) | crate root; the architecture in one paragraph |
| [scene.rs](src/scene.rs) | the payload's layout half, and the Z-up → Y-up convention |
| [spec.rs](src/spec.rs) | the payload's editor half — spec and vocabulary — and `Request`, what the panel sends back |
| [wire.rs](src/wire.rs) | length-prefixed framing on the pipe, both directions |
| [layout.rs](src/layout.rs) | putting a layout in a world and framing it; shared by the window and the renderer |
| [spawn.rs](src/spawn.rs) | payload geometry → meshes and materials |
| [viewer.rs](src/viewer.rs) | the windowed app: applies incoming messages, draws the status line (`hud_line`) |
| [editor/](src/editor/) | the panel: a card per spec entry, the dock with the asset browser and the spatial graph, drag-and-drop, relation gizmos |
| [render.rs](src/render.rs) | headless renders, a PNG per view |
| [tour.rs](src/tour.rs), [record.rs](src/record.rs) | the README's video: a storyboard played with a pointer of its own, and recorded |
| [python.rs](src/python.rs) | the PyO3 wrapper, behind the `python` feature |
| [main.rs](src/main.rs) | the dev `arena-viewer`: a packed payload off disk, in the window or, given `VIEW=PNG` pairs, headless |

[tests/](tests/) is Python only. Rust tests are inline `#[cfg(test)]` modules beside the code.
[showcase/](showcase/) is a gallery of renders for a person to look over, outside `pytest`'s run:
see [docs/development.md](docs/development.md#the-showcase).

## Docs

- [docs/architecture.md](docs/architecture.md) — the split, the payload, the processes, the pipe, and why an edit is answered twice
- [docs/editor.md](docs/editor.md) — the panel; read before touching the camera gate, framing, backdrop, gizmos or drag-and-drop
- [docs/assets.md](docs/assets.md) — SimReady props, the index, pictures, reindexing
- [docs/mcp.md](docs/mcp.md) — rules for the MCP server, and `arena-composer init`
- [docs/errors.md](docs/errors.md) — how a wrong spec is found, how an edit on one is judged, where
  the lines come out; read before adding a check
- [docs/development.md](docs/development.md) — setup, iterating on a packed payload, tests, the wheel, releasing
- [python/arena_composer/usd/AGENTS.md](python/arena_composer/usd/AGENTS.md) — how assets are read, and what does not render yet

## Rules that bite

- **No USD under `src/`.** Python bakes geometry into Arena's frame — Z-up, metres, transforms in the
  vertex positions — so Rust holds no USD convention at all. Keep it that way.
- **stdout is a protocol, in two places.** The viewer child's stdout carries framed requests up to
  Python: Bevy logs to stderr, and one `println!` desynchronises the reader. `arena-mcp`'s stdout is
  the MCP stream: tools work inside `_quiet()`, because Arena and the solver `print`.
- **Nothing imports Isaac Sim.** Importing `isaacsim` runs Kit's EULA check, which asks on stdout
  and exits where there is no terminal to answer, and nothing here accepts the EULA for the user.
  Arena's registries, its solver and `pxr` need none of it. [tests/conftest.py](tests/conftest.py)
  fails the run when something pulls it in.
- **Rust never writes YAML.** [spec_edit.py](python/arena_composer/spec_edit.py) is the only writer.
  It validates by reading the text back with PyYAML, the YAML 1.1 reader Arena uses, where a bare
  `on` is `true`.
- **MCP tools are documented in their docstrings and `INSTRUCTIONS`, nowhere else.** That is what
  the client shows the model. The [author-arena-spec](python/arena_composer/skills/author-arena-spec/SKILL.md)
  skill is procedure: it names tools and result fields but defines none.
- **Bevy's default features stay off.** Its audio and gamepad support link libasound and libudev,
  which a manylinux wheel may not. Name each feature you need in [Cargo.toml](Cargo.toml).
- **The `python` feature is off by default**, so plain `cargo check`/`clippy`/`test` never compile
  `src/python.rs`: pass `--features python`. `extension-module` is for building the extension
  alone, as maturin and `docs/video/record.sh` do. It unlinks libpython and breaks `cargo run` and
  `cargo test`.
- **CI has no Arena.** A test file that imports it goes in `collect_ignore` in
  [tests/conftest.py](tests/conftest.py), or CI fails.
- **A change people using the package can see goes in [CHANGELOG.md](CHANGELOG.md)**, in the same
  pull request: a line under `## [Unreleased]`, in `Added`, `Changed`, `Fixed` or `Removed`, saying
  what changed for someone running the commands, the panel or the MCP tools, not what changed in
  the code. CI fails a pull request that changes `src/`, `python/`, `Cargo.toml` or
  `pyproject.toml` without one. Where nothing they can see changed — a refactor, a test — label
  the pull request `no changelog` instead (`gh pr edit <n> --add-label "no changelog"`). A released
  version's section is never edited.

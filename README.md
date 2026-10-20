# arena-composer

[![Artefacts CI: showcase](https://app.artefacts.com/api/artefacts/arena-composer/badges/showcase.png)](https://app.artefacts.com/artefacts/arena-composer)

> [!WARNING]
> Under heavy development. Expect the commands, the visual editor and the MCP tools to change
> between releases, and things to break.

A visual editor, headless renderer and MCP server for authoring [IsaacLab-Arena YAML environment specs.](https://isaac-sim.github.io/IsaacLab-Arena/main/pages/concepts/environment/environment_definition.html)

<table>
  <tr>
    <!-- assets.png -->
    <td width="50%"><a href="https://github.com/user-attachments/assets/1cb38053-b2f1-40a1-b0f7-82fd06c4a1ea"><img src="https://github.com/user-attachments/assets/1cb38053-b2f1-40a1-b0f7-82fd06c4a1ea" alt="The visual editor with the asset browser searched"></a></td>
    <!-- graph.png -->
    <td width="50%"><a href="https://github.com/user-attachments/assets/13b3c05f-7442-4d8a-b87c-1fa8495f72b8"><img src="https://github.com/user-attachments/assets/13b3c05f-7442-4d8a-b87c-1fa8495f72b8" alt="The visual editor with the spatial graph and a relation drawn over the layout"></a></td>
  </tr>
  <tr>
    <td>Search Arena's assets and NVIDIA's SimReady props, and drag one onto the layout. Every entry of the spec is a card in the panel.</td>
    <td>The spatial graph: a node per object, an edge per relation. A click opens its card, and the view draws the relation's shape over the layout.</td>
  </tr>
</table>

<!-- arena-composer.mp4 -->
https://github.com/user-attachments/assets/fe77916a-6a83-4348-b8b7-55a05d9676da

## Contents

- [Highlights](#highlights)
- [Commands](#commands)
- [How it works](#how-it-works)
- [Compared with Arena's Agentic Environment Generation](#compared-with-arenas-agentic-environment-generation)
- [Install](#install)
- [Developing](#developing)
  - [The showcase on Artefacts](#the-showcase-on-artefacts)
- [License](#license)

## Highlights

- **Kit-less scene renderer.** Arena's solved layouts, drawn live and headless without Isaac Sim;
  no GPU required.
- **CAD-style visual editor for humans.** Drag assets in, set the relations between them, and watch
  Arena's solver place them.
- **MCP tools for agents.** Search assets, edit the spec, and check the solved layout by its
  numbers or in pictures.

## Commands

- `arena-view <spec>.yaml` opens the visual editor, a window with the layout Arena solves for the
  spec in 3D, beside a form that edits the spec.
- `arena-mcp` is an MCP server whose tools search assets, add and remove objects, and describe and
  render the solved layout.
- `arena-render <spec>.yaml...` draws the solved layout headless, a PNG per `--view`: `default`
  (perspective), or orthographic `top`, `front` and `side`. It needs no display, and a machine
  with no GPU draws through Mesa's lavapipe.
- `arena-resolve-layout <spec>.yaml` prints the solved layout as JSON or text.
- `arena-composer new environments/tasks/<name>.yaml` starts a spec: a task, and the scene it
  includes, holding a pick-and-place that validates, solves and builds.

Each command registers the Isaac Cap assets and tasks Arena's own CLI registers. A project that
registers its own assets or kinds names the modules that do it with `--import MODULE`, or in
`ARENA_COMPOSER_IMPORTS`, separated by `:`.

## How it works

- **The YAML is the source of truth.** The visual editor, the MCP tools and your text editor all
  change the same files.
- **Edits are checked.** The visual editor and the MCP tools write through one place, which:
  - refuses an edit that would not load, leaving the files as they were
  - keeps comments and key order
  - takes an object's relations along when it is renamed or removed
  - edits a task and the scene it includes as one spec
- **The visual editor follows the file.** Every change is re-solved and redrawn, whoever made it.
  One that does not solve leaves the last layout up and says why.
- **Arena places things.** The layout comes from Arena's own placement solver: one of the layouts
  Arena draws at runtime, held still by a seed.
- **Isaac Sim never loads.** A lightweight Rust renderer, [Bevy](https://bevy.org), draws the layout,
  and renders need no display.
- **The vocabulary is Arena's.** Assets, relations and tasks come from Arena's registries, plus
  NVIDIA's SimReady props.

## Compared with Arena's Agentic Environment Generation

Arena's [Agentic Environment Generation](https://isaac-sim.github.io/IsaacLab-Arena/main/pages/concepts/agentic_environment_generation/index.html)
(experimental) works on the same specs, and a spec it generates opens here like any other.

<table>
  <tr><th></th><th>Agentic Environment Generation</th><th>arena-composer</th></tr>
  <tr><th>Starts from</th><td>a natural-language prompt</td><td>a spec you already have</td></tr>
  <tr><th>3D view</th>
    <td>a sim preview in Isaac Sim: builds the environment and records a zero-action rollout</td>
    <td>a lightweight Rust renderer, live as the spec changes, and headless PNG renders; no physics</td></tr>
  <tr><th>Spatial graph</th><td colspan="2" align="center">objects as nodes with their pictures, relations as edges</td></tr>
  <tr><th>Placement</th><td colspan="2" align="center">Arena's relation solver</td></tr>
  <tr><th>Assets</th><td colspan="2" align="center">Arena's registry, plus NVIDIA's SimReady props</td></tr>
  <tr><th>Editing</th><td>the YAML as text, validated as you type</td>
    <td>the visual editor (a form, and drag-and-drop from an asset browser) and MCP tools</td></tr>
  <tr><th>LLM</th><td>calls one itself, with an NVIDIA or OpenAI API key</td>
    <td>none; MCP tools for the agent you use</td></tr>
  <tr><th>The model sees</th><td>the catalogs and validation errors</td>
    <td>also where the solver put things, and renders</td></tr>
  <tr><th>An invalid spec</th><td>retried, then handed back with its errors</td>
    <td>refused; the files stay as they were</td></tr>
  <tr><th>Tasks and relations</th><td>those Arena marks as agent-ready</td><td>every registered one</td></tr>
</table>

## Install

> TODO: publish on PyPi and update the install steps

Into the venv [Arena is installed](https://isaac-sim.github.io/IsaacLab-Arena/main/pages/quickstart/installation.html) in, from a release's wheel. `isaaclab_arena` is not on PyPI, so
the package goes where Arena already is. Linux x86_64, Python 3.12 or newer.

```bash
gh release download v0.3.0 -R art-e-fact/arena-composer -p '*.whl' -D /tmp/arena-composer
uv pip install --python <Arena's venv>/bin/python /tmp/arena-composer/*.whl
```

Then point the project's agent clients at the MCP server. `init` writes `bin/arena-mcp` and the
configs Claude Code, VS Code and Cursor read, keeping any that already exist:

```bash
<Arena's venv>/bin/arena-composer init <project>
```

An MCP client starts the server with almost no environment, so `init --import MODULE` writes
`ARENA_COMPOSER_IMPORTS` into `bin/arena-mcp` itself.

`init` also links the `author-arena-spec` skill into `.agents/skills/` and `.claude/skills/`. The
skill is the procedure an agent follows to write a spec with the MCP tools, through to every
sampled layout passing.

## Developing

See [AGENTS.md](AGENTS.md) and [docs/](docs/).

### The showcase on Artefacts

<!-- showcase.png -->
<a href="https://github.com/user-attachments/assets/00000000-0000-0000-0000-000000000000"><img src="https://github.com/user-attachments/assets/00000000-0000-0000-0000-000000000000" alt="Six showcase environments, drawn by arena-render: an AgiBot, a Galbot and a DROID in Lightwheel kitchens, a KUKA arm over a bench of hand tools, a DROID in a Replicator kitchen, and a GR1's kitchen from above"></a>

The [showcase](docs/development.md#the-showcase) builds about twenty environments and draws each
from four views. [artefacts.yaml](artefacts.yaml) runs it as an
[Artefacts](https://app.artefacts.com/artefacts/arena-composer) job, so every run checks two ways:
an environment that does not build or draw fails its test, and the pictures show at a glance
whether the rest still look right.

From this repo's root, with Arena's venv active and this checkout installed in it:

```bash
artefacts config add artefacts/arena-composer   # once: the project's API key
artefacts run showcase
```

The dashboard gets a test per environment and a PNG per environment and view. More in the
[Artefacts docs](https://docs.artefacts.com/).

## License

Licensed under either of

- Apache License, Version 2.0 ([LICENSE-APACHE](LICENSE-APACHE))
- MIT license ([LICENSE-MIT](LICENSE-MIT))

at your option.

Unless you explicitly state otherwise, any contribution intentionally submitted for
inclusion in this work by you, as defined in the Apache-2.0 license, shall be dual
licensed as above, without any additional terms or conditions.

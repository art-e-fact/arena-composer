# Changelog

What changed for someone using arena-composer: the `arena-*` commands, the editor panel, the MCP
tools. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions are
the wheel's, from `Cargo.toml`, and each is a GitHub Release whose notes are its section here.

## [Unreleased]

### Added

- The editor panel shows the background, object sets, object references and the task — its
  description, its composition and a card per subtask — where it showed only the robot, the
  objects and the relations.
- The asset browser docks under the 3D view, as Isaac Sim's content browser does, and folds down
  to its header. A folder tree files Arena's assets by role and SimReady props by their folders on
  the bucket, each folder with how many assets match the search. The grid shows each asset's
  picture, name and how many the scene already holds, at a size the slider sets.
- A details panel beside the grid describes the asset picked: where it comes from, its registry
  name, physics type, folder, USD path (with a copy button), its tags (a click searches by one)
  and which of the scene's nodes use it.
- Backgrounds and robots are in the asset browser, and the details' button makes one the scene's.
- The details' add button stands the new object on the anchor or on any node picked beside it.
- A graph tab beside the asset browser draws the spec as Arena's review GUI does: a node per id,
  an edge per relation from its subject to what it names, and a chip for each relation that names
  no other node. A click on a node, chip or edge opens its card in the panel and scrolls to it, and
  hovering one shows it in the 3D view. Drag to pan, scroll to zoom, double-click to fit.
- arena-composer is licensed under MIT OR Apache-2.0, at your option, and the wheel says so.
- `check_spec`, an MCP tool that validates a spec as Arena will read it — in milliseconds, with no
  solve and no asset read — and reports what the edit tools would refuse, for after a hand edit.
- `list_kinds`, an MCP tool listing the relation and task kinds a spec can name: the params each
  takes, the ones no form edits — a tuple range, a list of ids — by type, and which Arena marks
  `agent_ready`.
- `describe_scene` and `arena-resolve-layout` report `placement.not_run`: the checks only the
  simulator decides — physics settling, IK reach — so a dry pass reads as necessary, not sufficient.

### Changed

- The MCP tools that write nothing — `search_assets`, `describe_scene`, `render_views`, `check_spec`,
  `list_kinds` — declare themselves read-only, so a client may run them without asking.
- `arena-mcp` answers its handshake about two seconds sooner: Arena's solver is imported by the first
  tool that needs it, not at start.

- Every entry in the editor panel is a card, after Blender's modifier stack: it folds, its header
  names it, and its × removes it. A folded card sums up its entry beside the name, and an object's
  card shows its asset's picture.
- An id is renamed by typing into its card's header, where it took a double-click on the name.
- Renaming the robot's or the background's id renames the relations and task params naming it too,
  as renaming an object does.
- Switching the background's asset drops the params the new one does not take, as switching the
  robot does.
- Adding a second subtask to an `atomic` task makes it `sequential`, and removing back down to one
  makes it `atomic`, so the task stays valid at each step.
- A click on an asset in the browser picks it and shows its details, where it added it. A double
  click adds it, and a drag onto the layout still does.
- The 3D view is framed in what the panels leave, as a window that size would be: a taller asset
  browser zooms the layout out rather than covering it.
- The status line moved to the top-left corner, above the asset browser.
- Errors — a refused edit, a spec that will not build, a failed reindex — pop up in the bottom-right
  corner of the 3D view, where they were lines at the top of the panel. Each stays until its × is
  clicked, and its text can be selected or taken whole with its copy button.
- The panel no longer opens with the environment's name; the window's title carries it.
- `render_views` reports its two steps — resolving the spec, drawing the views — as MCP progress
  notifications, which reach a client that asked for progress on the call. They went out as log
  messages, which the protocol has deprecated: a current client receives none unless it opts in per
  request, and the SDK warned on every one.

### Removed

- The `bin/arena-mcp` that `arena-composer init` writes no longer accepts the Isaac Sim EULA for
  you by exporting `OMNI_KIT_ACCEPT_EULA`: none of the `arena-*` commands start Isaac Sim, so none
  need it. A launcher written before keeps the lines until it is deleted and `init` runs again.

### Fixed

- An edit on a spec that is already broken is refused only for the problems it introduces, and
  reports what was wrong before as `remaining_errors` — on the refusal, and on the edit that landed.
  Adding the first of two objects a task names no longer deadlocks on the second, from the panel or
  from `add_object`.
- Renaming an id carries it into every slot Arena checks — an object reference's `parent_id`, a CLI
  override's target, a task param that lists ids — and into the prim path of a reference inside a
  renamed object, where the spec went on naming the old id and stopped loading.
- Removing an object, from its card, the MCP tool or `apply`, also removes the object references
  inside it and their relations, where it left them dangling and the edit was refused.
- Removing the last entry of a list that holds a comment no longer writes a file that does not
  parse.
- A spec listing a light or a ground plane as an object resolves, in `arena-view`,
  `arena-resolve-layout` and the MCP tools, where it was refused: the asset stays where it is
  configured, out of placement as in the sim, and is drawn as nothing. A relation naming one is
  refused with its index. A spec's own light lights the window as the light Arena injects does.
- A number typed into the editor panel was saved even when escaped from, and saved twice when
  entered.
- The status line drew its separators as boxes.
- `arena-view` no longer exits at startup when api.lightwheel.net cannot be reached, and starts
  about seven seconds sooner when it can; `arena-mcp`'s first `search_assets` gets the same. A spec
  that places one of Arena's Lightwheel assets still needs the network to build.
- 70 of the 100 Lightwheel kitchen backgrounds, `lightwheel_kitchen_l_shaped_coastal` among them,
  build: `arena-view`, `arena-resolve-layout --payload` and `arena-mcp`'s `render_views` failed on
  them with `Singular matrix`.
- Picking a SimReady prop in the asset browser no longer squeezes the grid of tiles down to a
  column: the details beside it widened to fit the prop's folder on one line, and now wrap it.
- A change to the spec on disk that Arena cannot build — a hand edit, or an agent's — shows why as
  a toast in `arena-view`, as a refused edit from the panel does, where the window used to keep the
  last layout without a word. The form follows the file where it still reads.
- An edit from the editor panel is solved once, where `arena-view` solved it again when it noticed
  the write on disk. Solves no longer overlap either, so one from an older version of the file can
  no longer land last and stay drawn.
- A change to the spec made while `arena-view` was still solving its first layout is solved once
  the window is up, where it used to go unnoticed until the next change.
- An edit from the editor panel on a spec that no longer parses — a `task: [` left by a hand edit —
  is refused with the reason as a toast, where it used to end the loop serving the panel: every
  edit and reindex after it in that `arena-view` session went unanswered.
- The MCP edit tools — `add_object`, `remove_object`, `set_embodiment` — answer a spec that does
  not parse with `{"ok": false, "errors": [...]}` naming the file and the line, where the client
  saw only `Error executing tool`. `arena-resolve-layout` and `arena-view`'s toast name the file
  in that error too.
- The MCP tools' other refusals reach the agent again — no spec at the path, a scene passed where
  the task was asked for, an unknown view, a spec that does not read, validate or build, a render
  that logged an error — where the client saw only `Error executing tool`: the `mcp` SDK withholds
  the message of any exception but its own `ToolError`. `describe_scene` answers a spec that does
  not build as a refused edit does, `{"ok": false, "errors": [...]}`; `render_views` fails with the
  same lines.

## [0.2.0] - 2026-09-28

### Added

- Pick the robot in the editor panel, from the registered embodiments, and over MCP with
  `set_embodiment`. The robot's id stays, and the params the new one does not take are dropped.

### Fixed

- A spec naming something other than a robot as its embodiment is refused when edited, rather than
  failing when the scene is built.

## [0.1.0] - 2026-09-27

### Added

- The first release on its own: `arena-view`, `arena-resolve-layout`, `arena-mcp` and
  `arena-composer init`, as a manylinux wheel attached to the GitHub Release.

[Unreleased]: https://github.com/art-e-fact/arena-composer/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/art-e-fact/arena-composer/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/art-e-fact/arena-composer/releases/tag/v0.1.0

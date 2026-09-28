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

### Changed

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

### Fixed

- A number typed into the editor panel was saved even when escaped from, and saved twice when
  entered.
- The status line drew its separators as boxes.
- `arena-view` no longer exits at startup when api.lightwheel.net cannot be reached, and starts
  about seven seconds sooner when it can; `arena-mcp`'s first `search_assets` gets the same. A spec
  that places one of Arena's Lightwheel assets still needs the network to build.

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

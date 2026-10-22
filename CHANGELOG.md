# Changelog

What changed for someone using arena-composer: the `arena-*` commands, the editor panel, the MCP
tools. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions are
the wheel's, from `Cargo.toml`, and each is a GitHub Release whose notes are its section here.

## [Unreleased]

### Added

- A click on an object in `arena-view`'s 3D view picks it, as a click on its node in the graph does:
  its card opens in the panel and its node is outlined in the graph. A click on a counter picks the
  reference to its top, the node things stand on, not the room around it. Hovering outlines what a
  click would pick, a double click brings the camera round to it, and a click on the backdrop drops
  the pick.
- `check_spec`, and every edit, refuse a spec that Arena only refused minutes into a build, once
  Isaac Sim had started: a task param naming a node of another class than the task needs, such as an
  `OpenDoorTask` on a banana, a `PlaceUprightTask` on a bottle that is not `Placeable`, or an
  object reference without the joint param that makes it openable; a `TurnKnobTask` level the knob
  has not got; a `PickAndPlaceTask` into an articulated destination, such as a microwave; a
  `NoTask` beside other subtasks; a lift task whose object has no initial pose; and a tuple param
  short of numbers, or a range given as `[max, min]`. Each line names the entry to fix and, where
  there is one, what would do.
- They also refuse what Arena dropped without a word: a key no field reads, such as
  `openable_joint_name` beside a reference's `prim_path` rather than under its `params`, or
  `parms:`; and a name in `placer_params.enabled_checks` or `required_checks` that no check has,
  which turned that check off.
- `describe_scene` reports the world. Each asset has its box in the world, `bbox_min` and
  `bbox_max`, the one the checks use, and its `yaw_rad`. A `frame` block gives the up axis and the
  robot's forward and left. It gave only each asset's origin and size, so agents guessed a table's
  edges from an origin 0.2 m short of them, and the robot's left from a render.
  `arena-resolve-layout` gives the same boxes as `world_bbox_min` and `world_bbox_max`, beside the
  local ones.
- `describe_scene` and `arena-resolve-layout` list `warnings`: what passes every check and still
  cannot work in the sim. That is an object no relation places, which spawns at the origin; a
  relation on an anchor, which the solver ignores; the robot's origin inside an anchor's box, as on
  `table_oak_robolab`; and a background whose root is a rigid body nothing holds up, as
  `table_oak_robolab`'s is, so nothing on it settles. Each of these passed all eight samples.
- `list_kinds` says the class each task param that names an object needs, as `needs`: `Openable`
  for `OpenDoorTask`'s `openable_object`, `Turnable` for `TurnKnobTask`'s `turnable_object`,
  `EmbodimentBase`, the robot, for `LiftObjectTaskRL`'s `embodiment`, and `Asset`, any entry, for
  most. A list of ids under `other_params` says it too. It said only `node_ref`, so agents learnt
  the class from `check_spec`'s refusal.
- `arena-view`'s panel has a header that pages the view through the layouts a solve draws, one
  per env, five by default (`--samples N`), and says whether each passes the checks or which it
  fails. A page stays turned across edits, so the layout on screen moves only as far as an edit
  moves it. It drew one layout, and another took a restart with another `--seed`.
- The header's ⟳ solves again from a new random seed, and every solve after keeps it, as `--seed`
  would. The seed is shown beside it, to pass back as `--seed`.
- `arena-resolve-layout --samples N` lists every sample's layout under `layouts`, each with its
  verdict and failed checks, where it counted them.

### Changed

- The spinner saying a solve is under way is in the panel's header, where the ⟳ is, rather than
  over the corner of the view.

- The line for a task kind that cannot be a task's only subtask, such as `GoalPoseTask`, no longer
  suggests making it one of several, which sent agents padding it with a `NoTask` that does not
  build either.
- A subtask added or switched to another kind in the panel names, in each param that takes a node,
  the first node of the class its kind needs, such as an Openable for `OpenDoorTask`, where it
  named the first node of any class, which did not build.

### Fixed

- `check_spec` says where a misspelt object or background param shows: it passes the check, and
  `describe_scene`, `arena-render` and the window stop on it seconds in, with
  `TypeError: object.__init__() takes exactly one argument`, which names neither the param nor the
  object. It said the build would find it, once Isaac Sim had started. The `author-arena-spec`
  skill's playbook says what to fix for that line.
- `arena-render`'s and `render_views`' `top`, `front` and `side` views see into a room. They leave
  out the background's pieces that stand between them and everything the spec places, and from
  above the cabinets hung over the objects too. A Replicator kitchen was drawn from all three as
  its grey outside, and a Lightwheel kitchen from the side as its outer wall.
- A background whose USD carries its own lights, such as a Replicator kitchen or the packing table,
  is lit like the others in the window, `arena-render` and `render_views`, where it was flat grey:
  those lights are not drawn, and the light that stands in for Arena's now stands in for them too.
- A check for a relation kind the spec does not use is listed in `placement.not_run`, as 0.3.0
  said it would be, where it read as passed: a spec with no `next_to` reported `next_to: true`.
- A task param typed with a project's own asset or affordance class is a node, as Arena reads it:
  the panel picks it from the spec's ids, and `list_kinds` lists it as a `node_ref`. Only Arena's
  own classes counted, so the panel had no control for it.

## [0.4.0] - 2026-10-20

### Added

- Hovering or pinning a `clutter_on` relation in the panel draws the region its subject is dropped
  from: the middle `spread` of its support's top, and `clearance_m` above that, the floor the
  subject's bottom has to clear.

### Changed

- A node's card in the editor panel wears the colour its node has in the spatial graph: a stripe
  down its left edge, a tint over the card, and its icon in white, as the graph draws it. Its
  summary, after the id, is no longer greyed out. Relations, the task and subtasks are not nodes
  and stay grey.
- arena-composer runs on IsaacLab-Arena's main branch from 2026-10-13 on (#1430; tested at
  dc77564), where Arena rewrote its history without Isaac Cap and moved the placement APIs, and no
  longer on the Arena 0.3.0 ran on. Both call themselves version 0.3.0, so the install cannot tell
  them apart. Arena's 0.3.1 release is not supported: it predates #1430.
- A spec's placement settings are its `placer_params`, as Arena now reads them: `enabled_checks`
  and `required_checks` decide what a solve checks, and `describe_scene` reports the rest in
  `placement.not_run`. A spec still on `placement_validators` is refused with Arena's line saying
  to move them.
- `--seed`, and `seed` on `describe_scene` and `render_views`, default to the spec's
  `placer_params.placement_seed`, and to 42 only where the spec has none. A seed given still wins,
  as Arena's `--placement_seed` does, and `placement.seed` says which one ran.
- `get_prim_tree` gives a relative `prim_path` under an object too, as under the background: Arena
  now expands it under the reference's parent.
- Arena's new kinds are listed and edited like the others: the `clutter_on` relation, the
  `ObjectInTask` task, and the `clutter_on_relation` check.

### Fixed

- The window, `arena-render` and `render_views` draw a robot at the joint positions it starts at,
  which Arena bounds and places it by, rather than at the pose its USD file is authored in; that
  includes an `initial_joint_pose` the spec gives a DROID or a Franka. The KUKA-Allegro stood 1.6 m
  tall where Arena had placed it folded forward, 0.84 m tall and reaching 0.57 m, so props kept
  clear of an arm the picture did not show. The GR1's forearms and the Franka's arm moved too.
- Isaac's Seattle lab table, the `table` background, is drawn where Arena solves it. Its USD
  stands its root 0.55 m along x, which the panel and `arena-render` drew and Arena's box leaves
  out, so props solved onto the table stood beside it. An asset's root now keeps only its rotation,
  as in Arena's box.
- An object reference to a prim with no default-purpose geometry, such as the Seattle table's
  `Collisions/Cube`, a proxy, is refused before the solve with a line naming the prim and the
  purpose its geometry has. Arena bounds only default-purpose geometry, and its solver stopped on
  `Non-finite solver output` for such a reference that a relation names, or on an interval assert
  for one it avoids as an obstacle.
- `get_prim_tree` no longer lists those prims, about half of a Lightwheel kitchen's, its colliders,
  which are guides.

### Removed

- Registering Isaac Cap's assets and tasks: Arena no longer has them, nor its gear insertion and
  syringe sort specs.

## [0.3.0] - 2026-10-07

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
- Every command registers the Isaac Cap assets and tasks Arena's own CLI registers, so its gear
  insertion and syringe sort specs open and describe, and `search_assets` and `list_kinds` find
  them. The `gear_insertion_v2` specs still name assets nothing registers.
- `--import MODULE` on `arena-view` and `arena-resolve-layout`, and `ARENA_COMPOSER_IMPORTS` for all
  three commands, import a project's own modules first, for the assets and kinds they register. A
  module that does not import, for whatever reason, is named, with where it was named.
  `arena-composer init --import MODULE` writes the variable into `bin/arena-mcp`, since an MCP
  client starts it with almost no environment.
- `check_spec`, an MCP tool that validates a spec as Arena will read it — in milliseconds, with no
  solve and no asset read — and reports what the edit tools would refuse, for after a hand edit.
- `list_kinds`, an MCP tool listing the relation and task kinds a spec can name: the params each
  takes, the ones no form edits — a tuple range, a list of ids — by type, and which Arena marks
  `agent_ready`.
- `describe_scene` and `arena-resolve-layout` report `placement.not_run`: the checks a solve did
  not run — the ones only the simulator decides, physics settling and IK reach, and the ones for
  relation kinds the spec does not use — so a dry pass reads as necessary, not sufficient.
- `samples` on `describe_scene`, and `--samples N` on `arena-resolve-layout`, solve that many
  layouts at once, as Arena draws one per reset, and report how many passed, which failed, how often
  each check failed and how many of the layouts differ. The first is still the one described. A
  scene where nothing has a relation reports every sample passed and every check in `not_run`.
- `get_prim_tree`, an MCP tool listing the prims in the background's or an object's USD that an
  object reference can name — a tabletop, a drawer — each with the `prim_path` to write, its
  physics type and, for a jointed body, its joint names. It filters by text and by type, since a
  kitchen has a thousand.
- `arena-composer init` links the `author-arena-spec` skill into `.agents/skills/` and
  `.claude/skills/`. It is the procedure an agent follows to write a spec with the MCP tools, from
  the scaffold to every sampled layout passing, with a playbook for the errors and failed checks it
  meets on the way. The link follows the install, so a new release updates it.
- `arena-composer new PATH` writes a task YAML at `PATH`, and the scene it includes, holding a
  pick-and-place that validates, solves and builds. The scene goes in the `scenes` beside a `tasks`
  directory, else beside the task.
- `arena-render SPEC...` draws specs with no window and no display: it solves each layout and
  writes a PNG per `--view` — `default`, the perspective the window opens on, and the orthographic
  `top`, `front` and `side` — as `<spec>-<view>.png` under `--out-dir`, printing each path. Several
  specs share one import of Arena. One that does not build is skipped with its reasons, the rest are
  drawn, and the run exits 1; a layout that fails placement is drawn all the same, with the checks
  it failed.

### Changed

- A refused edit and `check_spec` name the file to fix when the spec includes a scene — for a key
  neither file has, the one holding its kin, so a missing `task` is the task file's — and an asset
  name, relation or task kind, or id that nothing declares comes with the closest that do. A name
  nothing registered is close to says how to import the module that registers it, and an unquoted
  `on` says to quote it.
- Edits and `check_spec` refuse what Arena only finds when it builds the scene, after the sim has
  started: a required param a subtask, relation, embodiment, background, object or object reference
  lacks — `num_levels` on a turnable reference — and one a subtask, relation or embodiment does not
  take, with the one probably meant; most objects and backgrounds take any param, so a misspelt one
  there still reaches the build. An id in a list param, `pick_up_object_list` among them, that the
  spec does not declare; an object reference with no `prim_path`, or an affordance on one that is
  not an articulation; and an `env_cfg_override` Arena would refuse, a field under `sim` its physics
  backend lacks among it. A spec that already has one is still edited.
- So is an atomic task whose kind takes no description, which Arena passes every atomic task:
  `NoTask`, `LiftObjectTask`, `SortMultiObjectTask`, `GoalPoseTask` and two more build only as one
  of several subtasks, and stopped the sim with a `TypeError` as it started.
- So is a param Arena passes itself — a relation's reference, as `parent` for `on`, or an atomic
  task's `task_description` — which the build would get twice.
- `check_spec` and a refused edit list a duplicate id, a duplicate `cli_override_specs` arg and a
  `task.composition` that does not fit its subtask count beside the spec's other problems, each at
  the entry to fix, where Arena reports them only once every other problem is fixed, and stops at
  the first. A rename to an id in use is refused on a spec with other problems too, where it merged
  the two entries.
- An edit replaces each file it changes whole, where it wrote into it, so `arena-view`, Arena or
  an editor never reads half a file. A write that fails, a full disk, is a refusal.
- `describe_scene` and `render_views` report progress every ten seconds while a solve, a download
  or a render runs, where a long one looked frozen.
- The MCP tools that write nothing — `search_assets`, `describe_scene`, `render_views`, `check_spec`,
  `list_kinds`, `get_prim_tree` — declare themselves read-only, so a client may run them without
  asking.
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
  reports what was wrong before as `remaining_errors` — on the refusal, and on the edit that landed;
  `arena-view` prints it on stderr for an edit from the panel. Adding the first of two objects a
  task names no longer deadlocks on the second, from the panel or from `add_object`. An edit that
  makes a pair read again — a hand edit left a key in both files — lands, with all that is wrong as
  `remaining_errors`; and of two entries wrong the same way, the refusal names the one the edit
  touched.
- Renaming an id carries it into every slot Arena checks — an object reference's `parent_id`, a CLI
  override's target, a task param that lists ids — and into the prim path of a reference inside a
  renamed object, where the spec went on naming the old id and stopped loading.
- Removing an object, from its card, the MCP tool or `apply`, also removes the object references
  inside it and their relations, where it left them dangling and the edit was refused.
- Removing the last entry of a list that holds a comment no longer writes a file that does not
  parse.
- A small float an edit writes, `0.00001`, goes out as `1.0e-05`, which Arena reads as a number.
  It went out as `1e-05`, which Arena reads as a string.
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
- Two MCP tool calls at once no longer write the solver's output into the protocol stream, or
  refuse a valid spec with `Unknown task kind` on a server's first calls: the server runs one tool
  at a time.
- `add_object` and `remove_object` answer a spec a hand edit left misshapen — `embodiment: null`,
  a `relations` that is not a list — with a refusal, where the agent got a bare
  `Error executing tool`.
- Lightwheel kitchens with the marble countertop, such as the ones
  `kitchen_bench_lightwheel_open_microwave` and `kitchen_bench_lightwheel_pick_and_place` use, draw
  their counters, and `arena-render` and `render_views` no longer fail on them: the texture is a
  16-bit greyscale image, which the renderer could not sample.
- Fourier's GR1 humanoid (`gr1`, `gr1_joint`, `gr1_pink`) is shaded smooth, as Isaac Sim shows it,
  where it drew in blocky light and dark patches: a mesh that authors no normals and is a
  subdivision surface, USD's default, gets smooth ones. Its textures are still not drawn.
- The window and the renders frame what is drawn, where a background's light, stray vertices or
  ground plane shrank the scene to a speck in a large dark frame: the replicator kitchens, galileo
  and packing_table among them. A spec that stands objects on packing_table or galileo as a whole
  still has Arena place them hundreds of metres away, and the frame takes them in.

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

[Unreleased]: https://github.com/art-e-fact/arena-composer/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/art-e-fact/arena-composer/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/art-e-fact/arena-composer/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/art-e-fact/arena-composer/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/art-e-fact/arena-composer/releases/tag/v0.1.0

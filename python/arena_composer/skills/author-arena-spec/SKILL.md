---
name: author-arena-spec
description: Writes or edits an IsaacLab-Arena environment spec - a task YAML and the scene YAML it includes - from a description of the scene and the task, through the arena MCP server, and iterates until every sampled layout passes. Use when asked to create, scaffold, extend or repair an environment, a scene or a task spec, or its objects, relations, object references or task. Do not use for running a policy or an Experiment, for watching a spec live in arena-view, or for changing Arena or arena-composer.
---

# Author an Arena spec

Turn a request such as "the robot puts the mug in the bowl" into a spec that Arena builds and
whose layouts pass. The work goes through the `arena` MCP server. Its tools describe themselves,
and their descriptions win where this skill disagrees. This skill gives the order to use them in,
the conventions of a spec, and what to do when a check fails.

## Before starting

- The server's tools (`search_assets`, `check_spec`, `describe_scene` and the rest) have to be
  available. If they are not, the project has not run `arena-composer init`, or the client has not
  been restarted since. Say which, and stop.
- Read one spec the project already has, to learn where its tasks and scenes live and how it names
  ids. Follow that over the defaults below.

## Procedure

1. **Start from the scaffold** for a new spec. `arena-composer new environments/tasks/<name>.yaml`
   writes the task, and the scene it includes, under `environments/scenes/`. They hold a
   pick-and-place that validates, solves and builds. To change a spec that exists, start at step 2.
2. **Choose the robot and the background.** Use `search_assets` with `role` set to `embodiment` or
   `background`. `set_embodiment` changes the robot. Change the background by editing the
   scene's `background` entry.
3. **Find every object with `search_assets`.** Never write a `registry_name` it did not return.
   If nothing fits, `reindex_assets` builds the index of NVIDIA's SimReady props, which the search
   covers from then on. Add each object with `add_object`, giving `on` the id of what it stands on.
4. **Write the task** in the task YAML, using `list_kinds` for the task kinds and their params.
   Point it at the new objects before removing the scaffold's banana and bowl with
   `remove_object`: an edit that leaves the task naming a missing id is refused.
5. **Write the relations** in the scene YAML, using `list_kinds` for the relation kinds and their
   params. For part of the background, such as a tabletop or a drawer, add an `object_references`
   entry, and take its `prim_path` and `object_type` from `get_prim_tree`.
6. **Run `check_spec` after every hand edit**, and fix every line it returns before going on. Each
   line names the file to fix. The playbook below covers the usual ones.
7. **Run `describe_scene` with `samples=8`.** The spec is done when `placement.passed` equals
   `placement.samples` and `warnings` is empty. Otherwise `check_failures` names the failing
   check, and each warning what cannot work; fix it from the playbook and run this step again.
8. **Use `render_views`** only for a question about shape or contact, such as whether something
   rests on its support or whether two objects overlap. Ask only for the views that answer it.

## Conventions

- A spec is two files. The task YAML holds `env_name`, `external_yaml` and `task`. The scene YAML
  it includes holds `embodiment`, `background`, `objects`, `object_sets`, `object_references` and
  `relations`. Give every tool the task YAML.
- Name an id for what the thing is in this scene, such as `mug` or `sink`, in snake_case. Do not
  name it after its `registry_name`.
- Give the scene one anchor: `is_anchor` on the background, or on an object reference to the
  surface things stand on. An anchor takes no other relation: it stays where it is configured.
- Read positions off the world boxes `describe_scene` returns, not off origins: a table's origin
  can sit far from its top, which is its `bbox_max[2]`. Its `frame` says which way the robot faces
  and where its left is. `next_to`'s `side` names a world axis, not the robot's.
- Give every placed object one relation that says where it is: `on` a support, `at_position`, or
  `position_limits`. A light or a ground plane takes no relation.
- Quote the kind `'on'`. Arena reads YAML 1.1, where a bare `on` is the boolean true.
- An `atomic` task has exactly one subtask. Some kinds cannot be a task's only subtask;
  `check_spec` names them. Never pad such a task with a `NoTask` or a copy of a subtask: if the
  goal is that one kind alone, report that Arena cannot build it.
- Write a `prim_path` exactly as `get_prim_tree` returned it, with that node as `parent_id`.
- Leave `env_cfg_override` and `placer_params` alone unless the request is about them.

## Playbook

| Symptom | Cause | Fix |
| --- | --- | --- |
| `relations.N.kind: Input should be a valid string; ... quote it` | A bare `on` | Write `kind: 'on'` |
| `Unknown asset registry_name 'x'; did you mean ...?` | A guessed name | Take one from `search_assets` |
| `Unknown asset ...` with a note about `--import` | The project's own asset, not registered yet | Its module goes in `ARENA_COMPOSER_IMPORTS` in `bin/arena-mcp`; tell the user |
| `... references unknown reference 'x'` or `... unknown node 'x'` | An id the spec does not declare | Add the object, or correct the id |
| `K needs a, b` or `K takes no 'x'; did you mean 'y'?` | Wrong params for a kind | Read the kind's `fields` in `list_kinds` |
| `K takes no task_description ... cannot be a spec's only subtask` | A kind Arena cannot build alone | Pair it only with subtasks the goal has. If the goal is that kind alone, report that it cannot be built |
| `NoTask has no timeout and no success ...` | `NoTask` among a task's subtasks | Remove it |
| `K needs an Openable here, and 'x' is ...` (or a Placeable, a Turnable, a Pressable) | The node is not the kind of thing the task acts on | Name an id or asset the line offers. For part of an asset, give its reference the joint param the line names, from `get_prim_tree` |
| `'x' turns through levels -1 to N` | A level the knob has not got | Choose a level in that range |
| `K filters its contact sensor against 'x', an articulation` | A destination with no rigid body to sense contact with | Add an object reference to the part to place onto, from `get_prim_tree`, and name that |
| `... Arena ignores this key` | A misspelt key, or a param written beside `params` | Correct it, or move it under `params` |
| `object_references.N.prim_path: ... names none` | A reference with no prim | Take a `prim_path` from `get_prim_tree` |
| `object_references.N.prim_path: ... has no box to place by` | A prim whose geometry Arena does not bound: a collision proxy, a guide, or none | Take a `prim_path` from `get_prim_tree`, which lists only prims with a box. If it lists none, relate to the parent itself |
| `relations.N: ... with nothing to place; it takes no relation` | A relation on a light or a ground plane | Remove the relation |
| `TypeError: object.__init__() takes exactly one argument` from `describe_scene` | A param an object or the background does not take. They take any name, so `check_spec` passes it | Correct or remove the `params` written on objects and the background |
| `remaining_errors` on an edit that landed | Problems the spec already had | Fix them one edit at a time |
| `check_failures` counts `on_relation` | The support is too small, or too crowded | Choose a larger support, move objects off it, or lower `edge_margin_m` |
| `check_failures` counts `no_overlap` | Too many objects for the space | Spread them over more supports, or use `next_to` with a `distance_m` |
| `check_failures` counts `next_to` | That side has no room, or another relation contradicts it | Choose another `side`, or remove the contradiction |
| The first sample passes and some others fail | A layout that passes only by luck | Fix the failing check. Do not pin `seed` to hide it |
| `distinct_layouts` is 1 | Every sample found the same layout | Fine for a fixed setup. For a task meant to vary, loosen the constraints that pin it |
| `warnings`: `X: no relation places it ...` | An object with nothing to say where it goes | Give it `on` a support, `at_position` or `position_limits` |
| `warnings`: `relations.N: ... is ignored: ... is an anchor ...` | A relation on an anchor | Remove it. To move the anchor, give it an `initial_pose` in its `params` |
| `warnings`: `<robot>: its origin ... is inside X's box ...` | The robot spawns inside the anchor | Give the robot, or the anchor, an `initial_pose` that clears the other |
| `warnings`: `<background>: ... is a dynamic rigid body ...` | A background that falls in the sim, such as `table_oak_robolab` | Choose another background, and tell the user: the fix is Arena's |

## Report

End by listing the files written, `passed` of `samples`, `distinct_layouts`, and the checks in
`not_run` that only the simulator decides, such as `physics_settled` and `ik_reachable`. The
others in it check relation kinds the spec does not use.

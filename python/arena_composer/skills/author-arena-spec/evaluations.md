# author-arena-spec: evaluations

Worked scenarios to measure the skill against, and the failure modes they exist to catch. Each
one starts in a project where `arena-composer init` has run. It passes when `check_spec` returns
`ok` and `describe_scene` with `samples=8` reports `passed` equal to `samples`.

## Scenarios

1. **A new spec from a sentence.** "The droid picks the mug up and puts it in the bowl, on the
   maple table." Expect `arena-composer new`, then a mug and a bowl found with `search_assets`, a
   `PickAndPlaceTask` naming them, and no banana left behind.
2. **A distractor beside the target.** Extend scenario 1 with "and a tomato soup can next to the
   bowl". Expect a `next_to` relation with a `side`, and eight passing samples.
3. **A repair.** Start from a spec whose scene says `kind: on`, unquoted, and names
   `bowl_ycb`. Expect both fixed from `check_spec`'s lines, with no other changes.
4. **Part of the background.** "Put the plate on the tabletop, not just on the table asset." Expect
   an `object_references` entry whose `prim_path` and `object_type` came from `get_prim_tree`, and
   `is_anchor` moved to it.
5. **A layout that cannot pass.** Eight objects on one small plate. Expect `check_failures` read,
   and the objects spread out, rather than a pinned `seed` or a report of success.

## Failure modes

- **A guessed `registry_name`.** It reads plausibly and is refused. The fix is `search_assets`,
  every time.
- **Removing before re-pointing.** Removing an object the task still names is refused. The task
  changes first.
- **One passing sample read as done.** Arena draws a new layout on every reset, so the criterion
  is all eight.
- **A kind that cannot be the only subtask.** `NoTask`, `LiftObjectTask` and
  `SortMultiObjectTask` validate as an atomic task and stop the sim when it builds them.
  `check_spec` names them.
- **A relative `prim_path` under an object.** It validates and fails at build. `get_prim_tree`
  writes the whole path.

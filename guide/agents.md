# Working with an agent

`arena-mcp` is an MCP server. It gives a coding agent, in Claude Code, VS Code or Cursor, tools to
find assets, edit a spec, and check the layout Arena solves for it, by the numbers and in pictures.
The `author-arena-spec` skill is the procedure the agent follows, from your request to a spec whose
every sampled layout passes.

> [!TIP]
> Open [`arena-view`](editor.md) on the spec while the agent works: the window redraws on every
> change it makes.

## Set it up

A project made from the [template](https://github.com/art-e-fact/artefacts-arena-template) is set up
already: its `source setup.sh` does all of this. Otherwise, with Arena's venv active:

```bash
arena-composer init <project>
```

`init` writes `bin/arena-mcp`, a launcher for the server, and the config each client reads it from:
`.mcp.json` for Claude Code, `.vscode/mcp.json` for VS Code and `.cursor/mcp.json` for Cursor. It
keeps any that already exist, and links the skill into `.agents/skills/` and `.claude/skills/`.
Restart the client, and the `arena` server's tools appear.

If your project registers assets or kinds of its own, name the module that does it with
`init --import MODULE`. A client starts the server with almost no environment, so the launcher has
to carry it. A launcher you already have is kept, and `init` prints the line to add to it.

## Ask for a scene

Ask in plain words, and name what matters to you:

> A kitchen where the robot puts the mug in the sink.

With the skill, the agent:

1. **Starts from a spec:** the scaffold `arena-composer new` writes, or the one you name.
2. **Picks the robot and the background,** and finds every object by searching the assets. It never
   guesses an asset's name.
3. **Writes the task and the relations,** and checks the spec after every edit it makes by hand.
4. **Solves eight sampled layouts,** and keeps fixing until every one passes, with no warnings.
5. **Looks at a picture** only to settle a question about shape or contact, such as whether
   something rests on its support.
6. **Reports** the files it wrote, how many samples passed, and what only the simulator can decide.

You can step in at any point: edit in the panel or the YAML while it works, and tell it what you
changed. Its tools read the files afresh on every call, so it works from your edits.

## The tools

Each tool describes itself to your agent, and that description is the reference. What each one is
for:

<!-- mcp-tools -->
See the docstrings in [mcp_server.py](../python/arena_composer/mcp_server.py).
<!-- /mcp-tools -->

A read-only tool changes nothing of yours, so your client may run it without asking. Checking and
editing take milliseconds. Solving and rendering take seconds, since they place every object again
and read every asset, so the agent makes a run of edits and then checks once. A picture costs it
about a thousand tokens to read.

## When something goes wrong

| Symptom | Likely cause | What to do |
| --- | --- | --- |
| The agent has no `arena` tools | `init` has not run in this project, or the client has not restarted since | Run `arena-composer init`, then restart the client |
| An unknown asset, with a note about `--import` | An asset your project registers, in a module the server has not imported | Run `init --import MODULE`. If you already have `bin/arena-mcp`, add the line it prints |
| A tool answers with nothing but `Error executing tool` | A bug in arena-composer: every failure it expects comes with its reason | Report it, with the spec |
| Some of the eight samples fail | A layout that passes only by luck | Let the agent fix the failing check; pinning the seed only hides it |
| A spec that passes here fails in Isaac Sim | The checks rule out what they can; settling and reach are for the simulator to decide | Run it, as the template's Experiments do |

## Going deeper

- [The skill](../python/arena_composer/skills/author-arena-spec/SKILL.md): the procedure your agent
  follows, the conventions it keeps, and its playbook of errors and their fixes.
- [How it works](how-it-works.md): specs, relations, the solver, and the checks the tools report.
- [The visual editor](editor.md): watching the agent's work, and editing beside it.
- For contributors: [the rules for the MCP server](../docs/mcp.md).

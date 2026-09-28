# The MCP server

[mcp_server.py](../python/arena_composer/mcp_server.py) is a stdio MCP server for authoring specs.
Its module docstring explains which tools exist and why. What each tool does lives in its own
docstring and in `INSTRUCTIONS`, because that is what the client shows the model. **Document the
tools there and nowhere else.**

Rules for changing it:

- **stdout is the protocol.** Arena and the solver report progress with `print`, and one stray line
  stops the client parsing the message it lands in. Every tool does its work inside `_quiet()`. The
  SDK's own file-descriptor diversion does not cover it, and a test checks that. A tool that reports
  progress does it through its `ctx: Context` with `await ctx.info(...)`, as `render_views` does.
- **No session.** Every tool takes a spec path and leaves its result on disk, so an `arena-view`
  window on the same spec redraws itself as the agent works.
- **Failures are return values**, `{"ok": false, "errors": [...]}`, not exceptions. A spec an agent
  got wrong is the normal case, and the errors are its repair loop.
- **Edits go through [spec_edit.py](../python/arena_composer/spec_edit.py).** It writes with
  `ruamel.yaml`, so comments and key order survive, and validates by reading the text back with
  PyYAML — the reader Arena uses. PyYAML is YAML 1.1, where a bare `on` is `true`, so the `on`
  relation kind has to go out quoted. Validating the in-memory document would not catch that.

## Starting it from a client

`arena-composer init [PROJECT]` writes `bin/arena-mcp` and the `.mcp.json`, `.vscode/mcp.json` and
`.cursor/mcp.json` that name it. It keeps any that already exist. Why all three name one launcher,
how each client finds it, and why the launcher prints only to stderr are the comments in
[cli.py](../python/arena_composer/cli.py).

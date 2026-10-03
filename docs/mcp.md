# The MCP server

[mcp_server.py](../python/arena_composer/mcp_server.py) is a stdio MCP server for authoring specs.
Its module docstring explains which tools exist and why. What each tool does lives in its own
docstring and in `INSTRUCTIONS`, because that is what the client shows the model. **Document the
tools there and nowhere else.**

Rules for changing it:

- **stdout is the protocol.** Arena and the solver report progress with `print`, and one stray line
  stops the client parsing the message it lands in. Every tool does its work inside `_quiet()`. The
  SDK's own file-descriptor diversion does not cover it, and a test checks that. A tool that reports
  progress does it through its `ctx: Context`, with `_Progress`, as `describe_scene` and
  `render_views` do. `_Progress.run` adds a heartbeat while the work runs in its thread, and keeps
  one climbing count across the call, as the protocol asks. Not `ctx.info(...)`: the protocol
  deprecated logging (SEP-2577), and a client has to opt in per request to receive any, so a log
  line reaches nobody by default.
- **No session.** Every tool takes a spec path and leaves its result on disk, so an `arena-view`
  window on the same spec redraws itself as the agent works.
- **Failures are return values**, `{"ok": false, "errors": [...]}`, not exceptions. A spec an agent
  got wrong is the normal case, and the errors are its repair loop.
- **Edits go through [spec_edit.py](../python/arena_composer/spec_edit.py).** It writes with
  `ruamel.yaml`, so comments and key order survive, and validates by reading the text back with
  PyYAML — the reader Arena uses. PyYAML is YAML 1.1, where a bare `on` is `true`, so the `on`
  relation kind has to go out quoted. Validating the in-memory document would not catch that.
- **Extensions load on the first tool call, not at startup.** `_quiet()` calls
  `catalog.load_extensions()`, which imports Arena's registries. In `main()` those seconds would
  delay the handshake, and a module that fails to import would end the server where only the
  client's log shows why. On the first call it is a `ToolError` the agent reads.

## Starting it from a client

`arena-composer init [PROJECT]` writes `bin/arena-mcp` and the `.mcp.json`, `.vscode/mcp.json` and
`.cursor/mcp.json` that name it. It keeps any that already exist. Why all three name one launcher,
how each client finds it, and why the launcher prints only to stderr are the comments in
[cli.py](../python/arena_composer/cli.py). `--import MODULE` writes `ARENA_COMPOSER_IMPORTS` into
the launcher, the only place a client's stripped environment can get it from. A kept launcher is
not rewritten, so `init` prints the line to add instead.

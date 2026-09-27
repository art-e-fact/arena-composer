"""A live editor, headless renderer and MCP server for IsaacLab-Arena environment specs.

`resolve_layout` turns an environment spec into a plain scene description; the
compiled `_core` extension draws one in a Bevy window (`arena-view`). Neither
is imported from here — `resolve_layout` needs Arena installed, and plain
`import arena_composer` must keep working without either.
"""

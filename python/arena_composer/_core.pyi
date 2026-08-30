"""Type stubs for the compiled viewer (see `src/python.rs`)."""

__version__: str

def run_viewer(scene_json: str) -> None:
    """Open a window on a resolved layout, blocking until it is closed.

    `scene_json` is `resolve_layout()`'s return value, serialized. Must be
    called from the main thread.
    """

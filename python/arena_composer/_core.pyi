"""Type stubs for the compiled viewer (see `src/python.rs`)."""

__version__: str

def run_viewer_stdin() -> None:
    """Draw the packed scenes arriving on stdin, each behind its length in four
    big-endian bytes, blocking until the window is closed.

    The first scene opens the window and the end of the stream closes it. Must be
    called from the main thread.
    """

def render_scene(payload: bytes, shots: list[tuple[str, str]], width: int, height: int) -> None:
    """Draw one packed scene from each named view, writing a PNG per `shots` entry.

    `shots` pairs a view (`default`, `top`, `front` or `side`) with the path to
    write. Needs no display server. Run it in a process of its own.
    """

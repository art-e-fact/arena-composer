"""The MCP server's contract with its transport.

On stdio, stdout is the protocol. Arena and the placement solver report
progress with `print`, so anything a tool does has to keep that off stdout —
one stray line and the client cannot parse the message it lands in.
"""

from __future__ import annotations

import sys

from arena_composer import mcp_server


def test_arena_chatter_never_reaches_stdout(capsys, monkeypatch):
    """A resolve prints `[placement] Validated ...`; the tool has to swallow it."""

    def noisy(path, seed):
        print("[placement] Validated 10 candidate layout(s)")
        return {"env_name": "t"}

    monkeypatch.setattr("arena_composer.resolve_layout.resolve_layout_from_yaml", noisy)
    mcp_server._resolve("spec.yaml", 42)

    captured = capsys.readouterr()
    assert captured.out == "", f"reached the protocol stream: {captured.out!r}"
    assert "[placement]" in captured.err, "the progress line was dropped rather than diverted"


def test_the_guard_puts_stdout_back(capsys):
    """It has to be a loan, not a swap: the transport writes after the tool
    returns."""
    before = sys.stdout
    with mcp_server._quiet():
        pass
    assert sys.stdout is before


def test_the_guard_survives_a_failing_tool(capsys):
    before = sys.stdout
    try:
        with mcp_server._quiet():
            raise RuntimeError("a tool that blew up")
    except RuntimeError:
        pass
    assert sys.stdout is before

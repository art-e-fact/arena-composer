"""The MCP server's contract with its transport, and with the model.

On stdio, stdout is the protocol. Arena and the placement solver report
progress with `print`, so anything a tool does has to keep that off stdout —
one stray line and the client cannot parse the message it lands in.

A failure a tool saw coming has to reach the model with its reason. The SDK
passes a `ToolError`'s message through and withholds every other exception's
behind a bare `Error executing tool`, so a refusal is a result or a `ToolError`,
never a plain exception.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

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


def _call(name: str, **arguments):
    """One tool call as a client makes it. What `call_tool` raises is what the
    server hands the model as the result's text, `str(exc)` — so the reason
    has to be in it, and it has to be a `ToolError` the tool raised, not the
    `UnexpectedToolError` the SDK wraps a crash in, whose text is the bare
    `Error executing tool`."""
    return asyncio.run(mcp_server.mcp.call_tool(name, arguments))


def _refusal(name: str, **arguments) -> str:
    with pytest.raises(ToolError) as refused:
        _call(name, **arguments)
    assert not isinstance(refused.value, UnexpectedToolError), f"the reason was withheld: {refused.value}"
    return str(refused.value)


def test_a_wrong_file_is_refused_with_the_reason(tmp_path: Path):
    """These refusals exist for the agent to read — a scene's ends with the
    regex that finds its task — and the SDK withholds any message but a
    `ToolError`'s."""
    scene = tmp_path / "scene.yaml"
    scene.write_text("objects: []\n")

    assert "is a scene" in _refusal("remove_object", spec=str(scene), id="banana")
    assert "no spec at" in _refusal("remove_object", spec=str(tmp_path / "nope.yaml"), id="banana")


def test_a_spec_that_does_not_read_is_described_as_a_result_and_refused_as_a_render(tmp_path: Path):
    """`describe_scene` answers as a refused edit does, with the reason naming
    the file; `render_views` has pictures or a reason, never both, so it fails
    with the same lines — as it does on a view it does not know."""
    spec = tmp_path / "task.yaml"
    spec.write_text("task: [\n")

    described = json.loads(_call("describe_scene", spec=str(spec)).content[0].text)
    assert described["ok"] is False
    assert "ParserError" in described["errors"][0] and "task.yaml" in described["errors"][0], described

    assert "sideways" in _refusal("render_views", spec=str(spec), views=["sideways"])
    # Called as a function: through `call_tool` its progress reports want a
    # request to send them on. The `ToolError` is what the SDK passes through.
    with pytest.raises(ToolError) as refused:
        asyncio.run(mcp_server.render_views(spec=str(spec)))
    assert "ParserError" in str(refused.value) and "task.yaml" in str(refused.value), str(refused.value)

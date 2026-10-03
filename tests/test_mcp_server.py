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
import warnings
from pathlib import Path

import pytest
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.shared.exceptions import MCPDeprecationWarning

from arena_composer import mcp_server, spec_edit


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
    with the same lines — as it does on a view it does not know — having
    reported the step it was on as progress."""
    spec = tmp_path / "task.yaml"
    spec.write_text("task: [\n")

    described = json.loads(_call("describe_scene", spec=str(spec)).content[0].text)
    assert described["ok"] is False
    assert "ParserError" in described["errors"][0] and "task.yaml" in described["errors"][0], described

    assert "sideways" in _refusal("render_views", spec=str(spec), views=["sideways"])

    # Through a client, which gives the tool a request to report progress on;
    # in-process, so the callback is called directly. As a log message the
    # report would reach nobody: logging is deprecated, a client opts in per
    # request, and the SDK warns on the call — an error here.
    progress: list[str | None] = []

    async def noted(_: float, __: float | None, message: str | None) -> None:
        progress.append(message)

    async def rendered():
        async with Client(mcp_server.mcp) as client:
            return await client.call_tool("render_views", {"spec": str(spec)}, progress_callback=noted)

    with warnings.catch_warnings():
        warnings.simplefilter("error", MCPDeprecationWarning)
        result = asyncio.run(rendered())
    assert result.is_error and "ParserError" in result.content[0].text and "task.yaml" in result.content[0].text, result
    assert progress == [f"resolving task.yaml (seed {mcp_server.DEFAULT_SEED})"], progress


def test_the_cheap_tools_stay_off_stdout_too(capsys, monkeypatch, tmp_path: Path):
    """`check_spec` and `list_kinds` import and populate Arena's registries,
    which print while they do."""

    def noisy_check(path):
        print("[registry] populated")
        return spec_edit.Result(ok=True)

    empty = {"relations": [], "tasks": [], "compositions": []}

    def noisy_kinds():
        print("[registry] populated")
        return empty

    monkeypatch.setattr(spec_edit, "check", noisy_check)
    monkeypatch.setattr("arena_composer.payload.kinds", noisy_kinds)
    spec = tmp_path / "task.yaml"
    spec.write_text("task: {}\n")

    assert json.loads(_call("check_spec", spec=str(spec)).content[0].text)["ok"] is True
    assert json.loads(_call("list_kinds").content[0].text) == empty
    captured = capsys.readouterr()
    assert captured.out == "", f"reached the protocol stream: {captured.out!r}"


def test_check_spec_answers_a_broken_spec_as_a_result(tmp_path: Path):
    spec = tmp_path / "task.yaml"
    spec.write_text("task: [\n")
    checked = json.loads(_call("check_spec", spec=str(spec)).content[0].text)
    assert checked["ok"] is False
    assert "ParserError" in checked["errors"][0] and "task.yaml" in checked["errors"][0], checked


def test_the_tools_that_write_nothing_say_so():
    """A client may run a read-only tool without asking, so the hint has to
    sit on exactly the tools that leave the spec alone — not the edits, and
    not the reindex, which rewrites a shared file."""
    tools = asyncio.run(mcp_server.mcp.list_tools())
    read_only = {tool.name for tool in tools if tool.annotations and tool.annotations.read_only_hint}
    assert read_only == {"search_assets", "describe_scene", "render_views", "check_spec", "list_kinds"}
    assert len(tools) == 9

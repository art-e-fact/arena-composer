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
import math
import sys
import threading
import time
import warnings
from pathlib import Path

import pytest
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.shared.exceptions import MCPDeprecationWarning

from arena_composer import cli, mcp_server, spec_edit


def test_arena_chatter_never_reaches_stdout(capsys, monkeypatch):
    """A resolve prints `[placement] Validated ...`; the tool has to swallow it."""

    def noisy(path, seed, samples):
        print("[placement] Validated 10 candidate layout(s)")
        return {"env_name": "t"}

    monkeypatch.setattr("arena_composer.resolve_layout.resolve_layout_from_yaml", noisy)
    mcp_server._resolve("spec.yaml", 42)

    captured = capsys.readouterr()
    assert captured.out == "", f"reached the protocol stream: {captured.out!r}"

    assert "[placement]" in captured.err, "the progress line was dropped rather than diverted"


def test_a_call_finishing_leaves_one_still_running_quiet():
    """Each call runs on a worker thread. One that ends while another is still
    solving must not hand the other's prints the real stdout."""
    inside, finished, quiet = threading.Event(), threading.Event(), []

    def first():
        with mcp_server._quiet():
            inside.set()
            time.sleep(0.2)
        finished.set()

    def second():
        with mcp_server._quiet():
            finished.wait(5)
            quiet.append(sys.stdout is sys.stderr)

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    threads[0].start()
    inside.wait(5)
    threads[1].start()
    for thread in threads:
        thread.join(10)
    assert quiet == [True]


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
    assert progress == ["resolving task.yaml"], progress


#: A resolve as `describe_scene` reads it. The G1 stands as Arena's loco-manipulation scene stands
#: it, turned to face world -x, and the maple table's origin is 0.2 m short of its near edge.
DUMP = {
    "env_name": "e",
    "spec_path": "task.yaml",
    "placement": {},
    "warnings": [],
    "relations": [],
    "assets": [
        {
            "id": "g1",
            "role": "embodiment",
            "registry_name": "g1_wbc_joint",
            "is_anchor": False,
            "solved": False,
            "position_xyz": [0.8, -1.38, 0.78],
            "yaw_rad": math.pi,
            "bbox_min": [-0.092, -0.186, -0.785],
            "bbox_max": [0.417, 0.186, 0.531],
            "world_bbox_min": [0.383, -1.566, -0.005],
            "world_bbox_max": [0.892, -1.194, 1.311],
        },
        {
            "id": "maple_table_robolab",
            "role": "background",
            "registry_name": "maple_table_robolab",
            "is_anchor": True,
            "solved": True,
            "position_xyz": [0.0, 0.0, 0.0],
            "yaw_rad": 0.0,
            "bbox_min": [0.19857, -0.47786, -0.697],
            "bbox_max": [0.89857, 0.52214, 0.003],
            "world_bbox_min": [0.19857, -0.47786, -0.697],
            "world_bbox_max": [0.89857, 0.52214, 0.003],
        },
    ],
}


def test_describe_scene_reports_the_world_and_which_way_the_robot_faces(tmp_path: Path, monkeypatch):
    """In the trial, agents guessed a table's edges from its origin and the robot's left from a
    render. The G1 turned to face -x has its left at -y, which `next_to` calls `negative_y`."""
    spec = tmp_path / "task.yaml"
    spec.write_text("task: [\n")
    monkeypatch.setattr(mcp_server, "_resolve", lambda path, seed, samples: DUMP)

    described = json.loads(_call("describe_scene", spec=str(spec)).content[0].text)
    assert described["frame"] == {
        "up": [0.0, 0.0, 1.0],
        "robot_forward": [-1.0, 0.0, 0.0],
        "robot_left": [0.0, -1.0, 0.0],
    }
    robot, table = described["assets"]
    assert robot["yaw_rad"] == 3.1416
    assert (robot["bbox_min"], robot["bbox_max"]) == ([0.383, -1.566, -0.005], [0.892, -1.194, 1.311])
    assert table["position_xyz"] == [0.0, 0.0, 0.0] and table["bbox_min"][0] == 0.1986 and table["bbox_max"][2] == 0.003
    assert table["size_xyz"] == [0.7, 1.0, 0.7]
    assert described["warnings"] == []


def test_a_long_solve_says_it_is_still_running(tmp_path: Path, monkeypatch):
    """A first download or a mesh-heavy solve runs for minutes with nothing to
    show. A heartbeat says it is still going, on one count that only climbs,
    as the protocol asks of progress."""
    spec = tmp_path / "task.yaml"
    spec.write_text("task: [\n")
    asked = []

    def slow(path: Path, seed: int, samples: int) -> dict:
        import time

        asked.append(samples)
        time.sleep(0.35)
        return DUMP

    monkeypatch.setattr(mcp_server, "HEARTBEAT", 0.1)
    monkeypatch.setattr(mcp_server, "_resolve", slow)
    progress: list[tuple[float, str | None]] = []

    async def noted(value: float, _: float | None, message: str | None) -> None:
        progress.append((value, message))

    async def described():
        async with Client(mcp_server.mcp) as client:
            return await client.call_tool(
                "describe_scene", {"spec": str(spec), "samples": 4}, progress_callback=noted
            )

    assert not asyncio.run(described()).is_error
    assert asked == [4]
    values, messages = zip(*progress)
    assert list(values) == sorted(set(values)), "progress has to climb"
    doing = "resolving task.yaml"
    assert messages[0] == doing and len(messages) >= 3, messages
    assert all(message.startswith(f"{doing}: ") and message.endswith(" s") for message in messages[1:]), messages


def test_fewer_than_one_sample_is_refused(tmp_path: Path):
    spec = tmp_path / "task.yaml"
    spec.write_text("task: [\n")
    assert "samples must be at least 1, got 0" in _refusal("describe_scene", spec=str(spec), samples=0)


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


def test_an_import_that_fails_is_a_refusal_the_agent_reads(monkeypatch):
    """Loaded on the first tool call, not at startup, so a bad
    ARENA_COMPOSER_IMPORTS reaches the agent rather than a client's log."""

    def failing(modules=()):
        raise ImportError("ARENA_COMPOSER_IMPORTS names my_assets, which does not import: No module named 'my_assets'")

    monkeypatch.setattr(mcp_server.catalog, "load_extensions", failing)
    with pytest.raises(ToolError, match="^ARENA_COMPOSER_IMPORTS names my_assets, which does not import"):
        mcp_server.list_kinds()


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
    assert read_only == {"search_assets", "describe_scene", "get_prim_tree", "render_views", "check_spec", "list_kinds"}
    assert len(tools) == 10


def test_a_central_hit_carries_its_credit(tmp_path: Path, monkeypatch):
    """CC-BY-4.0 asks for credit wherever the prop is passed on, so the model
    is handed it with the hit. `cached` looks in the Hugging Face cache, where
    a Central prop is downloaded to."""
    from arena_composer import catalog, simready

    monkeypatch.setattr("huggingface_hub.constants.HF_HUB_CACHE", str(tmp_path))
    url = (
        "https://huggingface.co/datasets/nvidia/simready-assets/resolve/faaa6cea782d370b0b73408674507ad9f7835484/"
        "paint_supplies_spraygun_b01/simready_usd/sm_paint_supplies_spraygun_b01_01.usd"
    )
    asset = catalog.Asset(
        registry_name="simready_usd_object", role="object", tags=["central", "paint"], object_type="rigid",
        name="paint_supplies_spraygun_b01", usd_path=url, params={"usd_path": url}, category="Central/paint",
        credit=simready.CENTRAL_CREDIT,
    )
    hit = mcp_server._hit(asset)
    assert hit["credit"] == simready.CENTRAL_CREDIT
    assert hit["cached"] is False
    assert "credit" not in mcp_server._hit(catalog.Asset("banana", "object", [], "rigid", "banana")), "none asked"


def test_a_reindex_says_what_it_got_only_part_of(monkeypatch):
    """SimReady Central unlisted, or listed without its rigid-body counts, is
    an index that was written, so it is a result and not an error: the agent
    reads what was kept in its keys."""
    from arena_composer import simready

    note = "unavailable: SimReady Central's search did not answer, so props are offered whatever their count"
    monkeypatch.setattr(simready, "build", lambda: ([], {"central_filter": note}))
    result = asyncio.run(mcp_server.reindex_assets())
    assert (result["indexed"], result["central"], result["central_filter"]) == (0, 0, note)


def test_the_skill_names_every_tool_the_server_has():
    """A tool renamed or added is a skill out of step with the server."""
    skill = (cli.SKILLS / "author-arena-spec" / "SKILL.md").read_text()
    tools = {tool.name for tool in asyncio.run(mcp_server.mcp.list_tools())}
    assert {tool for tool in tools if f"`{tool}`" not in skill} == set()


def test_a_new_spec_validates(tmp_path: Path):
    """The skill starts every spec from it."""
    task = tmp_path / "tasks" / "new.yaml"
    cli.new(task)
    assert json.loads(_call("check_spec", spec=str(task)).content[0].text)["ok"] is True

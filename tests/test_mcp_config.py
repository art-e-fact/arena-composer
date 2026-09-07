"""The checked-in client configuration.

Each agent has its own config file and its own idea of variable expansion, but
all of them point at one launcher — so what is worth testing is that they still
name a launcher that exists, and that it starts from a working directory that
is not the repo root. That is the case a relative path silently breaks.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
LAUNCHER = REPO / "bin" / "arena-mcp"
# Built by `source setup.sh`; without it there is no server to start.
INSTALLED = (REPO / "submodules/IsaacLab-Arena/.venv/bin/arena-mcp").exists()


def test_the_launcher_is_executable():
    assert LAUNCHER.is_file(), f"every client config points at {LAUNCHER}"
    assert LAUNCHER.stat().st_mode & 0o111, "a client cannot exec it"


@pytest.mark.parametrize(
    ("config", "key"),
    [(".vscode/mcp.json", "servers"), (".cursor/mcp.json", "mcpServers")],
)
def test_workspace_folder_configs_name_the_launcher(config: str, key: str):
    """VS Code and Cursor both expand `${workspaceFolder}`, so they can point
    straight at it."""
    server = json.loads((REPO / config).read_text())[key]["arena"]
    command = server["command"].replace("${workspaceFolder}", str(REPO))
    assert Path(command) == LAUNCHER


@pytest.mark.skipif(not INSTALLED, reason="needs `source setup.sh`")
def test_the_mcp_json_command_starts_the_server_from_a_subdirectory():
    """Claude Code runs a server from wherever `claude` was started and does not
    expand `CLAUDE_PROJECT_DIR` in the command, so `.mcp.json` searches upwards.

    Started with no input, the server reaches end-of-stream and exits cleanly.
    """
    server = json.loads((REPO / ".mcp.json").read_text())["mcpServers"]["arena"]
    result = subprocess.run(
        [server["command"], *server["args"]],
        cwd=REPO / "arena_composer" / "python",
        input=b"",
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode()


@pytest.mark.skipif(not INSTALLED, reason="needs `source setup.sh`")
def test_the_mcp_json_command_fails_loudly_outside_the_repo(tmp_path: Path):
    """Nothing to search upwards to. The complaint has to go to stderr: on
    stdio a stray stdout line reads as a broken server rather than a missing
    checkout."""
    server = json.loads((REPO / ".mcp.json").read_text())["mcpServers"]["arena"]
    result = subprocess.run(
        [server["command"], *server["args"]],
        cwd=tmp_path,
        input=b"",
        capture_output=True,
    )
    assert result.returncode != 0
    assert not result.stdout
    assert b"arena-mcp" in result.stderr

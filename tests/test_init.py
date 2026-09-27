"""`arena-composer init`, run into a temporary project.

Every client config points at the one launcher, so what is worth testing is
that they name it, that it starts the server from a working directory other
than the project root — the case a relative path silently breaks — and that
every failure goes to stderr, since on stdio a stray stdout line reads as a
broken server rather than a missing install.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from arena_composer.cli import init

VENV = Path(sys.prefix)
# The server imports Arena as it starts.
INSTALLED = (VENV / "bin" / "arena-mcp").exists() and importlib.util.find_spec("isaaclab_arena") is not None


def mcp_json_command(project: Path) -> list[str]:
    server = json.loads((project / ".mcp.json").read_text())["mcpServers"]["arena"]
    return [server["command"], *server["args"]]


def test_the_configs_name_the_launcher(tmp_path: Path):
    init(tmp_path, VENV)
    launcher = tmp_path / "bin" / "arena-mcp"
    assert launcher.stat().st_mode & 0o111, "a client cannot exec it"
    for config, key in [(".vscode/mcp.json", "servers"), (".cursor/mcp.json", "mcpServers")]:
        command = json.loads((tmp_path / config).read_text())[key]["arena"]["command"]
        assert Path(command.replace("${workspaceFolder}", str(tmp_path))) == launcher


def test_what_exists_is_kept(tmp_path: Path):
    (tmp_path / ".mcp.json").write_text("{}")
    written = dict(init(tmp_path, VENV))
    assert (tmp_path / ".mcp.json").read_text() == "{}"
    assert not written[tmp_path / ".mcp.json"]
    assert written[tmp_path / "bin" / "arena-mcp"]


def test_a_venv_inside_the_project_moves_with_it(tmp_path: Path):
    """Named relative to the project, so the launcher can be checked in. The
    space in the venv's path is there to be quoted."""
    init(tmp_path / "a", tmp_path / "a" / "my env")
    (tmp_path / "a").rename(tmp_path / "b")
    result = subprocess.run([tmp_path / "b" / "bin" / "arena-mcp"], input=b"", capture_output=True)
    assert result.returncode == 1
    assert not result.stdout
    assert f"no server at {tmp_path / 'b' / 'my env' / 'bin' / 'arena-mcp'}\n".encode() in result.stderr


def test_a_venv_at_the_project_root_is_refused(tmp_path: Path):
    """Its `bin/arena-mcp` is where the launcher goes, so it would exec itself."""
    with pytest.raises(ValueError):
        init(tmp_path, tmp_path)
    assert not (tmp_path / "bin").exists()


@pytest.mark.skipif(not INSTALLED, reason="needs arena-composer and Arena installed in this venv")
def test_the_mcp_json_command_starts_the_server_from_a_subdirectory(tmp_path: Path):
    """Started with no input, the server reaches end-of-stream and exits cleanly."""
    init(tmp_path, VENV)
    (tmp_path / "sub").mkdir()
    result = subprocess.run(mcp_json_command(tmp_path), cwd=tmp_path / "sub", input=b"", capture_output=True)
    assert result.returncode == 0, result.stderr.decode()


def test_the_mcp_json_command_fails_loudly_outside_the_project(tmp_path: Path):
    init(tmp_path / "project", VENV)
    result = subprocess.run(mcp_json_command(tmp_path / "project"), cwd=tmp_path, input=b"", capture_output=True)
    assert result.returncode != 0
    assert not result.stdout
    assert b"arena-mcp" in result.stderr

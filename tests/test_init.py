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
import os
import subprocess
import sys
from pathlib import Path

import pytest

from arena_composer import cli
from arena_composer.cli import SKILLS, init, main

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


def test_an_import_reaches_the_server_through_a_stripped_environment(tmp_path: Path):
    """A client starts the launcher with next to no environment, so the
    launcher is where the imports have to come from. The space is there to be
    quoted."""
    server = tmp_path / "venv" / "bin" / "arena-mcp"
    server.parent.mkdir(parents=True)
    server.write_text('#!/usr/bin/env bash\necho "${ARENA_COMPOSER_IMPORTS}" >&2\n')
    server.chmod(0o755)
    init(tmp_path / "project", tmp_path / "venv", ("my_assets", "my tasks"))
    result = subprocess.run(
        [tmp_path / "project" / "bin" / "arena-mcp"], input=b"", capture_output=True, env={"PATH": os.environ["PATH"]}
    )
    assert result.returncode == 0, result.stderr.decode()
    assert result.stderr == f"my_assets{os.pathsep}my tasks\n".encode()


def test_an_import_a_kept_launcher_lacks_says_how_to_add_it(tmp_path: Path, capsys):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "arena-mcp").write_text("#!/usr/bin/env bash\n")
    assert main(["init", str(tmp_path), "--import", "my_assets"]) == 0
    assert "add this line to it:\n  export ARENA_COMPOSER_IMPORTS=my_assets\n" in capsys.readouterr().err


def test_a_kept_launcher_that_links_nowhere_gets_the_same_advice(tmp_path: Path, capsys):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "arena-mcp").symlink_to(tmp_path / "gone")
    assert main(["init", str(tmp_path), "--import", "my_assets"]) == 0
    assert "add this line to it:" in capsys.readouterr().err


def test_the_skills_are_linked_where_clients_look(tmp_path: Path):
    written = dict(init(tmp_path, VENV))
    for directory in (".agents/skills", ".claude/skills"):
        link = tmp_path / directory / "author-arena-spec"
        assert link.is_symlink() and link.resolve() == (SKILLS / "author-arena-spec").resolve()
        assert written[link]


def test_through_an_alias_the_second_link_is_the_first(tmp_path: Path):
    """A template project commits `.claude/skills -> ../.agents/skills`."""
    (tmp_path / ".agents" / "skills").mkdir(parents=True)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "skills").symlink_to("../.agents/skills")
    written = dict(init(tmp_path, VENV))
    assert written[tmp_path / ".agents/skills/author-arena-spec"]
    assert not written[tmp_path / ".claude/skills/author-arena-spec"]
    assert (tmp_path / ".claude/skills/author-arena-spec/SKILL.md").is_file()


def test_a_link_an_earlier_install_left_is_pointed_at_this_one(tmp_path: Path):
    """Switching between a release and an editable checkout moves the package."""
    stale = tmp_path / ".agents" / "skills" / "author-arena-spec"
    stale.parent.mkdir(parents=True)
    stale.symlink_to(tmp_path / "old-venv" / "author-arena-spec")
    init(tmp_path, VENV)
    assert stale.resolve() == (SKILLS / "author-arena-spec").resolve()


def test_an_install_inside_the_project_is_linked_relative_to_it(tmp_path: Path, monkeypatch):
    skills = tmp_path / "a" / "venv" / "skills"
    (skills / "mine").mkdir(parents=True)
    (skills / "mine" / "SKILL.md").write_text("---\nname: mine\n---\n")
    monkeypatch.setattr(cli, "SKILLS", skills)
    init(tmp_path / "a", tmp_path / "a" / "venv")
    (tmp_path / "a").rename(tmp_path / "b")
    assert (tmp_path / "b" / ".agents" / "skills" / "mine" / "SKILL.md").is_file()


def test_a_skills_directory_linked_out_of_the_project_still_reaches_the_skill(tmp_path: Path, monkeypatch):
    """A shared `.claude/skills` elsewhere: the link lands there, and is
    relative from there."""
    skills = tmp_path / "project" / "venv" / "skills"
    (skills / "mine").mkdir(parents=True)
    (skills / "mine" / "SKILL.md").write_text("---\nname: mine\n---\n")
    monkeypatch.setattr(cli, "SKILLS", skills)
    (tmp_path / "shared").mkdir()
    (tmp_path / "project" / ".claude").mkdir()
    (tmp_path / "project" / ".claude" / "skills").symlink_to(tmp_path / "shared")
    init(tmp_path / "project", tmp_path / "project" / "venv")
    assert (tmp_path / "shared" / "mine" / "SKILL.md").is_file()
    # A second run finds it current, rather than stale.
    assert not dict(init(tmp_path / "project", tmp_path / "project" / "venv"))[tmp_path / "project/.claude/skills/mine"]


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

"""`arena-composer init`: point a project's agent clients at this install's MCP server and skills.
`arena-composer new`: start a spec.

A client starts a server with a stripped environment and from a working
directory of its own choosing, and each client has its own config format. So
every config names one launcher, `bin/arena-mcp`, which finds the project from
its own path and execs the server installed in the venv that ran `init`.

`init` writes only the files that are missing, so a project's own edits survive
a re-run. Delete a file to have it written again.

It links the skills this package ships into `.agents/skills/` and
`.claude/skills/`, where clients look for them. A link follows the install, so
an upgrade updates the skill; one an earlier install left pointing elsewhere is
pointed at this one.

`--import MODULE` bakes `ARENA_COMPOSER_IMPORTS` into the launcher: the one
place a client's stripped environment can get it from.

Also the options the commands that solve a spec share: `add_spec_options` and
`apply_spec_options`.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path

from arena_composer import catalog
from arena_composer.spec_edit import DEFAULT_SEED

LAUNCHER_PATH = "bin/arena-mcp"
SKILLS = Path(__file__).parent / "skills"
SKILL_DIRS = (".agents/skills", ".claude/skills")
#: `new` copies these: one source for the scaffold and the skill that reads it.
TEMPLATES = SKILLS / "author-arena-spec" / "templates"

#: `@SERVER@` becomes the venv's `arena-mcp`, relative to the project when the
#: venv is inside it so the launcher can be checked in, else absolute.
#: `@IMPORTS@` becomes `export_line()` under a comment, or nothing.
LAUNCHER = """\
#!/usr/bin/env bash
# Start the arena_composer MCP server. Written by `arena-composer init`.
#
# Every MCP client config in this project names this file. It finds the project
# from its own path, so a client may start it from any working directory.
set -euo pipefail

PROJECT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SERVER=@SERVER@
@IMPORTS@
if [[ ! -x "${SERVER}" ]]; then
  # Only stderr: on stdio, stdout is the protocol, and a client reads a
  # non-JSON line there as a broken server rather than a missing install.
  echo "arena-mcp: no server at ${SERVER}" >&2
  echo "  Install arena-composer there, or delete this file and run" >&2
  echo "  'arena-composer init' from the venv it is installed in." >&2
  exit 1
fi

exec "${SERVER}" "$@"
"""

#: Claude Code starts a server from wherever `claude` was started and expands no
#: variables in `command`, so its config searches upwards for the launcher.
FIND_LAUNCHER = (
    f'r=$PWD; until [ -x "$r/{LAUNCHER_PATH}" ]; do r=${{r%/*}}; [ "$r" ] || '
    f"{{ echo 'arena: {LAUNCHER_PATH} not found above the working directory' >&2; exit 1; }}; done; "
    f'exec "$r/{LAUNCHER_PATH}"'
)

#: VS Code and Cursor expand `${workspaceFolder}`, so they name the launcher directly.
CONFIGS = {
    ".mcp.json": {"mcpServers": {"arena": {"command": "bash", "args": ["-c", FIND_LAUNCHER]}}},
    ".vscode/mcp.json": {"servers": {"arena": {"type": "stdio", "command": f"${{workspaceFolder}}/{LAUNCHER_PATH}"}}},
    ".cursor/mcp.json": {"mcpServers": {"arena": {"command": f"${{workspaceFolder}}/{LAUNCHER_PATH}"}}},
}


def export_line(imports: tuple[str, ...]) -> str:
    """The launcher's line that has the server import `imports` first."""
    return f"export {catalog.IMPORTS}={shlex.quote(os.pathsep.join(imports))}"


def init(project: Path, venv: Path, imports: tuple[str, ...] = ()) -> list[tuple[Path, bool]]:
    """Write the launcher and the client configs into `project`, keeping any
    that already exist. Returns each file with whether it was written."""
    server = venv / "bin" / "arena-mcp"
    if server == project / LAUNCHER_PATH:
        # The launcher would exec itself.
        raise ValueError(f"{venv} is the project root; init needs a venv of its own, inside it or elsewhere")
    if server.is_relative_to(project):
        server_expr = '"${PROJECT}"/' + shlex.quote(str(server.relative_to(project)))
    else:
        server_expr = shlex.quote(str(server))

    exported = (
        f"\n# Imported before any tool runs, for what they register with Arena.\n{export_line(imports)}\n"
        if imports
        else ""
    )
    files = {LAUNCHER_PATH: LAUNCHER.replace("@SERVER@", server_expr).replace("@IMPORTS@", exported)}
    files |= {name: json.dumps(config, indent=2) + "\n" for name, config in CONFIGS.items()}

    results = []
    for name, text in files.items():
        path = project / name
        if path.exists() or path.is_symlink():
            results.append((path, False))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        if name == LAUNCHER_PATH:
            path.chmod(0o755)
        results.append((path, True))
    return results + link_skills(project)


def link_skills(project: Path) -> list[tuple[Path, bool]]:
    """Link each shipped skill into `project`'s skill directories: relative when
    the install is inside the project, so the link moves with it, else absolute.
    Through a `.claude/skills -> .agents/skills` alias the second is the first,
    and kept."""
    results = []
    for skill in sorted(path.resolve() for path in SKILLS.iterdir() if (path / "SKILL.md").is_file()):
        for directory in SKILL_DIRS:
            link = project / directory / skill.name
            if link.is_symlink() and link.resolve() != skill:
                link.unlink()
            if link.exists() or link.is_symlink():
                results.append((link, False))
                continue
            link.parent.mkdir(parents=True, exist_ok=True)
            # From the directory the link lands in, which a skills directory
            # linked out of the project moves.
            link.symlink_to(os.path.relpath(skill, link.parent.resolve()) if skill.is_relative_to(project) else skill)
            results.append((link, True))
    return results


def new(task: Path) -> Path:
    """Write a task YAML at `task`, and the scene it includes, from the skill's
    templates. Returns the scene's path: in the `scenes` beside a `tasks`
    directory, as a template project lays them out, else beside the task."""
    in_tasks = task.parent.name == "tasks"
    scene = task.parent.parent / "scenes" / task.name if in_tasks else task.with_name(f"{task.stem}_scene.yaml")
    for path in (task, scene):
        if path.exists():
            raise FileExistsError(f"{path} exists")
    text = (TEMPLATES / "tasks" / "new_task.yaml").read_text()
    # Quoted, as JSON: a bare `on` or `yes` is a boolean to the YAML 1.1 Arena
    # reads, a ` #` starts a comment, and a leading `*` or `&` is an alias.
    text = text.replace("env_name: new_task", f"env_name: {json.dumps(task.stem)}")
    text = text.replace("../scenes/new_scene.yaml", json.dumps(os.path.relpath(scene, task.parent)))
    for path, body in ((scene, (TEMPLATES / "scenes" / "new_scene.yaml").read_text()), (task, text)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    return scene


def add_spec_options(parser: argparse.ArgumentParser) -> None:
    """`--seed`, `--asset-cache` and `--import`, which `arena-view`,
    `arena-resolve-layout` and `arena-render` take alike."""
    parser.add_argument(
        "--seed", type=int, default=DEFAULT_SEED, metavar="N", help=f"Placement seed (default: {DEFAULT_SEED})"
    )
    parser.add_argument(
        "--asset-cache",
        type=Path,
        metavar="DIR",
        help="Directory for downloaded USD assets (default: $ARENA_COMPOSER_ASSET_CACHE, else ~/.cache/arena-composer)",
    )
    parser.add_argument(
        "--import",
        dest="imports",
        action="append",
        default=[],
        metavar="MODULE",
        help=f"Import MODULE first, for the assets and kinds it registers with Arena; repeatable. "
        f"${catalog.IMPORTS} names more, separated by '{os.pathsep}'",
    )


def apply_spec_options(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Import the modules `--import` and `ARENA_COMPOSER_IMPORTS` name, and
    read USDs through `--asset-cache`. A module that does not import is one
    line on stderr and exit 1."""
    try:
        catalog.load_extensions(tuple(args.imports))
    except ImportError as error:
        parser.exit(1, f"{parser.prog}: {error}\n")
    # Imported here: it imports pxr, which `init` and `new` have no use for.
    from arena_composer.usd_assets import use_local_asset_cache

    use_local_asset_cache(args.asset_cache)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="arena-composer")
    commands = parser.add_subparsers(dest="command", required=True)
    init_parser = commands.add_parser(
        "init", help="Point a project's agent clients at this install's MCP server", description=__doc__
    )
    init_parser.add_argument(
        "project", nargs="?", default=".", type=Path, help="The project root (default: the working directory)"
    )
    init_parser.add_argument(
        "--import",
        dest="imports",
        action="append",
        default=[],
        metavar="MODULE",
        help="Have the server import MODULE first, for the assets and kinds it registers with Arena; repeatable",
    )
    new_parser = commands.add_parser(
        "new",
        help="Start a spec: a task and the scene it includes, a pick-and-place that builds",
        description=new.__doc__,
    )
    new_parser.add_argument("task", type=Path, help="The task YAML to write, e.g. environments/tasks/my_task.yaml")
    args = parser.parse_args(argv)

    if args.command == "new":
        try:
            scene = new(args.task)
        except OSError as error:
            parser.error(str(error))
        print(f"wrote {args.task}\nwrote {scene}")
        return 0

    project, venv = args.project.resolve(), Path(sys.prefix).resolve()
    if not (venv / "bin" / "arena-mcp").exists():
        print(f"arena-composer: {venv} has no arena-mcp; the launcher will not start", file=sys.stderr)
    try:
        results = init(project, venv, tuple(args.imports))
    except (ValueError, OSError) as error:
        parser.error(str(error))
    for path, written in results:
        print(f"{'wrote' if written else 'kept '} {path.relative_to(project)}")
    launcher = project / LAUNCHER_PATH
    line = export_line(tuple(args.imports))
    if args.imports and not (launcher.is_file() and line in launcher.read_text()):
        print(
            f"arena-composer: kept {LAUNCHER_PATH}, which does not import {', '.join(args.imports)}; "
            f"delete it and run init again, or add this line to it:\n  {line}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

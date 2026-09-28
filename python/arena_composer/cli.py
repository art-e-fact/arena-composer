"""`arena-composer init`: point a project's agent clients at this install's MCP server.

A client starts a server with a stripped environment and from a working
directory of its own choosing, and each client has its own config format. So
every config names one launcher, `bin/arena-mcp`, which finds the project from
its own path and execs the server installed in the venv that ran `init`.

`init` writes only the files that are missing, so a project's own edits survive
a re-run. Delete a file to have it written again.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

LAUNCHER_PATH = "bin/arena-mcp"

#: `@SERVER@` becomes the venv's `arena-mcp`, relative to the project when the
#: venv is inside it so the launcher can be checked in, else absolute.
LAUNCHER = """\
#!/usr/bin/env bash
# Start the arena_composer MCP server. Written by `arena-composer init`.
#
# Every MCP client config in this project names this file. It finds the project
# from its own path, so a client may start it from any working directory.
set -euo pipefail

PROJECT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SERVER=@SERVER@

if [[ ! -x "${SERVER}" ]]; then
  # Only stderr: on stdio, stdout is the protocol, and a client reads a
  # non-JSON line there as a broken server rather than a missing install.
  echo "arena-mcp: no server at ${SERVER}" >&2
  echo "  Install arena-composer there, or delete this file and run" >&2
  echo "  'arena-composer init' from the venv it is installed in." >&2
  exit 1
fi

# Isaac Sim exits when its EULA is not accepted and there is no terminal to
# accept it at, and a client starts a server with a stripped environment.
export OMNI_KIT_ACCEPT_EULA=YES
export ACCEPT_EULA=Y

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


def init(project: Path, venv: Path) -> list[tuple[Path, bool]]:
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

    files = {LAUNCHER_PATH: LAUNCHER.replace("@SERVER@", server_expr)}
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
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="arena-composer")
    commands = parser.add_subparsers(dest="command", required=True)
    init_parser = commands.add_parser(
        "init", help="Point a project's agent clients at this install's MCP server", description=__doc__
    )
    init_parser.add_argument(
        "project", nargs="?", default=".", type=Path, help="The project root (default: the working directory)"
    )
    args = parser.parse_args(argv)

    project, venv = args.project.resolve(), Path(sys.prefix).resolve()
    if not (venv / "bin" / "arena-mcp").exists():
        print(f"arena-composer: {venv} has no arena-mcp; the launcher will not start", file=sys.stderr)
    try:
        results = init(project, venv)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    for path, written in results:
        print(f"{'wrote' if written else 'kept '} {path.relative_to(project)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

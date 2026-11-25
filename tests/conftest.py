"""These tests import Arena, which is not on PyPI. A venv without it, like CI's, sets them aside; a
new test that needs Arena fails there until it is listed."""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

# ARENA_COMPOSER_TEST_ARENA=<a checkout of another Arena>: test against it in place of the venv's own
# (docs/development.md#tests). The venv's editable install would still serve what the checkout lacks.
if arena := os.environ.get("ARENA_COMPOSER_TEST_ARENA"):
    roots = [Path(arena), Path(arena, "isaaclab_arena_embodiments"), Path(arena, "isaaclab_arena_policies")]
    sys.path[:0] = [str(root) for root in roots if root.is_dir()]
    for name, finder in list(sys.modules.items()):
        if name.startswith("__editable___isaaclab"):
            mapping = getattr(finder, "MAPPING", {})
            for package in [package for package in mapping if package.startswith("isaaclab_arena")]:
                del mapping[package]

if importlib.util.find_spec("isaaclab_arena") is None:
    collect_ignore = [
        "test_catalog.py",
        "test_composed_assets.py",
        "test_mcp_server.py",
        "test_payload.py",
        "test_prim_tree.py",
        "test_resolve_checks.py",
        "test_simready.py",
        "test_spec_edit.py",
        "test_view_requests.py",
        "test_view_watch.py",
    ]


@pytest.fixture(autouse=True, scope="session")
def no_isaac_sim():
    """Importing `isaacsim` runs Kit's EULA check, which a user of the `arena-*` commands never
    accepted, so nothing the tests reach may import it."""
    yield
    assert "isaacsim" not in sys.modules, "something imported isaacsim"

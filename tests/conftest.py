"""These tests import Arena, which is not on PyPI. A venv without it, like CI's, sets them aside; a
new test that needs Arena fails there until it is listed."""

import importlib.util

if importlib.util.find_spec("isaaclab_arena") is None:
    collect_ignore = [
        "test_catalog.py",
        "test_composed_assets.py",
        "test_mcp_server.py",
        "test_payload.py",
        "test_resolve_checks.py",
        "test_simready.py",
        "test_spec_edit.py",
        "test_view_requests.py",
        "test_view_watch.py",
    ]

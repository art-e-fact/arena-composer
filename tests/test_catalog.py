"""Search over both asset sources, and the constructor check that keeps a
validated spec buildable. Against the live registry, like the edits: a search
that stubs it tests the uninteresting half."""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

import pytest

from arena_composer import catalog, simready


def test_a_remote_asset_is_found_with_the_params_to_add_it(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(simready, "index_path", lambda: tmp_path / "index.json")
    folder = f"{simready.PREFIX}Residential/Kitchen/Baskets/Plastic_Basket_A01"
    monkeypatch.setattr(
        simready,
        "list_keys",
        lambda: [f"{folder}/sm_misc_basket_plastic_a01_01.usd", f"{folder}/.thumbs/256x256/sm_misc_basket_plastic_a01_01.usd.png"],
    )
    simready.build()

    # The folder name, the category and the layer's file name all count.
    for query in ("plastic basket", "kitchen basket", "sm_misc"):
        (hit,) = [asset for asset in catalog.search(query) if asset.params]
        assert hit.name == "Plastic_Basket_A01", query
    assert hit.registry_name == "simready_usd_object"
    assert hit.role == "object" and hit.object_type == "rigid"
    assert hit.params == {"usd_path": hit.usd_path}
    assert hit.usd_path.startswith(simready.ROOT)


def test_without_an_index_search_is_the_registry(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(simready, "index_path", lambda: tmp_path / "none.json")
    assert catalog.remote() == ()
    assert catalog.search("banana")[0].registry_name == "banana_ycb_robolab"


def test_listing_assets_stays_off_the_network(monkeypatch):
    # Lightwheel's assets resolve their USD by asking its server when read.
    def offline(*args, **kwargs):
        raise OSError("no network in this test")

    monkeypatch.setattr(socket, "getaddrinfo", offline)
    catalog.catalog.cache_clear()
    microwave = next(asset for asset in catalog.catalog() if asset.registry_name == "microwave")
    assert microwave.usd_path is None


def test_only_the_generic_simready_object_needs_a_param():
    from isaaclab_arena.assets.registries import AssetRegistry

    simready = AssetRegistry().get_asset_by_name("simready_usd_object")
    banana = AssetRegistry().get_asset_by_name("banana_ycb_robolab")
    # Arena fills `name` from the entry's id.
    assert catalog.missing_params(simready, {}, given=("name",)) == ["usd_path"]
    assert catalog.missing_params(simready, {"usd_path": "x.usd"}, given=("name",)) == []
    assert catalog.missing_params(banana, {}, given=("name",)) == []


def test_a_kind_lists_the_params_no_control_edits_and_whether_arena_vetted_it():
    """A tuple range or a list of ids gets no form field, but a text edit can
    still set it, so the kind says it is there and what shape it has. Whether
    Arena marked the kind `agent_ready` travels with it; the rest are
    registered all the same."""
    by_name = {kind.name: kind for kind in (*catalog.relations(), *catalog.tasks())}
    assert by_name["on"].agent_ready and not by_name["GoalPoseTask"].agent_ready
    assert by_name["GoalPoseTask"].other_params["target_x_range"] == {
        "type": "tuple[float, float] | None",
        "required": False,
        "default": None,
    }
    assert by_name["LiftObjectTask"].other_params["goal_position_delta_xyz"]["default"] == [0.0, 0.0, 0.3]
    assert by_name["SortMultiObjectTask"].other_params["pick_up_object_list"] == {
        "type": "list[Asset]",
        "required": True,
        "default": None,
        "needs": "Asset",
    }
    assert "pick_up_object" not in by_name["PickAndPlaceTask"].other_params, "a field was listed twice"


def test_a_task_param_naming_a_node_says_the_class_it_needs():
    """The task asserts the class as Arena builds it, once Isaac Sim has
    started. Told only `node_ref`, agents opened a banana as a door."""
    fields = {(kind.name, field.name): field for kind in catalog.tasks() for field in kind.fields}
    for kind, param, needs in [
        ("OpenDoorTask", "openable_object", "Openable"),
        ("PlaceUprightTask", "placeable_object", "Placeable"),
        ("TurnKnobTask", "turnable_object", "Turnable"),
        ("LiftObjectTaskRL", "embodiment", "EmbodimentBase"),
        ("PickAndPlaceTask", "destination_object", "Asset"),
    ]:
        assert (fields[kind, param].kind, fields[kind, param].needs) == ("node_ref", needs), (kind, param)
    assert fields["PickAndPlaceTask", "destination_object"].optional
    assert not any(field.needs for field in fields.values() if field.kind != "node_ref")
    # Arena passes a relation its params as they are written, so none names a node.
    assert not any(field.kind == "node_ref" for kind in catalog.relations() for field in kind.fields)


@pytest.fixture
def fresh_extensions():
    """`load_extensions` runs once per process, and the catalog keeps what was
    registered when first asked: both start over, and leave the next test the
    same."""
    catalog.load_extensions.cache_clear()
    catalog.catalog.cache_clear()
    yield
    catalog.load_extensions.cache_clear()
    catalog.catalog.cache_clear()


def test_modules_are_imported_from_the_flag_and_the_variable(fresh_extensions, tmp_path: Path, monkeypatch):
    for name in ("flag_extension", "variable_extension"):
        (tmp_path / f"{name}.py").write_text("")
    monkeypatch.syspath_prepend(tmp_path)
    # An empty entry, as a trailing separator leaves, names nothing.
    monkeypatch.setenv(catalog.IMPORTS, f"variable_extension{os.pathsep}")
    catalog.load_extensions(("flag_extension",))
    assert {"flag_extension", "variable_extension"} <= sys.modules.keys()


def test_a_module_that_does_not_import_is_named_with_where_it_was_named(fresh_extensions, monkeypatch):
    monkeypatch.setenv(catalog.IMPORTS, "no_such_extension")
    with pytest.raises(ImportError) as refused:
        catalog.load_extensions()
    assert str(refused.value) == (
        "ARENA_COMPOSER_IMPORTS names no_such_extension, which does not import: No module named 'no_such_extension'"
    )


def test_a_module_that_raises_as_it_imports_is_named_too(fresh_extensions, tmp_path: Path, monkeypatch):
    (tmp_path / "broken_extension.py").write_text("register(\n")
    monkeypatch.syspath_prepend(tmp_path)
    with pytest.raises(ImportError, match="^--import names broken_extension, which does not import: SyntaxError"):
        catalog.load_extensions(("broken_extension",))
